//! "Has this meeting's video been compressed?" — answered from the
//! recording itself, with no marker file on disk.
//!
//! The app's compress stage replaces a video `source.<ext>` with an HEVC
//! `source.mp4`, so a compressed recording is an mp4/mov whose video
//! sample entry is `hvc1` or `hev1`. [`source_video_compressed`] reads
//! exactly that fourcc: it walks the ISO-BMFF box headers with seeks only
//! (top level → `moov` → `trak` → `mdia` → `minf` → `stbl` → `stsd`) and
//! never touches `mdat`, so the cost is a handful of 8-byte reads however
//! large the recording is.
//!
//! Best-effort like the listing it feeds: a file that cannot be opened, is
//! truncated or is not an mp4 at all is simply "not compressed" — there is
//! no error to report.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use crate::list::source_file_in;
use crate::media::is_video_extension;

/// The sample entries an HEVC video track is stored under.
const HEVC_SAMPLE_ENTRIES: [&[u8; 4]; 2] = [b"hvc1", b"hev1"];

/// The containers that are ISO-BMFF; the other video extensions (`mkv`,
/// `webm`, `avi`) are never the compress stage's output.
const ISO_BMFF_EXTENSIONS: [&str; 2] = ["mp4", "mov"];

/// How many sibling boxes one level is walked through before giving up —
/// a real file has a handful; this only bounds the work on garbage.
const MAX_BOXES_PER_LEVEL: usize = 1024;

/// Whether the video recording in `meeting_dir` has been compressed.
///
/// `None` when the meeting has no video recording (no `source.*` at all,
/// or an audio-only one); otherwise `Some(true)` exactly when the source
/// is an mp4/mov holding an HEVC video track. Never an error: anything
/// that cannot be read answers `Some(false)`.
pub fn source_video_compressed(meeting_dir: &Path) -> Option<bool> {
    let source = source_file_in(meeting_dir)?;
    let ext = source.extension()?.to_str()?.to_ascii_lowercase();
    if !is_video_extension(&ext) {
        return None;
    }
    if !ISO_BMFF_EXTENSIONS.contains(&ext.as_str()) {
        return Some(false);
    }
    let compressed = File::open(&source)
        .ok()
        .and_then(|mut file| has_hevc_track(&mut file))
        .unwrap_or(false);
    Some(compressed)
}

/// One box header: its type and where its payload starts and ends.
struct BoxHeader {
    kind: [u8; 4],
    body_start: u64,
    end: u64,
}

/// Reads the box header at `pos`, inside a parent that ends at `end`.
/// `None` for anything malformed: a short read, a size smaller than its
/// own header, or a box reaching past its parent.
fn read_box<R: Read + Seek>(reader: &mut R, pos: u64, end: u64) -> Option<BoxHeader> {
    reader.seek(SeekFrom::Start(pos)).ok()?;
    let mut head = [0u8; 8];
    reader.read_exact(&mut head).ok()?;
    let size32 = u32::from_be_bytes([head[0], head[1], head[2], head[3]]);
    let kind = [head[4], head[5], head[6], head[7]];

    let (header_len, box_end) = match size32 {
        // The box runs to the end of its parent (the file, at top level).
        0 => (8, end),
        // A 64-bit size follows the type.
        1 => {
            let mut large = [0u8; 8];
            reader.read_exact(&mut large).ok()?;
            (16, pos.checked_add(u64::from_be_bytes(large))?)
        }
        size => (8, pos.checked_add(u64::from(size))?),
    };
    let body_start = pos.checked_add(header_len)?;
    if box_end < body_start || box_end > end {
        return None;
    }
    Some(BoxHeader {
        kind,
        body_start,
        end: box_end,
    })
}

/// The boxes directly inside `start..end`, in order, until the range ends
/// or a header does not parse.
fn child_boxes<R: Read + Seek>(reader: &mut R, start: u64, end: u64) -> Vec<BoxHeader> {
    let mut boxes = Vec::new();
    let mut pos = start;
    while pos < end && boxes.len() < MAX_BOXES_PER_LEVEL {
        let Some(header) = read_box(reader, pos, end) else {
            break;
        };
        pos = header.end;
        boxes.push(header);
    }
    boxes
}

/// The first box of type `kind` directly inside `start..end`.
fn find_child<R: Read + Seek>(
    reader: &mut R,
    start: u64,
    end: u64,
    kind: &[u8; 4],
) -> Option<BoxHeader> {
    child_boxes(reader, start, end)
        .into_iter()
        .find(|header| &header.kind == kind)
}

/// The fourcc of the first sample entry of the track whose payload is
/// `start..end` (`mdia` → `minf` → `stbl` → `stsd`).
fn sample_entry_of<R: Read + Seek>(reader: &mut R, start: u64, end: u64) -> Option<[u8; 4]> {
    let mut range = (start, end);
    for kind in [b"mdia", b"minf", b"stbl", b"stsd"] {
        let found = find_child(reader, range.0, range.1, kind)?;
        range = (found.body_start, found.end);
    }
    // `stsd` payload: version+flags (4), entry count (4), then the first
    // entry's own box header -- size (4), fourcc (4).
    let entry = read_box(reader, range.0.checked_add(8)?, range.1)?;
    Some(entry.kind)
}

/// Whether the ISO-BMFF stream holds a track stored as HEVC. `None` when
/// the stream cannot be read at all.
fn has_hevc_track<R: Read + Seek>(reader: &mut R) -> Option<bool> {
    let file_end = reader.seek(SeekFrom::End(0)).ok()?;
    let Some(moov) = find_child(reader, 0, file_end, b"moov") else {
        return Some(false);
    };
    let tracks = child_boxes(reader, moov.body_start, moov.end);
    Some(
        tracks
            .iter()
            .filter(|header| &header.kind == b"trak")
            .any(|trak| {
                sample_entry_of(reader, trak.body_start, trak.end)
                    .is_some_and(|fourcc| HEVC_SAMPLE_ENTRIES.contains(&&fourcc))
            }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::Cursor;
    use tempfile::tempdir;

    /// A box with a 32-bit size.
    fn bx(kind: &[u8; 4], body: &[u8]) -> Vec<u8> {
        let mut out = ((body.len() + 8) as u32).to_be_bytes().to_vec();
        out.extend_from_slice(kind);
        out.extend_from_slice(body);
        out
    }

    /// A box with a 64-bit size (`size == 1`).
    fn bx64(kind: &[u8; 4], body: &[u8]) -> Vec<u8> {
        let mut out = 1u32.to_be_bytes().to_vec();
        out.extend_from_slice(kind);
        out.extend_from_slice(&((body.len() + 16) as u64).to_be_bytes());
        out.extend_from_slice(body);
        out
    }

    /// A `trak` whose first sample entry is `fourcc`.
    fn trak(fourcc: &[u8; 4]) -> Vec<u8> {
        let mut stsd = vec![0, 0, 0, 0, 0, 0, 0, 1];
        stsd.extend(bx(fourcc, &[0u8; 20]));
        let stbl = bx(b"stbl", &bx(b"stsd", &stsd));
        let minf = bx(b"minf", &[bx(b"vmhd", &[0u8; 12]), stbl].concat());
        let mdia = bx(b"mdia", &[bx(b"mdhd", &[0u8; 24]), minf].concat());
        bx(b"trak", &[bx(b"tkhd", &[0u8; 84]), mdia].concat())
    }

    fn moov(tracks: &[&[u8; 4]]) -> Vec<u8> {
        let mut body = bx(b"mvhd", &[0u8; 100]);
        for fourcc in tracks {
            body.extend(trak(fourcc));
        }
        bx(b"moov", &body)
    }

    fn ftyp() -> Vec<u8> {
        bx(b"ftyp", b"isom\0\0\x02\0isomiso2mp41")
    }

    fn sniff(bytes: Vec<u8>) -> bool {
        has_hevc_track(&mut Cursor::new(bytes)).expect("a cursor always reads")
    }

    #[test]
    fn hvc1_and_hev1_tracks_read_as_compressed() {
        assert!(sniff([ftyp(), moov(&[b"hvc1", b"mp4a"])].concat()));
        assert!(sniff([ftyp(), moov(&[b"hev1"])].concat()));
    }

    #[test]
    fn the_video_track_is_found_behind_an_audio_track() {
        assert!(sniff([ftyp(), moov(&[b"mp4a", b"hvc1"])].concat()));
    }

    #[test]
    fn an_h264_recording_is_not_compressed() {
        assert!(!sniff([ftyp(), moov(&[b"avc1", b"mp4a"])].concat()));
    }

    #[test]
    fn moov_after_mdat_is_still_found() {
        let bytes = [
            ftyp(),
            bx(b"mdat", &[0xAB; 4096]),
            moov(&[b"hvc1", b"mp4a"]),
        ]
        .concat();
        assert!(sniff(bytes));
    }

    #[test]
    fn a_64_bit_box_size_is_skipped_correctly() {
        let bytes = [ftyp(), bx64(b"mdat", &[0xAB; 512]), moov(&[b"hvc1"])].concat();
        assert!(sniff(bytes));

        let large_moov = bx64(b"moov", &trak(b"hev1"));
        assert!(sniff([ftyp(), large_moov].concat()));
    }

    #[test]
    fn a_zero_size_box_runs_to_the_end_of_the_file() {
        // `mdat` with size 0 swallows everything after it: no `moov`.
        let mut open_mdat = 0u32.to_be_bytes().to_vec();
        open_mdat.extend_from_slice(b"mdat");
        open_mdat.extend_from_slice(&[0xAB; 64]);
        assert!(!sniff([ftyp(), open_mdat].concat()));

        // A size-0 `moov` at the end is read to the end of the file.
        let mut open_moov = 0u32.to_be_bytes().to_vec();
        open_moov.extend_from_slice(b"moov");
        open_moov.extend(trak(b"hvc1"));
        assert!(sniff([ftyp(), open_moov].concat()));
    }

    #[test]
    fn truncated_and_garbage_streams_are_not_compressed() {
        let whole = [ftyp(), moov(&[b"hvc1"])].concat();
        for cut in [0, 5, ftyp().len() + 4, whole.len() - 30, whole.len() - 1] {
            assert!(!sniff(whole[..cut].to_vec()), "cut at {cut}");
        }
        assert!(!sniff(b"recording bytes".to_vec()));
        assert!(!sniff(vec![0xFF; 4096]));
        // A box claiming a size smaller than its own header.
        assert!(!sniff(
            [vec![0, 0, 0, 4], b"moov".to_vec(), vec![0; 64]].concat()
        ));
        // Zero-filled: every header is a size-0 box of an unknown type.
        assert!(!sniff(vec![0; 4096]));
    }

    #[test]
    fn a_meeting_without_a_video_source_has_no_answer() {
        let dir = tempdir().expect("tempdir");
        assert_eq!(source_video_compressed(dir.path()), None, "no source");
        assert_eq!(
            source_video_compressed(&dir.path().join("missing")),
            None,
            "no folder"
        );

        fs::write(dir.path().join("source.m4a"), moov(&[b"hvc1"])).expect("write");
        assert_eq!(source_video_compressed(dir.path()), None, "audio-only");
    }

    #[test]
    fn a_video_source_is_sniffed_by_container() {
        let hevc = [ftyp(), moov(&[b"hvc1", b"mp4a"])].concat();

        for (name, bytes, expected) in [
            ("source.mp4", hevc.clone(), true),
            ("source.MOV", hevc.clone(), true),
            ("source.mp4", [ftyp(), moov(&[b"avc1"])].concat(), false),
            ("source.mp4", b"not an mp4 at all".to_vec(), false),
            ("source.mp4", Vec::new(), false),
            // Only an mp4/mov is ever the compress stage's output.
            ("source.mkv", hevc.clone(), false),
            ("source.webm", hevc.clone(), false),
        ] {
            let dir = tempdir().expect("tempdir");
            fs::write(dir.path().join(name), bytes).expect("write");
            assert_eq!(
                source_video_compressed(dir.path()),
                Some(expected),
                "{name}"
            );
        }
    }
}

//! The `ffmpeg`-child-process implementation of [`MediaDecoder`].

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use super::{MediaDecoder, MediaError, Pcm, SAMPLE_RATE};
use crate::jobs::CancelToken;

/// How often a running ffmpeg is checked for completion while waiting.
const POLL: Duration = Duration::from_millis(20);

/// Decodes through a bundled `ffmpeg` binary.
#[derive(Debug, Clone)]
pub struct FfmpegDecoder {
    program: PathBuf,
}

impl FfmpegDecoder {
    /// Resolve the binary: the one the installer put in the application
    /// folder, else whatever `ffmpeg` is on `PATH`.
    ///
    /// The bundled copy is preferred, and by absolute path, so a shipped app
    /// never depends on what a developer happens to have installed -- and
    /// never picks up a stranger's `ffmpeg.exe` from a directory earlier in
    /// `PATH`. The fallback exists for the development loop only.
    pub fn new(app_dir: &Path) -> Self {
        let bundled = app_dir.join(if cfg!(windows) {
            "ffmpeg.exe"
        } else {
            "ffmpeg"
        });
        FfmpegDecoder {
            program: if bundled.is_file() {
                bundled
            } else {
                PathBuf::from("ffmpeg")
            },
        }
    }

    /// Use a specific binary (tests, and anyone reproducing a decode by hand).
    pub fn with_program(program: impl Into<PathBuf>) -> Self {
        FfmpegDecoder {
            program: program.into(),
        }
    }

    pub fn program(&self) -> &Path {
        &self.program
    }

    /// Whether the binary can be executed at all.
    pub fn is_available(&self) -> bool {
        Command::new(&self.program)
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .stdin(Stdio::null())
            .status()
            .is_ok()
    }

    fn spawn(&self, args: &[&str]) -> Result<Child, MediaError> {
        let mut command = Command::new(&self.program);
        command
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        no_console_window(&mut command);

        command.spawn().map_err(|err| {
            if err.kind() == std::io::ErrorKind::NotFound {
                MediaError::FfmpegMissing(self.program.display().to_string())
            } else {
                MediaError::Io(err)
            }
        })
    }

    /// Run ffmpeg, streaming stdout into memory, and stop early if cancelled.
    ///
    /// Returns the captured stdout and stderr plus whether ffmpeg reported
    /// success; a non-zero exit is not turned into an error here because some
    /// callers -- probing for a video stream -- expect one.
    fn run(&self, args: &[&str], cancel: &CancelToken) -> Result<Output, MediaError> {
        let mut child = self.spawn(args)?;

        // stdout is read on this thread and stderr on another: ffmpeg writes
        // to both, and a full pipe on either blocks the process forever.
        let mut stdout = child.stdout.take().expect("stdout is piped");
        let mut stderr_handle = child.stderr.take().expect("stderr is piped");
        let stderr_reader = std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = stderr_handle.read_to_end(&mut buf);
            buf
        });

        let mut data = Vec::new();
        let mut chunk = vec![0u8; 64 * 1024];
        let mut cancelled = false;
        loop {
            if cancel.is_cancelled() {
                cancelled = true;
                // Killing the child is the whole cancellation story for
                // decoding: there is no cooperative checkpoint inside ffmpeg.
                let _ = child.kill();
                break;
            }
            match stdout.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => data.extend_from_slice(&chunk[..n]),
                Err(err) => {
                    let _ = child.kill();
                    let _ = stderr_reader.join();
                    return Err(MediaError::Io(err));
                }
            }
        }

        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) => {
                    if cancel.is_cancelled() {
                        let _ = child.kill();
                    }
                    std::thread::sleep(POLL);
                }
                Err(err) => {
                    let _ = stderr_reader.join();
                    return Err(MediaError::Io(err));
                }
            }
        };

        let stderr = stderr_reader.join().unwrap_or_default();
        if cancelled {
            return Err(MediaError::Cancelled);
        }

        Ok(Output {
            stdout: data,
            stderr: String::from_utf8_lossy(&stderr).into_owned(),
            success: status.success(),
        })
    }

    /// Whether the file carries a video stream, so screenshots are even
    /// possible.
    ///
    /// Asking ffmpeg to decode with no output makes it print the stream table
    /// and exit non-zero, which is the cheapest probe available without also
    /// shipping `ffprobe`. Best-effort by design: a wrong answer costs a
    /// missing screenshot, which the caller reports as a warning, never a
    /// failed job.
    fn has_video_stream(&self, path: &Path, cancel: &CancelToken) -> Result<bool, MediaError> {
        let path = path.to_string_lossy().into_owned();
        let output = self.run(&["-hide_banner", "-i", &path], cancel)?;
        Ok(output.stderr.contains(": Video:"))
    }
}

struct Output {
    stdout: Vec<u8>,
    stderr: String,
    success: bool,
}

/// The last few lines of ffmpeg's output -- enough to say what went wrong
/// without pasting a decoder's whole life story into a job error.
fn tail(stderr: &str) -> String {
    let lines: Vec<&str> = stderr.lines().filter(|l| !l.trim().is_empty()).collect();
    let start = lines.len().saturating_sub(4);
    lines[start..].join("; ")
}

impl MediaDecoder for FfmpegDecoder {
    fn decode_pcm(&self, path: &Path, cancel: &CancelToken) -> Result<Pcm, MediaError> {
        let path_str = path.to_string_lossy().into_owned();
        let rate = SAMPLE_RATE.to_string();
        let output = self.run(
            &[
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                &path_str,
                // Video is irrelevant here and decoding it would cost the
                // whole file's worth of work for nothing.
                "-vn",
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                &rate,
                "pipe:1",
            ],
            cancel,
        )?;

        if !output.success {
            return Err(MediaError::Decode {
                path: path_str,
                detail: tail(&output.stderr),
            });
        }

        // f32le: four little-endian bytes per sample. A trailing partial
        // sample would mean a truncated pipe, so it is dropped rather than
        // read as noise.
        let samples = output
            .stdout
            .as_chunks::<4>()
            .0
            .iter()
            .map(|b| f32::from_le_bytes(*b))
            .collect();

        Ok(Pcm { samples })
    }

    fn extract_frames(
        &self,
        path: &Path,
        timestamps: &[f64],
        cancel: &CancelToken,
    ) -> Result<Vec<(f64, Vec<u8>)>, MediaError> {
        if timestamps.is_empty() {
            return Ok(Vec::new());
        }
        if !self.has_video_stream(path, cancel)? {
            return Ok(Vec::new());
        }

        let path_str = path.to_string_lossy().into_owned();
        let mut frames = Vec::new();
        for stamp in timestamps {
            if cancel.is_cancelled() {
                return Err(MediaError::Cancelled);
            }
            let seek = format!("{stamp:.3}");
            // `-ss` before `-i` seeks by index instead of decoding up to the
            // timestamp, which is the difference between instant and minutes
            // on a long recording.
            let output = self.run(
                &[
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    &seek,
                    "-i",
                    &path_str,
                    "-frames:v",
                    "1",
                    "-c:v",
                    "png",
                    "-f",
                    "image2",
                    "pipe:1",
                ],
                cancel,
            )?;

            // A timestamp past the end of the video yields no frame. That is
            // a screenshot the caller does without, not a failure: the item
            // it belongs to is still worth writing.
            if output.success && !output.stdout.is_empty() {
                frames.push((*stamp, output.stdout));
            }
        }
        Ok(frames)
    }
}

#[cfg(windows)]
fn no_console_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    // CREATE_NO_WINDOW: without it every decode flashes a console window over
    // the app, once per screenshot.
    command.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
fn no_console_window(_command: &mut Command) {}

#[cfg(test)]
mod tests {
    use super::*;

    /// These need a real ffmpeg. They are skipped rather than failed when
    /// there is none, so the suite still runs on a machine that has not set
    /// one up -- and `ffmpeg_is_wired_up` fails loudly if it is missing in an
    /// environment that should have it.
    fn decoder() -> Option<FfmpegDecoder> {
        let decoder = FfmpegDecoder::with_program("ffmpeg");
        decoder.is_available().then_some(decoder)
    }

    /// A synthetic tone, so the decode tests do not depend on a fixture file.
    fn write_test_audio(decoder: &FfmpegDecoder, path: &Path, seconds: f64) {
        let status = Command::new(decoder.program())
            .args([
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                &format!("sine=frequency=440:duration={seconds}"),
                &path.to_string_lossy(),
            ])
            .status()
            .expect("generate test audio");
        assert!(status.success(), "could not generate test audio");
    }

    #[test]
    fn bundled_ffmpeg_is_preferred_over_the_path() {
        let dir = tempfile::tempdir().unwrap();
        let bundled = dir.path().join(if cfg!(windows) {
            "ffmpeg.exe"
        } else {
            "ffmpeg"
        });
        std::fs::write(&bundled, b"not really ffmpeg").unwrap();

        assert_eq!(FfmpegDecoder::new(dir.path()).program(), bundled);
    }

    #[test]
    fn a_missing_bundled_binary_falls_back_to_the_path() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(
            FfmpegDecoder::new(dir.path()).program(),
            Path::new("ffmpeg")
        );
    }

    #[test]
    fn a_missing_binary_is_reported_as_a_broken_install_not_a_bad_file() {
        let decoder = FfmpegDecoder::with_program("definitely-not-ffmpeg-xyz");
        let err = decoder
            .decode_pcm(Path::new("whatever.mp4"), &CancelToken::default())
            .expect_err("should fail");
        assert!(matches!(err, MediaError::FfmpegMissing(_)), "{err:?}");
    }

    #[test]
    fn audio_decodes_to_sixteen_kilohertz_mono() {
        let Some(decoder) = decoder() else {
            eprintln!("skipping: no ffmpeg available");
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("tone.wav");
        write_test_audio(&decoder, &source, 2.0);

        let pcm = decoder
            .decode_pcm(&source, &CancelToken::default())
            .expect("decode");
        assert!(
            (pcm.duration_sec() - 2.0).abs() < 0.05,
            "expected ~2s, got {}",
            pcm.duration_sec()
        );
        // Mono at the pinned rate: the sample count *is* the duration, which
        // is what the rest of the engine assumes when it reports progress.
        assert_eq!(
            pcm.samples.len(),
            (pcm.duration_sec() * SAMPLE_RATE as f64).round() as usize
        );
        assert!(
            pcm.samples.iter().any(|s| s.abs() > 0.1),
            "a sine wave should not decode to silence"
        );
    }

    #[test]
    fn an_undecodable_file_is_attributed_with_ffmpegs_own_words() {
        let Some(decoder) = decoder() else {
            eprintln!("skipping: no ffmpeg available");
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("broken.mp4");
        std::fs::write(&source, b"this is not a video").unwrap();

        let err = decoder
            .decode_pcm(&source, &CancelToken::default())
            .expect_err("should fail");
        match err {
            MediaError::Decode { detail, .. } => assert!(!detail.is_empty()),
            other => panic!("expected a decode error, got {other:?}"),
        }
    }

    #[test]
    fn an_audio_only_file_yields_no_screenshots() {
        let Some(decoder) = decoder() else {
            eprintln!("skipping: no ffmpeg available");
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("tone.wav");
        write_test_audio(&decoder, &source, 1.0);

        let frames = decoder
            .extract_frames(&source, &[0.5], &CancelToken::default())
            .expect("extract");
        assert!(frames.is_empty(), "audio has no frames to extract");
    }

    #[test]
    fn no_timestamps_means_no_work() {
        // Checked before anything is spawned, so this holds even with no
        // ffmpeg present.
        let decoder = FfmpegDecoder::with_program("definitely-not-ffmpeg-xyz");
        assert!(decoder
            .extract_frames(Path::new("x.mp4"), &[], &CancelToken::default())
            .expect("no work")
            .is_empty());
    }

    #[test]
    fn a_cancelled_decode_stops_instead_of_finishing() {
        let Some(decoder) = decoder() else {
            eprintln!("skipping: no ffmpeg available");
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("long.wav");
        write_test_audio(&decoder, &source, 30.0);

        let cancel = CancelToken::default();
        cancel.cancel();
        let err = decoder.decode_pcm(&source, &cancel).expect_err("cancelled");
        assert!(matches!(err, MediaError::Cancelled), "{err:?}");
    }
}

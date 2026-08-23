//! Minimal GGUF header reading and GPU-offload fitting.
//!
//! Port of `services/transcription/src/transcription/llm/gguf_meta.py`.
//!
//! Just enough of the GGUF v2/v3 container format to answer one question --
//! how many transformer layers does this model have -- so the llama.cpp engine
//! can auto-fit `llm_gpu_layers` to the free VRAM. Only the metadata key-value
//! block at the top of the file is read; tensor data is never touched.
//!
//! Format reference: ggml's `gguf.md`. Header: magic `GGUF`, u32 version, u64
//! tensor count, u64 metadata KV count, then KV pairs of `(string key, u32
//! value type, value)`, all little-endian.
//!
//! Every failure -- a missing file, a container that is not GGUF, a truncated
//! or corrupt header -- collapses to "no block count". A model whose header
//! cannot be read still runs, on the CPU; failing the job over it would be a
//! worse trade than losing the offload.

use std::fs::File;
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::Path;

const MAGIC: [u8; 4] = *b"GGUF";

const TYPE_STRING: u32 = 8;
const TYPE_ARRAY: u32 = 9;

// A corrupt file must fail fast rather than spin through, or allocate for,
// entries that were never written. Real headers are two orders of magnitude
// under the KV cap; the array cap is generous enough for a full tokenizer
// vocabulary.
const MAX_KV_COUNT: u64 = 4096;
const MAX_STRING_BYTES: u64 = 1 << 20;
const MAX_ARRAY_LEN: u64 = 10_000_000;

/// A metadata value, narrowed to what this walk can act on. Anything else --
/// floats, arrays -- is read past and discarded, since neither key being
/// looked for can be one.
enum Value {
    Int(i64),
    Text(String),
    Other,
}

/// The model's transformer layer count (`<arch>.block_count`), or `None` when
/// the header cannot be read.
pub fn read_block_count(model_file: &Path) -> Option<u32> {
    let file = File::open(model_file).ok()?;
    block_count_of(&mut BufReader::new(file))
}

/// The header walk itself, over anything seekable, so the tests can pin it
/// against hand-built headers without touching a disk.
fn block_count_of<R: Read + Seek>(reader: &mut R) -> Option<u32> {
    let magic: [u8; 4] = read_fixed(reader)?;
    if magic != MAGIC {
        return None;
    }
    let version = read_u32(reader)?;
    if version < 2 {
        return None;
    }
    let _tensor_count = read_u64(reader)?;
    let kv_count = read_u64(reader)?;
    if kv_count > MAX_KV_COUNT {
        return None;
    }

    // The two keys can arrive in either order, and the architecture prefix is
    // only known once `general.architecture` has been seen -- so both are kept
    // and the pairing is retried after every entry, which lets the walk stop
    // at the second of the two rather than reading the whole block.
    let mut architecture: Option<String> = None;
    let mut block_counts: Vec<(String, i64)> = Vec::new();

    for _ in 0..kv_count {
        let key = read_string(reader)?;
        let value_type = read_u32(reader)?;
        let value = read_value(reader, value_type)?;

        if key == "general.architecture" {
            if let Value::Text(text) = value {
                architecture = Some(text);
            }
        } else if key.ends_with(".block_count") {
            if let Value::Int(count) = value {
                block_counts.push((key, count));
            }
        }

        if let Some(arch) = &architecture {
            let wanted = format!("{arch}.block_count");
            let found = block_counts.iter().find(|(key, _)| *key == wanted);
            if let Some((_, count)) = found {
                if *count > 0 {
                    return u32::try_from(*count).ok();
                }
            }
        }
    }
    None
}

fn read_fixed<R: Read, const N: usize>(reader: &mut R) -> Option<[u8; N]> {
    let mut buf = [0u8; N];
    reader.read_exact(&mut buf).ok()?;
    Some(buf)
}

fn read_u32<R: Read>(reader: &mut R) -> Option<u32> {
    Some(u32::from_le_bytes(read_fixed(reader)?))
}

fn read_u64<R: Read>(reader: &mut R) -> Option<u64> {
    Some(u64::from_le_bytes(read_fixed(reader)?))
}

/// The width of a fixed-size metadata value type, which is also what makes an
/// array of them skippable without decoding a single element.
fn scalar_size(value_type: u32) -> Option<usize> {
    match value_type {
        // uint8, int8, bool
        0 | 1 | 7 => Some(1),
        // uint16, int16
        2 | 3 => Some(2),
        // uint32, int32, float32
        4..=6 => Some(4),
        // uint64, int64, float64
        10..=12 => Some(8),
        _ => None,
    }
}

fn read_scalar<R: Read>(reader: &mut R, value_type: u32) -> Option<Value> {
    let size = scalar_size(value_type)?;
    let mut buf = [0u8; 8];
    reader.read_exact(&mut buf[..size]).ok()?;
    let value = match value_type {
        0 | 7 => i64::from(buf[0]),
        1 => i64::from(i8::from_le_bytes([buf[0]])),
        2 => i64::from(u16::from_le_bytes([buf[0], buf[1]])),
        3 => i64::from(i16::from_le_bytes([buf[0], buf[1]])),
        4 => i64::from(u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]])),
        5 => i64::from(i32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]])),
        10 => u64::from_le_bytes(buf) as i64,
        11 => i64::from_le_bytes(buf),
        _ => return Some(Value::Other),
    };
    Some(Value::Int(value))
}

/// Read one value of `value_type`, leaving the reader on the next key.
fn read_value<R: Read + Seek>(reader: &mut R, value_type: u32) -> Option<Value> {
    if scalar_size(value_type).is_some() {
        return read_scalar(reader, value_type);
    }
    match value_type {
        TYPE_STRING => Some(Value::Text(read_string(reader)?)),
        TYPE_ARRAY => {
            let element_type = read_u32(reader)?;
            let count = read_u64(reader)?;
            if count > MAX_ARRAY_LEN {
                return None;
            }
            if let Some(size) = scalar_size(element_type) {
                // Tokenizer scores and token types are the bulk of a real
                // header: seek over them rather than decode them.
                let skip = i64::try_from(size as u64 * count).ok()?;
                reader.seek(SeekFrom::Current(skip)).ok()?;
            } else if element_type == TYPE_STRING {
                // Self-describing lengths leave no arithmetic to skip by, so
                // the vocabulary has to be walked string by string.
                for _ in 0..count {
                    read_string(reader)?;
                }
            } else {
                return None;
            }
            Some(Value::Other)
        }
        _ => None,
    }
}

fn read_string<R: Read>(reader: &mut R) -> Option<String> {
    let length = read_u64(reader)?;
    if length > MAX_STRING_BYTES {
        return None;
    }
    let mut buf = vec![0u8; length as usize];
    reader.read_exact(&mut buf).ok()?;
    Some(String::from_utf8_lossy(&buf).into_owned())
}

/// Working memory the offload plan always leaves free on the GPU: KV cache,
/// compute buffers and allocator fragmentation, none of which scale with the
/// number of offloaded layers in a way this estimate could see.
pub const VRAM_RESERVE_BYTES: u64 = 2_000_000_000;

/// ...plus this fraction of the free VRAM, so a nearly-full card still keeps
/// proportional headroom.
pub const VRAM_RESERVE_FRACTION: f64 = 0.05;

/// How many whole layers fit on the GPU: `-1` for all of them (llama.cpp's
/// "everything, output layer included"), else a count.
///
/// The per-layer cost is approximated as an even split of the file over
/// `block_count + 1` (the +1 standing in for the embedding/output tensors) --
/// coarse, but the reserve absorbs the error, and a layer too few merely costs
/// a little speed where a layer too many aborts the load.
pub fn fit_gpu_layers(free_vram_bytes: u64, model_file_bytes: u64, block_count: u32) -> i32 {
    if free_vram_bytes == 0 || model_file_bytes == 0 || block_count == 0 {
        return 0;
    }
    let reserve = VRAM_RESERVE_BYTES + (free_vram_bytes as f64 * VRAM_RESERVE_FRACTION) as u64;
    if reserve >= free_vram_bytes {
        return 0;
    }
    let usable = (free_vram_bytes - reserve) as f64;
    let with_output = f64::from(block_count) + 1.0;
    let per_layer = model_file_bytes as f64 / with_output;
    let layers = (usable / per_layer).floor();
    if layers >= with_output {
        return -1;
    }
    layers.max(0.0) as i32
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    const GB: u64 = 1_000_000_000;

    fn gguf_string(value: &str) -> Vec<u8> {
        let mut bytes = (value.len() as u64).to_le_bytes().to_vec();
        bytes.extend_from_slice(value.as_bytes());
        bytes
    }

    fn header(version: u32, kv_count: u64) -> Vec<u8> {
        let mut bytes = MAGIC.to_vec();
        bytes.extend_from_slice(&version.to_le_bytes());
        bytes.extend_from_slice(&0u64.to_le_bytes());
        bytes.extend_from_slice(&kv_count.to_le_bytes());
        bytes
    }

    fn kv_string(key: &str, value: &str) -> Vec<u8> {
        let mut bytes = gguf_string(key);
        bytes.extend_from_slice(&TYPE_STRING.to_le_bytes());
        bytes.extend(gguf_string(value));
        bytes
    }

    fn kv_u32(key: &str, value: u32) -> Vec<u8> {
        let mut bytes = gguf_string(key);
        bytes.extend_from_slice(&4u32.to_le_bytes());
        bytes.extend_from_slice(&value.to_le_bytes());
        bytes
    }

    fn kv_float_array(key: &str, values: &[f32]) -> Vec<u8> {
        let mut bytes = gguf_string(key);
        bytes.extend_from_slice(&TYPE_ARRAY.to_le_bytes());
        bytes.extend_from_slice(&6u32.to_le_bytes());
        bytes.extend_from_slice(&(values.len() as u64).to_le_bytes());
        for value in values {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        bytes
    }

    /// A hand-built v3 header with just enough metadata, and a float array in
    /// the middle standing in for the tokenizer tables of a real one.
    fn minimal_gguf(arch: &str, block_count: u32) -> Vec<u8> {
        let mut bytes = header(3, 3);
        bytes.extend(kv_string("general.architecture", arch));
        bytes.extend(kv_float_array("tokenizer.scores", &[0.0, 1.0, 2.0, 3.0]));
        bytes.extend(kv_u32(&format!("{arch}.block_count"), block_count));
        bytes
    }

    fn block_count(bytes: Vec<u8>) -> Option<u32> {
        block_count_of(&mut Cursor::new(bytes))
    }

    #[test]
    fn the_block_count_is_read_past_the_arrays_on_the_way_to_it() {
        assert_eq!(block_count(minimal_gguf("qwen3moe", 48)), Some(48));
    }

    #[test]
    fn a_v2_container_reads_but_a_v1_one_does_not() {
        let mut v2 = header(2, 2);
        v2.extend(kv_string("general.architecture", "llama"));
        v2.extend(kv_u32("llama.block_count", 32));
        assert_eq!(block_count(v2), Some(32));

        let mut v1 = header(1, 2);
        v1.extend(kv_string("general.architecture", "llama"));
        v1.extend(kv_u32("llama.block_count", 32));
        assert_eq!(block_count(v1), None, "v1 counted in u32, not u64");
    }

    #[test]
    fn the_block_count_may_be_declared_before_the_architecture() {
        let mut bytes = header(3, 2);
        bytes.extend(kv_u32("qwen3moe.block_count", 36));
        bytes.extend(kv_string("general.architecture", "qwen3moe"));
        assert_eq!(block_count(bytes), Some(36));
    }

    #[test]
    fn a_block_count_belonging_to_another_architecture_is_ignored() {
        let mut bytes = header(3, 2);
        bytes.extend(kv_string("general.architecture", "qwen3moe"));
        bytes.extend(kv_u32("clip.block_count", 24));
        assert_eq!(block_count(bytes), None);
    }

    #[test]
    fn junk_and_truncated_headers_read_as_no_block_count() {
        assert_eq!(block_count(b"not a gguf file at all".to_vec()), None);

        let mut truncated = minimal_gguf("qwen3moe", 48);
        truncated.truncate(20);
        assert_eq!(block_count(truncated), None);
    }

    #[test]
    fn an_absurd_kv_count_is_refused_rather_than_walked() {
        // The pair is right there and would otherwise be found; the count
        // alone is what makes this header untrustworthy.
        let mut bytes = header(3, 100_000);
        bytes.extend(kv_string("general.architecture", "qwen3moe"));
        bytes.extend(kv_u32("qwen3moe.block_count", 48));
        assert_eq!(block_count(bytes), None);
    }

    #[test]
    fn an_absurd_string_length_is_refused_rather_than_allocated() {
        // The answer would be `None` either way once the read ran off the end
        // -- what is pinned here is that no buffer is reserved for the
        // declared length before that is discovered.
        let mut bytes = header(3, 1);
        bytes.extend_from_slice(&(2u64 << 20).to_le_bytes());
        bytes.extend_from_slice(b"short");
        assert_eq!(block_count(bytes), None);
    }

    #[test]
    fn an_unknown_value_type_ends_the_walk() {
        let mut bytes = header(3, 3);
        bytes.extend(gguf_string("something.new"));
        bytes.extend_from_slice(&99u32.to_le_bytes());
        bytes.extend(kv_string("general.architecture", "qwen3moe"));
        bytes.extend(kv_u32("qwen3moe.block_count", 48));
        assert_eq!(block_count(bytes), None, "no resync is possible");
    }

    #[test]
    fn a_model_file_on_disk_reads_its_block_count() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("model.gguf");
        std::fs::write(&path, minimal_gguf("qwen3moe", 48)).unwrap();
        assert_eq!(read_block_count(&path), Some(48));
    }

    #[test]
    fn a_model_file_that_is_not_there_reads_as_no_block_count() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(read_block_count(&dir.path().join("absent.gguf")), None);
    }

    #[test]
    fn a_partial_offload_never_exceeds_the_free_vram_less_its_reserve() {
        // A 20 GB, 48-layer model against ~11 GB free.
        let layers = fit_gpu_layers(11 * GB, 20 * GB, 48);
        assert!(layers > 0, "expected a partial offload, got {layers}");
        assert!(layers < 48, "expected a partial offload, got {layers}");

        let per_layer = (20 * GB) as f64 / 49.0;
        let budget = (11 * GB - VRAM_RESERVE_BYTES) as f64;
        assert!(f64::from(layers) * per_layer <= budget);
    }

    #[test]
    fn a_small_model_on_a_large_card_offloads_everything() {
        assert_eq!(fit_gpu_layers(24 * GB, 2 * GB, 32), -1);
    }

    #[test]
    fn nothing_is_offloaded_when_the_reserve_swallows_the_free_vram() {
        assert_eq!(fit_gpu_layers(GB, 20 * GB, 48), 0);
    }

    #[test]
    fn a_missing_measurement_offloads_nothing_rather_than_guessing() {
        assert_eq!(fit_gpu_layers(0, 20 * GB, 48), 0);
        assert_eq!(fit_gpu_layers(11 * GB, 20 * GB, 0), 0);
        assert_eq!(fit_gpu_layers(11 * GB, 0, 48), 0);
    }
}

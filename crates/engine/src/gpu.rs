//! What the machine's NVIDIA GPU can offer, if it has one.
//!
//! Every function here answers "no GPU" rather than failing. A machine with no
//! NVIDIA driver is the ordinary case, not a broken one, and the whole point of
//! measuring free VRAM is to decide how much work to give the GPU -- a
//! measurement that cannot be taken simply means none.
//!
//! NVML is loaded at runtime from the driver's own `nvml.dll`, so this links
//! and runs on a machine that has never had CUDA installed.

/// Free VRAM on the first NVIDIA device, in bytes.
///
/// Free rather than total: another process may already hold most of the card,
/// and offloading against a number nobody checked is how a job dies with an
/// out-of-memory error halfway through.
pub fn free_vram_bytes() -> Option<u64> {
    let nvml = nvml_wrapper::Nvml::init().ok()?;
    let device = nvml.device_by_index(0).ok()?;
    Some(device.memory_info().ok()?.free)
}

/// Whether an NVIDIA GPU is visible at all.
///
/// This is what decides whether the app offers the GPU download; it must never
/// offer one to a machine that could not use it.
pub fn nvidia_present() -> bool {
    nvml_wrapper::Nvml::init()
        .ok()
        .and_then(|nvml| nvml.device_count().ok())
        .is_some_and(|count| count > 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Runs on every machine, with or without a GPU: what is being tested is
    /// that the absence of one is an answer rather than a crash.
    #[test]
    fn probing_a_machine_without_nvidia_is_not_an_error() {
        let present = nvidia_present();
        let vram = free_vram_bytes();
        if present {
            assert!(
                vram.is_some_and(|bytes| bytes > 0),
                "a visible GPU should report free memory"
            );
        } else {
            assert_eq!(vram, None);
        }
    }
}

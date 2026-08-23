//! Which cited moments actually become screenshots.
//!
//! Pure planning, no decoding: given the timestamps an extracted item cites,
//! decide what is worth capturing. Two frames half a second apart show the
//! same slide, and an item illustrated with twenty near-identical images is
//! worse than one illustrated with three.
//!
//! Port of `services/transcription/src/transcription/frames.py`.

/// At most this many screenshots per action item or fact.
pub const MAX_SCREENSHOTS_PER_ITEM: usize = 6;

/// Two cited moments closer than this collapse into one screenshot.
pub const MIN_GAP_SEC: f64 = 2.0;

/// The moments to capture: ascending, deduplicated within the gap, capped.
///
/// Timestamps outside the recording are dropped rather than clamped -- a
/// citation past the end is a model's mistake, and a screenshot of the last
/// frame would illustrate the wrong moment convincingly.
pub fn plan_screenshots(
    timestamps: &[f64],
    duration_sec: Option<f64>,
    max_count: usize,
    min_gap_sec: f64,
) -> Vec<f64> {
    let mut valid: Vec<f64> = timestamps
        .iter()
        .copied()
        .filter(|stamp| *stamp >= 0.0 && duration_sec.is_none_or(|duration| *stamp <= duration))
        .collect();
    valid.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

    let mut planned: Vec<f64> = Vec::new();
    for stamp in valid {
        if planned.len() >= max_count {
            break;
        }
        if planned
            .last()
            .is_some_and(|last| stamp - last < min_gap_sec)
        {
            continue;
        }
        planned.push(stamp);
    }
    planned
}

/// [`plan_screenshots`] with the shipped limits.
pub fn plan(timestamps: &[f64], duration_sec: Option<f64>) -> Vec<f64> {
    plan_screenshots(
        timestamps,
        duration_sec,
        MAX_SCREENSHOTS_PER_ITEM,
        MIN_GAP_SEC,
    )
}

/// Deterministic screenshot filename: `screenshot-mmss.png`, or `-hmmss` past
/// an hour.
///
/// Deterministic so that re-running an extraction overwrites the same files
/// instead of accumulating near-duplicates beside them.
pub fn screenshot_name(timestamp_sec: f64) -> String {
    let total = timestamp_sec.max(0.0) as u64;
    let hours = total / 3600;
    let minutes = (total % 3600) / 60;
    let seconds = total % 60;
    if hours > 0 {
        format!("screenshot-{hours}{minutes:02}{seconds:02}.png")
    } else {
        format!("screenshot-{minutes:02}{seconds:02}.png")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn moments_are_sorted_and_deduplicated_within_the_gap() {
        // 10.5 is within two seconds of 10.0: the same slide.
        let planned = plan(&[30.0, 10.0, 10.5], Some(60.0));
        assert_eq!(planned, vec![10.0, 30.0]);
    }

    #[test]
    fn a_moment_past_the_end_is_dropped_not_clamped() {
        // Clamping would illustrate the wrong moment convincingly.
        assert_eq!(plan(&[5.0, 120.0], Some(60.0)), vec![5.0]);
    }

    #[test]
    fn a_negative_moment_is_dropped() {
        assert_eq!(plan(&[-1.0, 5.0], Some(60.0)), vec![5.0]);
    }

    #[test]
    fn an_unknown_duration_accepts_any_positive_moment() {
        assert_eq!(plan(&[5.0, 120.0], None), vec![5.0, 120.0]);
    }

    #[test]
    fn the_count_is_capped_per_item() {
        let many: Vec<f64> = (0..20).map(|i| i as f64 * 10.0).collect();
        assert_eq!(plan(&many, Some(1000.0)).len(), MAX_SCREENSHOTS_PER_ITEM);
    }

    #[test]
    fn no_timestamps_plan_nothing() {
        assert!(plan(&[], Some(60.0)).is_empty());
    }

    #[test]
    fn names_are_padded_and_grow_an_hour_field() {
        assert_eq!(screenshot_name(0.0), "screenshot-0000.png");
        assert_eq!(screenshot_name(65.4), "screenshot-0105.png");
        assert_eq!(screenshot_name(3661.0), "screenshot-10101.png");
    }

    #[test]
    fn the_same_moment_always_names_the_same_file() {
        // Re-running an extraction must overwrite, not accumulate.
        assert_eq!(screenshot_name(42.9), screenshot_name(42.1));
    }
}

"""Screenshot planning (pure logic, zero package imports beyond stdlib).

Given the transcript timestamps an extraction item cites, decide which
moments actually become screenshots: clamped into the recording, sorted,
deduplicated within a minimum gap (two frames 0.5 s apart show the same
slide), and capped per item.
"""

from __future__ import annotations

# At most this many screenshots per action item / fact.
MAX_SCREENSHOTS_PER_ITEM = 6

# Two cited moments closer than this collapse into one screenshot.
MIN_GAP_SEC = 2.0


def plan_screenshots(
    timestamps: list[float],
    duration_sec: float | None,
    *,
    max_count: int = MAX_SCREENSHOTS_PER_ITEM,
    min_gap_sec: float = MIN_GAP_SEC,
) -> list[float]:
    """The moments to actually capture, ascending, deduped, capped."""
    valid = sorted(
        stamp
        for stamp in timestamps
        if stamp >= 0 and (duration_sec is None or stamp <= duration_sec)
    )
    planned: list[float] = []
    for stamp in valid:
        if len(planned) >= max_count:
            break
        if planned and stamp - planned[-1] < min_gap_sec:
            continue
        planned.append(stamp)
    return planned


def screenshot_name(timestamp_sec: float) -> str:
    """Deterministic screenshot filename: ``screenshot-mmss.png`` (or hmmss)."""
    total = max(0, int(timestamp_sec))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"screenshot-{hours}{minutes:02d}{secs:02d}.png"
    return f"screenshot-{minutes:02d}{secs:02d}.png"

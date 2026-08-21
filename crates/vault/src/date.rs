//! Date component — `YYMMDD` validation and the local-date clock.
//!
//! Owned by T2. Every date string kept by this crate is validated against
//! a real Gregorian calendar (leap years included) and preserved verbatim
//! for the meeting folder name (FR-5) — this module never reformats it.
//! No filesystem access happens here, ever.

use crate::error::Rejection;
use chrono::{Datelike, Local, NaiveDate};

/// A `YYMMDD` date string that has been validated against a real Gregorian
/// calendar (FR-5).
///
/// Keeps the original six-character string verbatim alongside the parsed
/// [`chrono::NaiveDate`] — the meeting folder name must never reformat it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidDate {
    raw: String,
    date: NaiveDate,
}

impl ValidDate {
    /// The original, verbatim six-character `YYMMDD` string.
    pub fn as_str(&self) -> &str {
        &self.raw
    }

    /// The parsed calendar date.
    pub fn date(&self) -> NaiveDate {
        self.date
    }
}

/// Validates a date component: exactly six ASCII digits (never
/// `char::is_numeric`, per NFR-3) in `YYMMDD` form, denoting a real
/// Gregorian calendar date. `YY` is mapped onto `2000 + YY`, so the
/// supported range is 2000-2099.
pub fn validate(raw: &str) -> Result<ValidDate, Rejection> {
    if raw.len() != 6 || !raw.chars().all(|c| c.is_ascii_digit()) {
        return Err(Rejection::DateNotSixDigits);
    }

    // `len() == 6` together with every char being an ASCII digit means this
    // is exactly six ASCII bytes, so byte slicing is safe.
    let yy: i32 = raw[0..2].parse().map_err(|_| Rejection::DateNotSixDigits)?;
    let mm: u32 = raw[2..4].parse().map_err(|_| Rejection::DateNotSixDigits)?;
    let dd: u32 = raw[4..6].parse().map_err(|_| Rejection::DateNotSixDigits)?;

    let year = 2000 + yy;
    let date = NaiveDate::from_ymd_opt(year, mm, dd).ok_or(Rejection::DateNotACalendarDate)?;

    Ok(ValidDate {
        raw: raw.to_string(),
        date,
    })
}

/// Today's date in the local timezone.
pub fn today_local() -> NaiveDate {
    Local::now().date_naive()
}

/// Formats a [`chrono::NaiveDate`] as a six-character `YYMMDD` string, for
/// FR-10's "date added" prefix on unsorted recordings.
pub fn format_yymmdd(d: NaiveDate) -> String {
    let yy = d.year().rem_euclid(100);
    format!("{yy:02}{:02}{:02}", d.month(), d.day())
}

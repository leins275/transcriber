//! JSON serialization that is byte-identical to the Python service's output.
//!
//! Every document this crate writes already exists in users' vaults, written
//! by `json.dump(data, handle, ensure_ascii=False)`. That call is not the same
//! as `serde_json::to_string`: with `indent=None`, Python's encoder uses the
//! separators `", "` and `": "`, while serde_json's compact formatter uses
//! `","` and `":"`. Same JSON, different bytes -- and "different bytes" is
//! exactly what the golden-file tests exist to catch, so the difference is
//! removed here rather than tolerated.
//!
//! `ensure_ascii=False` needs no counterpart: serde_json emits real UTF-8 for
//! non-ASCII text already, which is what keeps a Russian transcript readable
//! in an editor instead of a wall of `\uXXXX`.

use serde::Serialize;
use serde_json::ser::{CompactFormatter, Formatter};
use std::io;

/// serde_json formatter matching Python's `json.dump` defaults.
#[derive(Clone, Debug, Default)]
pub struct PythonFormatter;

impl Formatter for PythonFormatter {
    fn begin_array_value<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_key<W>(&mut self, writer: &mut W, first: bool) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_value<W>(&mut self, writer: &mut W) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        writer.write_all(b": ")
    }

    // String escaping is CompactFormatter's behaviour, which already matches
    // Python's. Floats are not: see [`python_repr`].
    fn write_f64<W>(&mut self, writer: &mut W, value: f64) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        if value.is_finite() {
            writer.write_all(python_repr(value).as_bytes())
        } else {
            // JSON has no infinity or NaN. Python's encoder writes bare
            // `Infinity`/`NaN` here, which is not valid JSON and which no
            // transcript has ever contained -- so let serde reject it rather
            // than reproduce it.
            CompactFormatter.write_f64(writer, value)
        }
    }
}

/// Format a float the way CPython's `repr` does.
///
/// Both languages print the shortest digit string that round-trips, so the
/// digits agree. What differs is when each switches to exponent notation:
/// Rust stays positional far longer, so Python's `6.812810897827148e-05` comes
/// back as `0.00006812810897827148` -- the same number, different bytes, and a
/// rewritten transcript that no longer matches the one on disk. Found exactly
/// that way, against a real vault.
///
/// CPython's rule (`format_float_short`, repr mode) is a decision about the
/// decimal point's position: exponent notation when it sits at or before the
/// fourth place to the left, or past the sixteenth to the right.
fn python_repr(value: f64) -> String {
    let (negative, digits, decpt) = shortest_digits(value);
    let sign = if negative { "-" } else { "" };

    if decpt <= -4 || decpt > 16 {
        let head = &digits[..1];
        let tail = &digits[1..];
        let point = if tail.is_empty() {
            String::new()
        } else {
            format!(".{tail}")
        };
        // Python pads the exponent to two digits and always signs it.
        let exp = decpt - 1;
        return format!(
            "{sign}{head}{point}e{}{:02}",
            if exp < 0 { '-' } else { '+' },
            exp.abs()
        );
    }

    if decpt <= 0 {
        // Leading zeros after the point: 0.000123
        let zeros = "0".repeat((-decpt) as usize);
        format!("{sign}0.{zeros}{digits}")
    } else if (decpt as usize) >= digits.len() {
        // An integral value keeps a trailing `.0`, which is what makes a
        // float distinguishable from an int in the output.
        let zeros = "0".repeat(decpt as usize - digits.len());
        format!("{sign}{digits}{zeros}.0")
    } else {
        let (head, tail) = digits.split_at(decpt as usize);
        format!("{sign}{head}.{tail}")
    }
}

/// The shortest round-tripping digits of `value`, and where its decimal point
/// falls: `value == 0.<digits> * 10^decpt`.
///
/// The digits come from ryu rather than from Rust's own `{}`/`{:e}`, and the
/// difference is not cosmetic. Both produce a shortest representation, but
/// they break ties differently: for `0x1.148p-11`, whose exact value ends in
/// `...625`, ryu rounds half to even and prints `...62` while std prints
/// `...63`. Python rounds half to even too, so ryu is the one that agrees --
/// found in a real transcript, where one segment in a thousand differed.
fn shortest_digits(value: f64) -> (bool, String, i32) {
    let mut buffer = ryu::Buffer::new();
    let text = buffer.format_finite(value);

    let negative = text.starts_with('-');
    let text = text.strip_prefix('-').unwrap_or(text);

    // ryu picks positional or exponential on its own thresholds, which are not
    // Python's; both forms are normalised here so the decision is made once,
    // in `python_repr`.
    let (mantissa, exponent) = match text.split_once('e') {
        Some((mantissa, exponent)) => (mantissa, exponent.parse::<i32>().unwrap_or(0)),
        None => (text, 0),
    };
    let (integral, fractional) = mantissa.split_once('.').unwrap_or((mantissa, ""));

    let all = format!("{integral}{fractional}");
    let mut decpt = integral.len() as i32 + exponent;

    let significant = all.trim_start_matches('0');
    decpt -= (all.len() - significant.len()) as i32;
    let significant = significant.trim_end_matches('0');

    if significant.is_empty() {
        // Zero has no significant digits; `decpt` of 1 renders it as "0.0".
        return (negative, "0".to_string(), 1);
    }
    (negative, significant.to_string(), decpt)
}

/// Serialize to a `String` the way the Python service does.
pub fn to_string<T: Serialize>(value: &T) -> Result<String, serde_json::Error> {
    let mut buf = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, PythonFormatter);
    value.serialize(&mut ser)?;
    // serde_json only ever emits UTF-8.
    Ok(String::from_utf8(buf).expect("serde_json emits UTF-8"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn object_and_array_separators_match_python() {
        // Reference: python -c "import json; print(json.dumps({'a':1,'b':[1,2]}))"
        let value = json!({"a": 1, "b": [1, 2]});
        assert_eq!(to_string(&value).unwrap(), r#"{"a": 1, "b": [1, 2]}"#);
    }

    #[test]
    fn empty_containers_have_no_separators() {
        let value = json!({"a": {}, "b": []});
        assert_eq!(to_string(&value).unwrap(), r#"{"a": {}, "b": []}"#);
    }

    #[test]
    fn non_ascii_stays_unescaped() {
        // The ensure_ascii=False half of the contract: a Cyrillic transcript
        // must be readable in an editor.
        let value = json!({"text": "Привет"});
        assert_eq!(to_string(&value).unwrap(), r#"{"text": "Привет"}"#);
    }

    #[test]
    fn integral_floats_keep_their_decimal_point() {
        // Python's repr(1.0) is "1.0"; an int-looking float here would change
        // the bytes of every stats block whose realtime_factor lands on a
        // whole number.
        let value = json!({"realtime_factor": 1.0f64});
        assert_eq!(to_string(&value).unwrap(), r#"{"realtime_factor": 1.0}"#);
    }

    /// Every expectation here is the output of CPython's `repr` for the same
    /// literal, captured by running it -- not by reasoning about the rule.
    #[test]
    fn floats_are_formatted_exactly_as_python_reprs_them() {
        let cases: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-1.0, "-1.0"),
            (0.5, "0.5"),
            (12.5, "12.5"),
            (3.0, "3.0"),
            (0.1, "0.1"),
            // The switch to exponent notation on the small side happens
            // between these two.
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (6.812810897827148e-05, "6.812810897827148e-05"),
            (2.5e-8, "2.5e-08"),
            (1e-7, "1e-07"),
            (1e-300, "1e-300"),
            (2.0482456140350878, "2.0482456140350878"),
            (-0.04700969745372904, "-0.04700969745372904"),
            // ...and between these two on the large side.
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1e17, "1e+17"),
            (1.5e16, "1.5e+16"),
            (1e21, "1e+21"),
            (123456789012345.6, "123456789012345.6"),
            (1234567890123456.0, "1234567890123456.0"),
        ];

        for (value, expected) in cases {
            assert_eq!(&python_repr(*value), expected, "for {value:?}");
        }
    }

    #[test]
    fn every_formatted_float_reads_back_as_the_same_number() {
        // The formatting is only allowed to change how a number looks.
        for value in [
            6.812810897827148e-05,
            2.0482456140350878,
            -0.04700969745372904,
            1.5e16,
            f64::MIN_POSITIVE,
            f64::MAX,
            f64::MIN,
        ] {
            let text = python_repr(value);
            let parsed: f64 = text.parse().expect("parses back");
            assert_eq!(parsed.to_bits(), value.to_bits(), "{text}");
        }
    }

    #[test]
    fn nested_objects_nest_separators() {
        let value = json!({"outer": {"inner": [{"k": "v"}, 2]}});
        assert_eq!(
            to_string(&value).unwrap(),
            r#"{"outer": {"inner": [{"k": "v"}, 2]}}"#
        );
    }
}

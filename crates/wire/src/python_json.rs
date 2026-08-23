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

    // Everything else -- number and string escaping in particular -- is
    // CompactFormatter's behaviour, which already matches Python's.
    fn write_f64<W>(&mut self, writer: &mut W, value: f64) -> io::Result<()>
    where
        W: ?Sized + io::Write,
    {
        CompactFormatter.write_f64(writer, value)
    }
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

    #[test]
    fn nested_objects_nest_separators() {
        let value = json!({"outer": {"inner": [{"k": "v"}, 2]}});
        assert_eq!(
            to_string(&value).unwrap(),
            r#"{"outer": {"inner": [{"k": "v"}, 2]}}"#
        );
    }
}

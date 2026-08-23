//! Structured-output shapes for the extraction jobs.
//!
//! Two things have to agree about these: what the model is *allowed* to emit,
//! and what we accept when it does. In Python one pydantic model produced both
//! -- a JSON schema compiled to a grammar, and a validator. Here the grammar is
//! written out by hand in GBNF and the validator is serde, so the pairing is
//! held together by tests rather than by a shared object: every field in a
//! struct below has a rule in the grammar beside it, and
//! [`tests::a_grammar_shaped_answer_parses`] fails if they drift.
//!
//! Hand-written because the schemas are small, frozen, and only two: porting
//! pydantic's schema-to-grammar compiler to constrain four fields would be
//! more machinery than the thing it constrains.

use serde::{Deserialize, Serialize};

/// What kind of work an action item describes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ItemType {
    Requirement,
    Epic,
    Task,
    Spike,
}

/// Whether a fact is a statement or the answer to a question that was asked.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FactKind {
    /// A model that omits the field is stating something, not answering.
    #[default]
    Fact,
    AnsweredQuestion,
}

/// One extracted action item, as the model reports it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActionItemOut {
    #[serde(rename = "type")]
    pub item_type: ItemType,
    pub title: String,
    #[serde(default)]
    pub description_md: String,
    /// Seconds from the start of the recording, cited from the `[m:ss]`
    /// markers in the prompt.
    #[serde(default)]
    pub timestamps: Vec<f64>,
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct ActionItemsOut {
    #[serde(default)]
    pub items: Vec<ActionItemOut>,
}

/// One extracted fact or answered question.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FactOut {
    #[serde(default)]
    pub kind: FactKind,
    pub title: String,
    #[serde(default)]
    pub description_md: String,
    #[serde(default)]
    pub timestamps: Vec<f64>,
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct FactsOut {
    #[serde(default)]
    pub items: Vec<FactOut>,
}

/// Longest title we keep; the model is asked for a short one and occasionally
/// writes a paragraph, which would become an unusable folder name.
pub const MAX_TITLE_CHARS: usize = 200;

/// Shared GBNF rules: whitespace, strings and numbers.
const COMMON_RULES: &str = r#"
ws ::= [ \t\n]*
string ::= "\"" char* "\""
char ::= [^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" hex hex hex hex)
hex ::= [0-9a-fA-F]
number ::= "-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?
numbers ::= "[" ws (number (ws "," ws number)*)? ws "]"
"#;

/// The grammar that constrains an action-items answer.
///
/// Constraining generation rather than validating after the fact is what makes
/// the one repair retry rare: the model cannot emit a missing field or a
/// mistyped enum in the first place.
pub fn action_items_grammar() -> String {
    format!(
        r#"root ::= "{{" ws "\"items\"" ws ":" ws items ws "}}"
items ::= "[" ws (item (ws "," ws item)*)? ws "]"
item ::= "{{" ws "\"type\"" ws ":" ws itemtype ws "," ws "\"title\"" ws ":" ws string ws "," ws "\"description_md\"" ws ":" ws string ws "," ws "\"timestamps\"" ws ":" ws numbers ws "}}"
itemtype ::= "\"requirement\"" | "\"epic\"" | "\"task\"" | "\"spike\""
{COMMON_RULES}"#
    )
}

/// The grammar that constrains a facts answer.
pub fn facts_grammar() -> String {
    format!(
        r#"root ::= "{{" ws "\"items\"" ws ":" ws items ws "}}"
items ::= "[" ws (item (ws "," ws item)*)? ws "]"
item ::= "{{" ws "\"kind\"" ws ":" ws factkind ws "," ws "\"title\"" ws ":" ws string ws "," ws "\"description_md\"" ws ":" ws string ws "," ws "\"timestamps\"" ws ":" ws numbers ws "}}"
factkind ::= "\"fact\"" | "\"answered_question\""
{COMMON_RULES}"#
    )
}

/// The model's output was not valid JSON for the requested schema.
///
/// Carries the raw text so the caller's one repair retry can show the model
/// its own output.
#[derive(Debug, Clone, thiserror::Error)]
#[error("{message}")]
pub struct LlmOutputError {
    pub message: String,
    pub raw: String,
}

/// Parse and validate a model's JSON answer.
///
/// Tolerates a Markdown code fence around the JSON. Models add one even when
/// told not to, and even when a grammar should have prevented it -- an
/// unconstrained repair retry is exactly where it shows up.
pub fn parse_llm_json<T: serde::de::DeserializeOwned>(text: &str) -> Result<T, LlmOutputError> {
    let stripped = strip_code_fence(text.trim());
    serde_json::from_str(stripped).map_err(|err| LlmOutputError {
        message: format!("output does not match the schema: {err}"),
        raw: text.to_string(),
    })
}

fn strip_code_fence(text: &str) -> &str {
    let text = text.trim();
    let Some(rest) = text.strip_prefix("```") else {
        return text;
    };
    // ```json and ``` are both common; the language tag runs to end of line.
    let rest = match rest.find('\n') {
        Some(newline) => &rest[newline + 1..],
        None => rest,
    };
    rest.trim_end().strip_suffix("```").unwrap_or(rest).trim()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_action_items_answer_parses() {
        let out: ActionItemsOut = parse_llm_json(
            r#"{"items": [{"type": "task", "title": "Fix it", "description_md": "soon", "timestamps": [12.5]}]}"#,
        )
        .expect("parses");
        assert_eq!(out.items.len(), 1);
        assert_eq!(out.items[0].item_type, ItemType::Task);
        assert_eq!(out.items[0].timestamps, vec![12.5]);
    }

    #[test]
    fn a_facts_answer_parses_with_its_default_kind() {
        let out: FactsOut =
            parse_llm_json(r#"{"items": [{"title": "The API is public"}]}"#).expect("parses");
        assert_eq!(out.items[0].kind, FactKind::Fact);
        assert_eq!(out.items[0].description_md, "");
        assert!(out.items[0].timestamps.is_empty());
    }

    #[test]
    fn an_empty_answer_is_valid() {
        // A meeting with no action items is a real outcome, not a failure.
        let out: ActionItemsOut = parse_llm_json(r#"{"items": []}"#).expect("parses");
        assert!(out.items.is_empty());
    }

    #[test]
    fn a_code_fence_around_the_json_is_tolerated() {
        let fenced = "```json\n{\"items\": []}\n```";
        let out: ActionItemsOut = parse_llm_json(fenced).expect("parses");
        assert!(out.items.is_empty());

        let bare_fence = "```\n{\"items\": []}\n```";
        let out: ActionItemsOut = parse_llm_json(bare_fence).expect("parses");
        assert!(out.items.is_empty());
    }

    #[test]
    fn output_that_is_not_json_carries_its_own_text_back() {
        // The repair retry shows the model what it said, so the raw text has
        // to survive the failure.
        let err = parse_llm_json::<ActionItemsOut>("I cannot do that").expect_err("fails");
        assert_eq!(err.raw, "I cannot do that");
    }

    #[test]
    fn an_unknown_item_type_is_refused() {
        let err =
            parse_llm_json::<ActionItemsOut>(r#"{"items": [{"type": "wish", "title": "x"}]}"#)
                .expect_err("fails");
        assert!(err.message.contains("schema"), "{}", err.message);
    }

    /// The grammar and the struct are two statements of one shape; this is
    /// what keeps them from drifting apart.
    ///
    /// The field names are searched for as they appear *in the grammar text*,
    /// where a JSON key is a quoted literal and its quotes are escaped --
    /// `\"type\"`, backslashes included. Searching for the unescaped spelling
    /// silently matches nothing and the test passes while asserting nothing.
    #[test]
    fn a_grammar_shaped_answer_parses() {
        for field in ["type", "title", "description_md", "timestamps"] {
            let literal = format!("\\\"{field}\\\"");
            assert!(
                action_items_grammar().contains(&literal),
                "the action-items grammar is missing {literal}"
            );
        }
        for field in ["kind", "title", "description_md", "timestamps"] {
            let literal = format!("\\\"{field}\\\"");
            assert!(
                facts_grammar().contains(&literal),
                "the facts grammar is missing {literal}"
            );
        }
        for variant in ["requirement", "epic", "task", "spike"] {
            assert!(action_items_grammar().contains(variant));
            assert!(
                serde_json::from_str::<ItemType>(&format!("\"{variant}\"")).is_ok(),
                "{variant} is in the grammar but not in the enum"
            );
        }
        for variant in ["fact", "answered_question"] {
            assert!(facts_grammar().contains(variant));
            assert!(serde_json::from_str::<FactKind>(&format!("\"{variant}\"")).is_ok());
        }
    }
}

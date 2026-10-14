//! Tag rules — the optional last section of a recording's name, read as a
//! comma-separated list.
//!
//! A dropped file `<PRJ> - <DATE> - <NAME> - <tag>, <tag>, …` carries its
//! tags in the fourth section, and a filed meeting carries them as the third
//! section of its folder name (`<DATE> - <NAME> - <tag>, <tag>`). `,` is the
//! tag separator inside that section the way `-` is the section separator
//! around it; a title may still contain a comma, because the title is never
//! split on one. Pure — no filesystem, no path types.
//!
//! Each tag is held to the title's character rules through
//! [`crate::title::validate`] (Windows-illegal and control characters,
//! reserved device names), trimmed of surrounding whitespace and trailing
//! dots, and the list is de-duplicated case-insensitively keeping the first
//! spelling in order. The canonical on-disk form is [`join`]'s — one `, `
//! between tags — so a folder name is a pure function of its tag list and
//! round-trips through [`split`].

use crate::error::Rejection;
use crate::title;

/// The character separating tags inside the tag section.
pub const TAG_SEPARATOR: char = ',';

/// Reads a raw tag section into a validated, normalized list (FR-1, FR-2).
///
/// The section is split on every [`TAG_SEPARATOR`]; a piece that is empty
/// or only ASCII spaces is skipped (a trailing comma, a doubled comma), and
/// every other piece is validated as a title would be, re-labelling the two
/// rejections that name their subject: [`Rejection::EmptyTitle`] becomes
/// [`Rejection::EmptyTag`] and [`Rejection::IllegalTitleCharacter`] becomes
/// [`Rejection::IllegalTagCharacter`]. [`Rejection::ReservedDeviceName`]
/// passes through unchanged — it names the rule, not the section. A section
/// that yields no tag at all is [`Rejection::EmptyTag`]: the section was
/// present, so it was meant to carry something.
///
/// A `-` inside the section is deliberately **not** checked here. The
/// filename parser splits on every `-` before validating, so a section it
/// hands over can never contain one; an operator-supplied list reaches the
/// vault only through [`crate::manage::rename_meeting`], which refuses a `-`
/// in the title or in any tag with [`Rejection::ReservedSeparator`] before
/// anything moves.
pub fn validate(raw: &str) -> Result<Vec<String>, Rejection> {
    let mut tags: Vec<String> = Vec::new();
    for piece in raw.split(TAG_SEPARATOR) {
        if piece.trim_matches(' ').is_empty() {
            continue;
        }
        let tag = title::validate(piece).map_err(|reason| match reason {
            Rejection::EmptyTitle => Rejection::EmptyTag,
            Rejection::IllegalTitleCharacter(c) => Rejection::IllegalTagCharacter(c),
            other => other,
        })?;
        let lowered = tag.to_lowercase();
        if !tags.iter().any(|seen| seen.to_lowercase() == lowered) {
            tags.push(tag.to_string());
        }
    }
    if tags.is_empty() {
        return Err(Rejection::EmptyTag);
    }
    Ok(tags)
}

/// The canonical spelling of a tag list inside a folder name: the tags in
/// order, one `, ` between them. Empty for an empty list.
pub fn join(tags: &[String]) -> String {
    tags.join(", ")
}

/// Reads a tag section back into its tags without validating them — the
/// display-side inverse of [`join`], mirrored by the app's TypeScript
/// helper. Splits on every [`TAG_SEPARATOR`], trims ASCII spaces and drops
/// empty pieces, so `Standup, Q3` and `Standup,Q3` read the same.
pub fn split(section: &str) -> Vec<String> {
    section
        .split(TAG_SEPARATOR)
        .map(|piece| piece.trim_matches(' '))
        .filter(|piece| !piece.is_empty())
        .map(str::to_string)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tags(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn a_single_tag_is_accepted_verbatim() {
        assert_eq!(validate("Standup"), Ok(tags(&["Standup"])));
    }

    #[test]
    fn tags_are_split_on_commas_and_trimmed() {
        assert_eq!(
            validate(" Standup ,Q3,  Sprint review "),
            Ok(tags(&["Standup", "Q3", "Sprint review"]))
        );
    }

    #[test]
    fn empty_pieces_are_skipped() {
        assert_eq!(validate("Standup,,Q3,"), Ok(tags(&["Standup", "Q3"])));
    }

    #[test]
    fn duplicates_fold_case_insensitively_keeping_the_first_spelling() {
        assert_eq!(
            validate("Standup, standup, STANDUP, Q3"),
            Ok(tags(&["Standup", "Q3"]))
        );
    }

    #[test]
    fn an_empty_or_blank_section_is_an_empty_tag() {
        assert_eq!(validate(""), Err(Rejection::EmptyTag));
        assert_eq!(validate("   "), Err(Rejection::EmptyTag));
        assert_eq!(validate(", ,"), Err(Rejection::EmptyTag));
    }

    #[test]
    fn an_illegal_character_is_reported_against_the_tag() {
        assert_eq!(
            validate("Standup, Q:A"),
            Err(Rejection::IllegalTagCharacter(':'))
        );
    }

    #[test]
    fn a_control_character_beside_a_comma_is_not_trimmed_away() {
        assert_eq!(
            validate("Standup,\tQ3"),
            Err(Rejection::IllegalTagCharacter('\t'))
        );
    }

    #[test]
    fn a_reserved_device_name_passes_through_as_itself() {
        assert_eq!(validate("Standup, CON"), Err(Rejection::ReservedDeviceName));
    }

    #[test]
    fn a_hyphen_is_not_this_validators_concern() {
        assert_eq!(validate("Q3 - review"), Ok(tags(&["Q3 - review"])));
    }

    #[test]
    fn join_and_split_round_trip() {
        let list = tags(&["Standup", "Sprint review", "Q3"]);
        let joined = join(&list);
        assert_eq!(joined, "Standup, Sprint review, Q3");
        assert_eq!(split(&joined), list);
        assert_eq!(split("Standup,Sprint review,Q3"), list);
        assert_eq!(join(&[]), "");
        assert!(split("").is_empty());
        assert!(split(" , ").is_empty());
    }
}

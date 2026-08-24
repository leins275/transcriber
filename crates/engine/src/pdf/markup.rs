//! Markdown to Typst markup.
//!
//! The Python renderer went through HTML because xhtml2pdf reads HTML. Typst
//! reads its own markup, so the conversion is direct -- which also removes a
//! whole class of problem: no HTML means no HTML injection from a model's
//! output into a document we render.
//!
//! Everything a model or a template actually emits is handled: headings,
//! paragraphs, emphasis, inline and fenced code, nested lists, links, images,
//! block quotes, rules and tables. Anything else degrades to its text, which
//! is the right failure for a document whose value is its words.
//!
//! **Text is escaped, structure is not.** Typst markup gives meaning to
//! `#$*_`, backticks, `<>@[]~` and to `-+=/` at the start of a line, and every
//! one of those appears in ordinary meeting notes. Text runs are escaped
//! character by character; the structure around them is emitted by this module
//! and is trusted because this module wrote it.

use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};

/// The document preamble: page geometry and the font stack.
///
/// The font list is a fallback chain rather than one name. Arial matches every
/// PDF a vault already holds, and Libertinus Serif is compiled into the binary
/// -- so a machine without Arial still renders Cyrillic instead of boxes.
const PREAMBLE: &str = r#"#set page(paper: "a4", margin: (x: 1.6cm, y: 2cm))
#set text(font: ("Arial", "Libertinus Serif"), size: 10.5pt, lang: "ru")
#set par(justify: false, leading: 0.65em)
#show heading.where(level: 1): it => block(below: 0.8em)[#text(size: 18pt, weight: "bold")[#it.body]]
#show heading.where(level: 2): it => block(above: 1.2em, below: 0.6em)[#text(size: 14pt, weight: "bold")[#it.body]]
#show heading.where(level: 3): it => block(above: 1em, below: 0.4em)[#text(size: 12pt, weight: "bold")[#it.body]]
#show raw.where(block: true): it => block(fill: luma(240), inset: 6pt, radius: 2pt, width: 100%)[#it]
#set table(stroke: 0.5pt + luma(150))
"#;

/// Convert a Markdown document to a complete Typst source file.
pub fn to_typst(markdown: &str) -> String {
    let mut options = Options::empty();
    options.insert(Options::ENABLE_TABLES);
    options.insert(Options::ENABLE_STRIKETHROUGH);

    let mut out = String::with_capacity(markdown.len() * 2 + PREAMBLE.len());
    out.push_str(PREAMBLE);
    out.push('\n');

    let mut writer = Writer::default();
    for event in Parser::new_ext(markdown, options) {
        writer.handle(event, &mut out);
    }
    out
}

/// Which kind of list each open level is, so items get the right marker.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ListKind {
    Bullet,
    Numbered,
}

#[derive(Default)]
struct Writer {
    lists: Vec<ListKind>,
    /// Depth of open table cells, since cell content is a Typst argument
    /// rather than block markup.
    in_table: bool,
    /// Set while inside a fenced or indented code block, where text must be
    /// passed through unescaped.
    in_code_block: bool,
}

impl Writer {
    fn handle(&mut self, event: Event<'_>, out: &mut String) {
        match event {
            Event::Start(tag) => self.start(tag, out),
            Event::End(tag) => self.end(tag, out),
            Event::Text(text) => {
                if self.in_code_block {
                    out.push_str(&text);
                } else {
                    out.push_str(&escape(&text));
                }
            }
            Event::Code(code) => {
                // A raw span delimited by a backtick pair; the content is
                // verbatim, so only a backtick inside it needs care.
                out.push('`');
                out.push_str(&code.replace('`', ""));
                out.push('`');
            }
            Event::SoftBreak => out.push(' '),
            Event::HardBreak => out.push_str(" \\\n"),
            Event::Rule => out.push_str("\n#line(length: 100%, stroke: 0.5pt + luma(150))\n\n"),
            Event::TaskListMarker(done) => {
                out.push_str(if done { "☑ " } else { "☐ " });
            }
            // Raw HTML, footnotes and maths are not produced by anything that
            // writes these documents; dropping them keeps a stray tag from
            // reaching the page as literal text.
            Event::Html(_) | Event::InlineHtml(_) => {}
            Event::FootnoteReference(_) => {}
            Event::InlineMath(text) | Event::DisplayMath(text) => {
                out.push_str(&escape(&text));
            }
        }
    }

    fn start(&mut self, tag: Tag<'_>, out: &mut String) {
        match tag {
            Tag::Heading { level, .. } => {
                out.push('\n');
                out.push_str(&"=".repeat(heading_depth(level)));
                out.push(' ');
            }
            Tag::Paragraph => {
                if self.lists.is_empty() && !self.in_table {
                    out.push('\n');
                }
            }
            Tag::List(first) => {
                self.lists.push(match first {
                    Some(_) => ListKind::Numbered,
                    None => ListKind::Bullet,
                });
                out.push('\n');
            }
            Tag::Item => {
                // Two spaces per level: Typst reads indentation as nesting.
                let depth = self.lists.len().saturating_sub(1);
                out.push_str(&"  ".repeat(depth));
                out.push_str(match self.lists.last() {
                    Some(ListKind::Numbered) => "+ ",
                    _ => "- ",
                });
            }
            Tag::Emphasis => out.push('_'),
            Tag::Strong => out.push('*'),
            Tag::Strikethrough => out.push_str("#strike["),
            Tag::BlockQuote(_) => out.push_str("\n#quote(block: true)["),
            Tag::CodeBlock(kind) => {
                self.in_code_block = true;
                let language = match &kind {
                    pulldown_cmark::CodeBlockKind::Fenced(info) => {
                        info.split_whitespace().next().unwrap_or("").to_string()
                    }
                    pulldown_cmark::CodeBlockKind::Indented => String::new(),
                };
                // Four backticks so a fenced block that itself contains three
                // does not end the raw block early.
                out.push_str("\n````");
                out.push_str(&language);
                out.push('\n');
            }
            Tag::Link { dest_url, .. } => {
                out.push_str("#link(\"");
                out.push_str(&escape_string(&dest_url));
                out.push_str("\")[");
            }
            Tag::Image { dest_url, .. } => {
                // Width-capped rather than natural size: a screenshot at its
                // own resolution overflows an A4 page.
                out.push_str("\n#figure(image(\"");
                out.push_str(&escape_string(&dest_url));
                out.push_str("\", width: 85%))\n");
            }
            Tag::Table(_) => {
                self.in_table = true;
                out.push_str("\n#table(columns: auto,\n");
            }
            Tag::TableHead | Tag::TableRow => {}
            Tag::TableCell => out.push('['),
            Tag::HtmlBlock | Tag::MetadataBlock(_) | Tag::FootnoteDefinition(_) => {}
            Tag::DefinitionList
            | Tag::DefinitionListTitle
            | Tag::DefinitionListDefinition
            | Tag::Superscript
            | Tag::Subscript => {}
        }
    }

    fn end(&mut self, tag: TagEnd, out: &mut String) {
        match tag {
            TagEnd::Heading(_) => out.push('\n'),
            TagEnd::Paragraph => out.push('\n'),
            TagEnd::List(_) => {
                self.lists.pop();
                if self.lists.is_empty() {
                    out.push('\n');
                }
            }
            TagEnd::Item => out.push('\n'),
            TagEnd::Emphasis => out.push('_'),
            TagEnd::Strong => out.push('*'),
            TagEnd::Strikethrough => out.push(']'),
            TagEnd::BlockQuote(_) => out.push_str("]\n"),
            TagEnd::CodeBlock => {
                self.in_code_block = false;
                out.push_str("````\n");
            }
            TagEnd::Link => out.push(']'),
            // The image tag closed itself when it opened.
            TagEnd::Image => {}
            TagEnd::Table => {
                self.in_table = false;
                out.push_str(")\n");
            }
            TagEnd::TableHead | TagEnd::TableRow => out.push('\n'),
            TagEnd::TableCell => out.push_str("], "),
            TagEnd::HtmlBlock | TagEnd::MetadataBlock(_) | TagEnd::FootnoteDefinition => {}
            TagEnd::DefinitionList
            | TagEnd::DefinitionListTitle
            | TagEnd::DefinitionListDefinition
            | TagEnd::Superscript
            | TagEnd::Subscript => {}
        }
    }
}

fn heading_depth(level: HeadingLevel) -> usize {
    match level {
        HeadingLevel::H1 => 1,
        HeadingLevel::H2 => 2,
        HeadingLevel::H3 => 3,
        HeadingLevel::H4 => 4,
        HeadingLevel::H5 => 5,
        HeadingLevel::H6 => 6,
    }
}

/// Backslash-escape every character Typst markup gives a meaning to.
///
/// Deliberately generous: escaping a character that did not need it is
/// invisible in the output, while missing one turns a stray `*` in someone's
/// notes into bold text that runs to the end of the paragraph -- or a `#` into
/// a syntax error that fails the whole render.
fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 8);
    for ch in text.chars() {
        match ch {
            '\\' | '#' | '$' | '*' | '_' | '`' | '<' | '>' | '@' | '[' | ']' | '~' | '"' | '\''
            | '=' | '+' | '-' | '/' => {
                out.push('\\');
                out.push(ch);
            }
            _ => out.push(ch),
        }
    }
    out
}

/// Escape a value going inside a Typst string literal, such as a URL or an
/// image path.
fn escape_string(text: &str) -> String {
    text.replace('\\', "/").replace('"', "")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(markdown: &str) -> String {
        to_typst(markdown)
            .strip_prefix(PREAMBLE)
            .expect("output starts with the preamble")
            .trim()
            .to_string()
    }

    #[test]
    fn headings_become_equals_signs() {
        assert_eq!(body("# One"), "= One");
        assert_eq!(body("### Three"), "=== Three");
    }

    #[test]
    fn emphasis_and_strong_use_typsts_own_markers() {
        assert_eq!(body("*em* and **strong**"), "_em_ and *strong*");
    }

    #[test]
    fn bullet_and_numbered_lists_keep_their_kind() {
        assert_eq!(body("- a\n- b"), "- a\n- b");
        assert_eq!(body("1. a\n2. b"), "+ a\n+ b");
    }

    #[test]
    fn nested_lists_are_indented_by_level() {
        // Typst reads indentation as nesting, so the depth has to be real.
        let out = body("- a\n  - b\n    - c");
        assert!(out.contains("\n  - b"), "{out}");
        assert!(out.contains("\n    - c"), "{out}");
    }

    #[test]
    fn a_fenced_code_block_keeps_its_language_and_its_text_verbatim() {
        let out = body("```rust\nlet x = *y;\n```");
        assert!(out.starts_with("````rust"), "{out}");
        // Not escaped: this is the one place the source must survive as typed.
        assert!(out.contains("let x = *y;"), "{out}");
    }

    #[test]
    fn markup_characters_in_prose_are_escaped() {
        // The failure this prevents: a stray asterisk in someone's notes
        // turning the rest of the paragraph bold, or a `#` failing the render.
        let out = body("Costs #3 rose 5*2 and _x_ in C:\\path");
        assert!(!out.contains(" #3"), "{out}");
        assert!(out.contains("\\#3"), "{out}");
        assert!(out.contains("5\\*2"), "{out}");
        assert!(out.contains("C:\\\\path"), "{out}");
    }

    #[test]
    fn cyrillic_text_passes_through_untouched() {
        assert_eq!(body("Обсудили сроки"), "Обсудили сроки");
    }

    #[test]
    fn an_image_becomes_a_width_capped_figure() {
        // A screenshot at its own resolution would overflow the page.
        let out = body("![shot](screenshot-0000.png)");
        assert!(out.contains("image(\"screenshot-0000.png\""), "{out}");
        assert!(out.contains("width: 85%"), "{out}");
    }

    #[test]
    fn a_windows_style_image_path_is_normalised_for_typst() {
        let out = body("![s](sub\\dir\\shot.png)");
        assert!(out.contains("sub/dir/shot.png"), "{out}");
    }

    #[test]
    fn a_link_keeps_both_its_target_and_its_text() {
        let out = body("see [the docs](https://example.com/a)");
        assert!(out.contains("#link(\"https://example.com/a\")["), "{out}");
        assert!(out.contains("the docs]"), "{out}");
    }

    #[test]
    fn a_table_becomes_a_typst_table() {
        let out = body("| a | b |\n|---|---|\n| 1 | 2 |");
        assert!(out.starts_with("#table(columns: auto,"), "{out}");
        assert!(out.contains("[a], "), "{out}");
        assert!(out.ends_with(')'), "{out}");
    }

    #[test]
    fn a_block_quote_is_wrapped_rather_than_prefixed() {
        let out = body("> quoted");
        assert!(out.contains("#quote(block: true)["), "{out}");
    }

    #[test]
    fn raw_html_is_dropped_rather_than_printed() {
        // Item bodies come from a model; a stray tag must not reach the page
        // as literal text.
        let out = body("<script>alert(1)</script>\n\ntext");
        assert!(!out.contains("script"), "{out}");
        assert!(out.contains("text"), "{out}");
    }

    #[test]
    fn a_horizontal_rule_becomes_a_line() {
        assert!(body("---\n").contains("#line(length: 100%"));
    }

    #[test]
    fn the_preamble_names_a_cyrillic_capable_fallback() {
        // Arial is not on every machine; the fallback is what keeps a Russian
        // document from rendering as boxes.
        assert!(PREAMBLE.contains("Libertinus Serif"));
    }
}

//! Markdown to PDF.
//!
//! Replaces `pdf.py`, which went markdown → HTML → PDF through xhtml2pdf on
//! reportlab. Nothing in Rust covers that path, so this compiles Typst
//! instead: markdown becomes Typst markup and Typst produces the PDF.
//!
//! **Cyrillic is the primary case, not an edge one** -- these are Russian
//! meeting documents -- and it is why the font list matters. Typst's own
//! embedded families all cover Cyrillic, so a correct render never depends on
//! what the machine has installed. Arial is still preferred when Windows has
//! it, because that is what every PDF already exported from a vault was set
//! in.
//!
//! A failed render is a warning, never a failed job. The markdown export is
//! the deliverable and the PDF is the convenience; `pdf.py` made the same
//! choice, and the export job still depends on it.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use typst::diag::{FileError, FileResult};
use typst::foundations::{Bytes, Datetime, Duration};
use typst::syntax::{FileId, RootedPath, Source, VirtualPath, VirtualRoot};
use typst::text::{Font, FontBook};
use typst::utils::LazyHash;
use typst::{Library, LibraryExt, World};
use typst_layout::PagedDocument;

pub mod markup;

pub use markup::to_typst;

/// The PDF backend could not produce a document.
#[derive(Debug, thiserror::Error)]
#[error("could not render the PDF: {0}")]
pub struct PdfRenderError(String);

/// The Arial faces the Python renderer registered, when Windows has them.
fn system_font_candidates() -> Vec<PathBuf> {
    let dir = PathBuf::from(std::env::var("WINDIR").unwrap_or_else(|_| "C:\\Windows".to_string()))
        .join("Fonts");
    ["arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"]
        .iter()
        .map(|name| dir.join(name))
        .filter(|path| path.is_file())
        .collect()
}

/// Every font available to a render, loaded once per process.
///
/// Shared because parsing font files per document would dominate the cost of
/// rendering a two-page report.
fn fonts() -> &'static (LazyHash<FontBook>, Vec<Font>) {
    static FONTS: OnceLock<(LazyHash<FontBook>, Vec<Font>)> = OnceLock::new();
    FONTS.get_or_init(|| {
        let mut faces = Vec::new();

        // Preferred first, so a document asking for Arial gets Arial.
        for path in system_font_candidates() {
            if let Ok(data) = std::fs::read(&path) {
                faces.extend(Font::iter(Bytes::new(data)));
            }
        }
        // Always present: compiled into the binary, which is what makes a
        // correct Cyrillic render independent of the machine.
        for data in typst_assets::fonts() {
            faces.extend(Font::iter(Bytes::new(data)));
        }

        let book = FontBook::from_fonts(&faces);
        (LazyHash::new(book), faces)
    })
}

/// A Typst compilation whose only source is one in-memory document.
///
/// Deliberately narrow: no package loading, and no file access outside the
/// document's own directory, so a markdown file cannot reach anything the
/// caller did not put next to it.
struct ExportWorld {
    library: LazyHash<Library>,
    main: FileId,
    source: Source,
    /// Images referenced by the document resolve against this directory.
    base_dir: PathBuf,
}

impl ExportWorld {
    fn new(typst_source: String, base_dir: &Path) -> Self {
        let vpath = VirtualPath::new("main.typ").expect("a literal file name is a valid path");
        let main = FileId::new(RootedPath::new(VirtualRoot::Project, vpath));
        ExportWorld {
            library: LazyHash::new(Library::default()),
            main,
            source: Source::new(main, typst_source),
            base_dir: base_dir.to_path_buf(),
        }
    }

    /// Resolve a referenced file inside the document's own directory.
    ///
    /// Refusing to leave `base_dir` is the point: an export document is
    /// assembled partly from item bodies a model wrote, and a `../..` in one
    /// of them must not turn a PDF render into a file read.
    fn resolve(&self, id: FileId) -> FileResult<PathBuf> {
        let vpath = id.vpath();
        let resolved = vpath
            .realize(&self.base_dir)
            .map_err(|_| FileError::NotFound(vpath.get_without_slash().into()))?;

        let base = self
            .base_dir
            .canonicalize()
            .unwrap_or_else(|_| self.base_dir.clone());
        let canonical = resolved
            .canonicalize()
            .map_err(|err| FileError::from_io(err, &resolved))?;
        if !canonical.starts_with(&base) {
            return Err(FileError::AccessDenied);
        }
        Ok(canonical)
    }
}

impl World for ExportWorld {
    fn library(&self) -> &LazyHash<Library> {
        &self.library
    }

    fn book(&self) -> &LazyHash<FontBook> {
        &fonts().0
    }

    fn main(&self) -> FileId {
        self.main
    }

    fn source(&self, id: FileId) -> FileResult<Source> {
        if id == self.main {
            Ok(self.source.clone())
        } else {
            // Only the one in-memory document is a source; whatever else a
            // document references is data, not code.
            Err(FileError::NotSource)
        }
    }

    fn file(&self, id: FileId) -> FileResult<Bytes> {
        let path = self.resolve(id)?;
        std::fs::read(&path)
            .map(Bytes::new)
            .map_err(|err| FileError::from_io(err, &path))
    }

    fn font(&self, index: usize) -> Option<Font> {
        fonts().1.get(index).cloned()
    }

    fn today(&self, _offset: Option<Duration>) -> Option<Datetime> {
        // These documents never print a date they do not already carry in
        // their own text, so there is nothing to answer with -- and answering
        // would make the output depend on the clock.
        None
    }
}

/// Render `markdown` to PDF bytes; relative image links resolve against
/// `base_dir`.
pub fn render(markdown: &str, base_dir: &Path) -> Result<Vec<u8>, PdfRenderError> {
    let world = ExportWorld::new(markup::to_typst(markdown), base_dir);

    let compiled = typst::compile::<PagedDocument>(&world);
    let document = compiled.output.map_err(|errors| {
        let detail = errors
            .iter()
            .take(3)
            .map(|error| error.message.to_string())
            .collect::<Vec<_>>()
            .join("; ");
        PdfRenderError(format!(
            "typst reported {} error(s): {detail}",
            errors.len()
        ))
    })?;

    typst_pdf::pdf(&document, &typst_pdf::PdfOptions::default()).map_err(|errors| {
        PdfRenderError(format!("pdf export failed with {} error(s)", errors.len()))
    })
}

/// Render `markdown` and write it to `out_path` atomically.
pub fn render_to_file(
    markdown: &str,
    out_path: &Path,
    base_dir: &Path,
) -> Result<(), PdfRenderError> {
    let bytes = render(markdown, base_dir)?;
    wire::atomic::write_bytes(out_path, &bytes, ".export-")
        .map_err(|err| PdfRenderError(format!("could not write {}: {err}", out_path.display())))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A PDF must at least be a PDF: the header, a trailer, and enough bytes
    /// to be a page rather than an empty shell.
    fn assert_looks_like_pdf(bytes: &[u8]) {
        assert!(bytes.starts_with(b"%PDF-"), "not a PDF");
        assert!(
            bytes.windows(5).any(|w| w == b"%%EOF"),
            "PDF has no trailer"
        );
        assert!(
            bytes.len() > 1000,
            "PDF is suspiciously small: {} bytes",
            bytes.len()
        );
    }

    #[test]
    fn a_plain_document_renders() {
        let dir = tempfile::tempdir().unwrap();
        let pdf = render("# Title\n\nSome text.\n", dir.path()).expect("render");
        assert_looks_like_pdf(&pdf);
    }

    #[test]
    fn a_cyrillic_document_renders_text_that_reads_back() {
        // The case the font list exists for, checked properly: the text is
        // extracted back out of the finished PDF, so a render that succeeded
        // while dropping every Cyrillic glyph -- or drawing them as boxes --
        // fails here instead of passing.
        let dir = tempfile::tempdir().unwrap();
        let pdf = render(
            "# Отчёт по проекту

Обсудили сроки и договорились о демо.
",
            dir.path(),
        )
        .expect("render");
        assert_looks_like_pdf(&pdf);

        let extracted = pdf_extract::extract_text_from_mem(&pdf).expect("extract text");
        let flattened = extracted.split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(
            flattened.contains("Отчёт по проекту"),
            "the heading did not survive: {flattened:?}"
        );
        assert!(
            flattened.contains("договорились о демо"),
            "the body did not survive: {flattened:?}"
        );
    }

    #[test]
    fn the_same_markdown_renders_to_the_same_bytes() {
        // Determinism matters for a vault kept under version control:
        // re-exporting an unchanged meeting must not produce a changed file.
        let dir = tempfile::tempdir().unwrap();
        let md = "# Заголовок\n\n- один\n- два\n";
        assert_eq!(
            render(md, dir.path()).expect("render"),
            render(md, dir.path()).expect("render")
        );
    }

    #[test]
    fn headings_lists_code_and_quotes_survive() {
        let dir = tempfile::tempdir().unwrap();
        let md = concat!(
            "# One\n\n## Two\n\n",
            "- a\n- b\n  - nested\n\n",
            "1. first\n2. second\n\n",
            "```rust\nfn main() {}\n```\n\n",
            "> quoted\n\n",
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
        );
        assert_looks_like_pdf(&render(md, dir.path()).expect("render"));
    }

    #[test]
    fn an_image_beside_the_document_is_embedded() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("screenshot-0000.png"), one_pixel_png()).unwrap();

        let pdf = render(
            "# With a picture\n\n![shot](screenshot-0000.png)\n",
            dir.path(),
        )
        .expect("render");
        assert_looks_like_pdf(&pdf);
    }

    #[test]
    fn a_missing_image_is_reported_rather_than_silently_dropped() {
        // The caller turns this into a warning and keeps the markdown; what
        // matters is that it is reported at all.
        let dir = tempfile::tempdir().unwrap();
        let err = render("![gone](no-such-file.png)\n", dir.path()).expect_err("should fail");
        assert!(err.to_string().contains("typst reported"), "{err}");
    }

    #[test]
    fn a_document_cannot_read_outside_its_own_directory() {
        // Item bodies are model output; a traversal in one must not turn a
        // render into a file read.
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("secret.png"), one_pixel_png()).unwrap();
        let base = root.path().join("export");
        std::fs::create_dir(&base).unwrap();

        let err = render("![x](../secret.png)\n", &base).expect_err("should fail");
        assert!(err.to_string().contains("typst reported"), "{err}");
    }

    #[test]
    fn rendering_to_a_file_leaves_nothing_beside_it() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("nested").join("export.pdf");
        render_to_file("# Hi\n", &out, dir.path()).expect("render");

        assert!(out.is_file());
        assert_looks_like_pdf(&std::fs::read(&out).unwrap());

        let strays: Vec<String> = std::fs::read_dir(out.parent().unwrap())
            .unwrap()
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| name != "export.pdf")
            .collect();
        assert!(strays.is_empty(), "left behind {strays:?}");
    }

    /// The smallest valid PNG, so image tests have something real to embed.
    fn one_pixel_png() -> Vec<u8> {
        vec![
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48,
            0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00,
            0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41, 0x54, 0x78,
            0x9C, 0x63, 0x00, 0x01, 0x00, 0x00, 0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
            0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
        ]
    }
}

"""The standalone stdio MCP server: ``transcriber-mcp``.

Lets an MCP client (Claude Desktop and friends) query the meetings vault --
hybrid search, listings, transcripts, summaries, notes -- **without the
desktop app or the HTTP service running**: it opens the search index
read-only and reads vault files directly. Configuration comes from the same
sources the service uses (``TRANSCRIBER_APP_DIR``/``TRANSCRIBER_VAULT_ROOT``
env, or the installed ``config.json``); no port, no token.

stdout carries ONLY the MCP protocol (the same discipline as ``serve``'s
ready line): logging is forced to stderr before anything else runs.

Claude Desktop launch config (against the repo; the installed bake ships no
console scripts). ``--extra llm-cpu`` gives search its query embedder;
``TRANSCRIBER_APP_DIR`` points at the *install* dir (models + data live
there) while ``TRANSCRIBER_CONFIG_PATH`` names the shared config.json --
the app's ``meetings_root`` key doubles as the vault root::

    {"command": "uv",
     "args": ["run", "--project", "D:\\\\path\\\\to\\\\services\\\\transcription",
              "--extra", "llm-cpu", "transcriber-mcp"],
     "env": {"TRANSCRIBER_APP_DIR": "%LOCALAPPDATA%\\\\Transcriber",
             "TRANSCRIBER_CONFIG_PATH":
                 "%APPDATA%\\\\com.transcriber.desktop\\\\config.json"}}

Degradation over failure throughout: a missing index answers a friendly
"index not built yet" message, a missing embedding GGUF drops to text-only
search, and a missing vault root names the fix.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from transcription import paths
from transcription.artifacts import NOTE_FILE_NAME, SUMMARY_FILE_NAME, TRANSCRIPT_FILE_NAME
from transcription.config import Config, load_config
from transcription.exporting import load_speaker_overrides, load_transcript
from transcription.llm import get_embedder
from transcription.llm.prompts import render_transcript_lines
from transcription.search.dates import normalize_date_param
from transcription.search.index_db import IndexDb
from transcription.search.service import SearchService

_MAX_TEXT_BYTES = 4 * 1024 * 1024

_NO_INDEX_MESSAGE = (
    "The search index has not been built yet. Open the Transcriber app once "
    "(it indexes after every transcription and note save), then try again."
)

# Vault-root subdirectories that are not projects (the vault crate's
# reserved names plus legacy trees).
_RESERVED_DIRS = frozenset({"reports", "action items", "facts", "exports", "chats"})


def _is_reserved_dir(name: str) -> bool:
    """Not a project: a reserved/legacy tree, or any dot-dir (the index's
    own `.transcriber/` home, `.git`, sync-tool metadata, ...)."""
    return name.startswith(".") or name.lower() in _RESERVED_DIRS


class _Vault:
    """Lazily-resolved handles shared by the tools."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._db: IndexDb | None = None
        self._search: SearchService | None = None

    def root(self) -> Path:
        if not self.config.vault_root:
            raise ValueError(
                "no vault root is configured; set TRANSCRIBER_VAULT_ROOT or run "
                "the Transcriber app once so config.json records it"
            )
        return Path(self.config.vault_root)

    def meeting_dir(self, relative: str) -> Path:
        """Resolve a vault-relative meeting dir, refusing traversal."""
        root = self.root()
        return paths.resolve_under_roots(
            root / relative.replace("/", "\\" if "\\" in str(root) else "/"),
            [root],
            must_exist=True,
        )

    def index(self) -> IndexDb | None:
        if self._db is None:
            index_path = Path(self.config.index_db_path)
            if not index_path.is_file():
                return None
            self._db = IndexDb(
                index_path,
                embedding_model=self.config.embedding_model,
                embedding_dim=0,  # never migrates in read-only mode
                read_only=True,
            )
        return self._db

    def search_service(self) -> SearchService | None:
        db = self.index()
        if db is None:
            return None
        if self._search is None:
            self._search = SearchService(
                lambda: db,
                lambda: get_embedder(self.config),
                top_k_default=self.config.search_top_k,
            )
        return self._search


def _read_capped(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def build_server(config: Config) -> Any:
    """The FastMCP server with its tools bound to ``config``'s vault."""
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415 - keeps import lazy for tests

    vault = _Vault(config)
    server = FastMCP("transcriber")

    @server.tool()
    def hybrid_search(
        query: str, project: str | None = None, top_k: int = 10, date: str | None = None
    ) -> list[dict[str, Any]] | str:
        """Search all meeting transcripts, summaries and notes (hybrid:
        semantic + full-text + fuzzy titles). Returns ranked hits with a
        snippet, the meeting's vault-relative directory and a timestamp.
        ``date`` (``YYMMDD`` or ``YYYY-MM-DD``) hard-filters to that
        meeting day."""
        service = vault.search_service()
        if service is None:
            return _NO_INDEX_MESSAGE
        normalized = normalize_date_param(date)
        results = service.search(
            query, project=project, top_k=top_k, dates={normalized} if normalized else None
        )
        return [result.as_dict() for result in results]

    @server.tool()
    def list_projects() -> list[dict[str, Any]]:
        """List the vault's projects with their meeting counts."""
        root = vault.root()
        projects: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or _is_reserved_dir(entry.name):
                continue
            count = sum(1 for child in entry.iterdir() if child.is_dir())
            projects.append({"project": entry.name, "meeting_count": count})
        return projects

    @server.tool()
    def list_meetings(project: str | None = None) -> list[dict[str, Any]]:
        """List meetings (optionally one project's), newest-named first,
        with which artifacts each one has."""
        root = vault.root()
        meetings: list[dict[str, Any]] = []
        for project_dir in sorted(root.iterdir()):
            if not project_dir.is_dir() or _is_reserved_dir(project_dir.name):
                continue
            if project is not None and project_dir.name != project:
                continue
            for meeting in sorted(project_dir.iterdir(), reverse=True):
                if not meeting.is_dir():
                    continue
                meetings.append(
                    {
                        "meeting_dir": f"{project_dir.name}/{meeting.name}",
                        "project": project_dir.name,
                        "name": meeting.name,
                        "has_transcript": (meeting / TRANSCRIPT_FILE_NAME).is_file(),
                        "has_summary": (meeting / SUMMARY_FILE_NAME).is_file(),
                        "has_note": (meeting / NOTE_FILE_NAME).is_file(),
                    }
                )
        return meetings

    @server.tool()
    def read_transcript(
        meeting_dir: str, start_sec: float | None = None, end_sec: float | None = None
    ) -> str:
        """Read a meeting's transcript as timestamped, speaker-labelled
        lines (the operator's speaker names applied). ``meeting_dir`` is the
        vault-relative directory from search/listing results; the optional
        time window slices by segment start."""
        resolved = vault.meeting_dir(meeting_dir)
        transcript = load_transcript(resolved)
        if transcript is None:
            return f"No readable transcript in {meeting_dir}."
        segments_raw = transcript.get("segments")
        segments = [seg for seg in segments_raw if isinstance(seg, dict)] if segments_raw else []
        if start_sec is not None or end_sec is not None:
            low = start_sec if start_sec is not None else float("-inf")
            high = end_sec if end_sec is not None else float("inf")
            segments = [seg for seg in segments if low <= float(seg.get("start", 0.0)) <= high]
        lines = render_transcript_lines(segments, load_speaker_overrides(resolved))
        return "\n".join(lines) if lines else "The transcript is empty."

    @server.tool()
    def read_summary(meeting_dir: str) -> str:
        """Read a meeting's generated summary (markdown)."""
        resolved = vault.meeting_dir(meeting_dir)
        text = _read_capped(resolved / SUMMARY_FILE_NAME)
        return text if text is not None else f"No summary in {meeting_dir}."

    @server.tool()
    def read_note(meeting_dir: str) -> str:
        """Read the operator's own note for a meeting (markdown)."""
        resolved = vault.meeting_dir(meeting_dir)
        text = _read_capped(resolved / NOTE_FILE_NAME)
        return text if text is not None else f"No note in {meeting_dir}."

    return server


def main() -> None:
    """Entry point for the ``transcriber-mcp`` console script (stdio)."""
    # stdout purity: everything diagnostic goes to stderr, configured
    # before any import or tool can emit a line.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    config = load_config()
    build_server(config).run()


if __name__ == "__main__":
    main()

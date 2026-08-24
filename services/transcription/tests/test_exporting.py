"""Contract tests for per-recording export assembly (``exporting.py``).

FR-5 pins the app's indifference to ``archived``: the export document and
the artifact listing include archived items exactly like unarchived ones.
``archived`` is toggled by the operator's external editor and consumed only
there. No production code implements this -- ``_item_section`` reads title,
type and body and nothing else -- so these tests exist to make that
indifference a pinned contract: a future "hide archived items" change fails
loudly here instead of silently dropping content from an operator's export.
"""

from __future__ import annotations

from pathlib import Path

from transcription.artifacts import ACTION_ITEMS_DIR_NAME, list_items, write_item
from transcription.exporting import build_export_md

MEETING_NAME = "260824 - standup"


def _project_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(project_dir, meeting_dir, export_dir)`` in the layout
    ``build_export_md`` consumes via ``items_for_meeting``."""
    project_dir = tmp_path / "vault" / "ELS"
    meeting_dir = project_dir / MEETING_NAME
    export_dir = meeting_dir / "exports" / "260824"
    export_dir.mkdir(parents=True)
    (meeting_dir / "summary.md").write_text("The meeting summary.", encoding="utf-8")
    return project_dir, meeting_dir, export_dir


def _write_action_item(project_dir: Path, *, archived: bool) -> Path:
    return write_item(
        project_dir / ACTION_ITEMS_DIR_NAME,
        title="Fix login",
        meta={
            "type": "task",
            "title": "Fix login",
            "archived": archived,
            "source_meeting": MEETING_NAME,
        },
        body_md="Broken on refresh.",
        images=[],
    )


def _flip_archived_in_place(md_path: Path) -> None:
    """Simulate an external editor toggling the flag and nothing else."""
    text = md_path.read_text(encoding="utf-8")
    flipped = text.replace("archived: false", "archived: true", 1)
    assert flipped != text, "the fixture must actually carry `archived: false`"
    md_path.write_text(flipped, encoding="utf-8")


def test_export_output_is_identical_whether_an_item_is_archived(tmp_path: Path) -> None:
    project_dir, meeting_dir, export_dir = _project_tree(tmp_path)
    md_path = _write_action_item(project_dir, archived=False)

    def export() -> tuple[str, list[str]]:
        return build_export_md(
            meeting_dir=meeting_dir,
            meeting_name=MEETING_NAME,
            project_dir=project_dir,
            export_dir=export_dir,
        )

    before, warnings_before = export()
    assert "Fix login" in before, "the unarchived item is in the export to begin with"

    _flip_archived_in_place(md_path)
    after, warnings_after = export()

    assert after == before, "FR-5: archiving an item must not change the export document"
    assert warnings_after == warnings_before


def test_list_items_includes_an_archived_item_exactly_like_an_unarchived_one(
    tmp_path: Path,
) -> None:
    project_dir, _meeting_dir, _export_dir = _project_tree(tmp_path)
    md_path = _write_action_item(project_dir, archived=False)
    items_dir = project_dir / ACTION_ITEMS_DIR_NAME

    unarchived = list_items(items_dir)
    _flip_archived_in_place(md_path)
    archived = list_items(items_dir)

    assert len(unarchived) == 1
    assert len(archived) == 1, "FR-5: nothing filters archived items out of the listing"
    assert unarchived[0].meta["archived"] is False
    assert archived[0].meta["archived"] is True
    # Identical in every respect but the flag itself.
    assert archived[0].dir == unarchived[0].dir
    assert archived[0].body == unarchived[0].body
    assert {k: v for k, v in archived[0].meta.items() if k != "archived"} == {
        k: v for k, v in unarchived[0].meta.items() if k != "archived"
    }

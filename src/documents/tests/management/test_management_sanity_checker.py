"""Tests for the document_sanity_checker management command.

Verifies Rich rendering (table, panel, summary), orphan report rendering,
and end-to-end CLI behavior.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.core.management import call_command
from rich.console import Console

from documents.management.commands.document_sanity_checker import Command
from documents.sanity_checker import OrphanFileInfo
from documents.sanity_checker import OrphanSummary
from documents.sanity_checker import SanityCheckMessages
from documents.tests.factories import DocumentFactory

if TYPE_CHECKING:
    from documents.models import Document
    from documents.tests.conftest import PaperlessDirs


def _render_to_string(messages: SanityCheckMessages) -> str:
    """Render command output to a plain string for assertion."""
    buf = StringIO()
    cmd = Command()
    cmd.console = Console(file=buf, width=120, no_color=True)
    cmd._render_results(messages)
    return buf.getvalue()


def _render_orphans_to_string(orphan_summary: OrphanSummary) -> str:
    """Render orphan report to a plain string for assertion."""
    buf = StringIO()
    cmd = Command()
    cmd.console = Console(file=buf, width=120, no_color=True)
    cmd._render_orphan_report(orphan_summary)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


class TestRenderResultsNoIssues:
    """No DB access needed -- renders an empty SanityCheckMessages."""

    def test_shows_panel(self) -> None:
        output = _render_to_string(SanityCheckMessages())
        assert "No issues detected" in output
        assert "Sanity Check" in output


@pytest.mark.django_db
class TestRenderResultsWithIssues:
    def test_error_row(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.error(sample_doc.pk, "Original missing")
        output = _render_to_string(msgs)
        assert "Sanity Check Results" in output
        assert "ERROR" in output
        assert "Original missing" in output
        assert f"#{sample_doc.pk}" in output
        assert sample_doc.title in output

    def test_warning_row(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.warning(sample_doc.pk, "Suspicious file")
        output = _render_to_string(msgs)
        assert "WARN" in output
        assert "Suspicious file" in output

    def test_info_row(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.info(sample_doc.pk, "No OCR data")
        output = _render_to_string(msgs)
        assert "INFO" in output
        assert "No OCR data" in output

    @pytest.mark.usefixtures("_media_settings")
    def test_global_message(self) -> None:
        msgs = SanityCheckMessages()
        msgs.warning(None, "Orphaned file: /tmp/stray.pdf")
        output = _render_to_string(msgs)
        assert "(global)" in output
        assert "Orphaned file" in output

    def test_multiple_messages_same_doc(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.error(sample_doc.pk, "Thumbnail missing")
        msgs.error(sample_doc.pk, "Checksum mismatch")
        output = _render_to_string(msgs)
        assert "Thumbnail missing" in output
        assert "Checksum mismatch" in output

    @pytest.mark.usefixtures("_media_settings")
    def test_unknown_doc_pk(self) -> None:
        msgs = SanityCheckMessages()
        msgs.error(99999, "Ghost document")
        output = _render_to_string(msgs)
        assert "#99999" in output
        assert "Unknown" in output


@pytest.mark.django_db
class TestRenderResultsSummary:
    def test_errors_only(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.error(sample_doc.pk, "broken")
        output = _render_to_string(msgs)
        assert "1 document(s) with" in output
        assert "errors" in output

    def test_warnings_only(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.warning(sample_doc.pk, "odd")
        output = _render_to_string(msgs)
        assert "1 document(s) with" in output
        assert "warnings" in output

    def test_infos_only(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.info(sample_doc.pk, "no OCR")
        output = _render_to_string(msgs)
        assert "1 document(s) with infos" in output

    def test_empty_messages(self) -> None:
        msgs = SanityCheckMessages()
        output = _render_to_string(msgs)
        assert "No issues detected." in output

    def test_document_errors_and_global_warnings(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.error(sample_doc.pk, "broken")
        msgs.warning(None, "orphan")
        output = _render_to_string(msgs)
        assert "1 document(s) with" in output
        assert "errors" in output
        assert "1 global warning(s)" in output
        assert "2 document(s)" not in output

    def test_global_warnings_only(self) -> None:
        msgs = SanityCheckMessages()
        msgs.warning(None, "extra file")
        output = _render_to_string(msgs)
        assert "1 global warning(s)" in output
        assert "document(s) with" not in output

    def test_all_levels_combined(self, sample_doc: Document) -> None:
        msgs = SanityCheckMessages()
        msgs.error(sample_doc.pk, "broken")
        msgs.warning(sample_doc.pk, "odd")
        msgs.info(sample_doc.pk, "fyi")
        msgs.warning(None, "extra file")
        output = _render_to_string(msgs)
        assert "1 document(s) with errors" in output
        assert "1 document(s) with warnings" in output
        assert "1 document(s) with infos" in output
        assert "1 global warning(s)" in output


# ---------------------------------------------------------------------------
# Orphan report rendering
# ---------------------------------------------------------------------------


class TestRenderOrphanReport:
    def test_no_orphans_renders_nothing(self) -> None:
        summary = OrphanSummary()
        output = _render_orphans_to_string(summary)
        assert output.strip() == ""

    def test_orphan_table_and_summary(self) -> None:
        summary = OrphanSummary(
            orphans=[
                OrphanFileInfo(
                    path=Path("/media/documents/originals/stray.pdf"),
                    category="originals",
                    size=1024,
                ),
            ],
            by_category={"originals": {"count": 1, "size": 1024}},
            total_count=1,
            total_size=1024,
        )
        output = _render_orphans_to_string(summary)
        assert "Orphaned Files" in output
        assert "originals" in output
        assert "1.0 KB" in output
        assert "Orphan Summary" in output
        assert "--delete-orphans" in output

    def test_cleanup_results(self) -> None:
        summary = OrphanSummary(
            orphans=[
                OrphanFileInfo(
                    path=Path("/media/documents/originals/stray.pdf"),
                    category="originals",
                    size=2048,
                ),
            ],
            by_category={"originals": {"count": 1, "size": 2048}},
            total_count=1,
            total_size=2048,
            cleaned_up=True,
            freed_bytes=2048,
        )
        output = _render_orphans_to_string(summary)
        assert "Cleaned up" in output
        assert "freed" in output
        assert "--delete-orphans" not in output

    def test_multiple_categories(self) -> None:
        summary = OrphanSummary(
            orphans=[
                OrphanFileInfo(
                    path=Path("/media/documents/originals/a.pdf"),
                    category="originals",
                    size=100,
                ),
                OrphanFileInfo(
                    path=Path("/media/documents/archive/b.pdf"),
                    category="archive",
                    size=200,
                ),
            ],
            by_category={
                "originals": {"count": 1, "size": 100},
                "archive": {"count": 1, "size": 200},
            },
            total_count=2,
            total_size=300,
        )
        output = _render_orphans_to_string(summary)
        assert "originals" in output
        assert "archive" in output


# ---------------------------------------------------------------------------
# End-to-end command execution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.management
class TestDocumentSanityCheckerCommand:
    def test_no_issues(self, sample_doc: Document) -> None:
        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--no-progress-bar",
            stdout=out,
            skip_checks=True,
        )
        assert "No issues detected" in out.getvalue()

    def test_missing_original(self, sample_doc: Document) -> None:
        Path(sample_doc.source_path).unlink()
        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--no-progress-bar",
            stdout=out,
            skip_checks=True,
        )
        output = out.getvalue()
        assert "ERROR" in output
        assert "Original of document does not exist" in output

    @pytest.mark.usefixtures("_media_settings")
    def test_checksum_mismatch(self, paperless_dirs: PaperlessDirs) -> None:
        """Lightweight document with zero-byte files triggers checksum mismatch."""
        doc = DocumentFactory(
            title="test",
            content="test",
            filename="test.pdf",
            checksum="abc",
        )
        Path(doc.source_path).touch()
        Path(doc.thumbnail_path).touch()

        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--no-progress-bar",
            stdout=out,
            skip_checks=True,
        )
        output = out.getvalue()
        assert "ERROR" in output
        assert "Checksum mismatch. Stored: abc, actual:" in output

    def test_orphan_report_shown(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        """Orphaned files trigger the orphan report panel."""
        (paperless_dirs.originals / "orphan.pdf").write_bytes(b"orphan data")
        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--no-progress-bar",
            stdout=out,
            skip_checks=True,
        )
        output = out.getvalue()
        assert "Orphaned Files" in output
        assert "orphan.pdf" in output
        assert "Orphan Summary" in output

    def test_delete_orphans_flag(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        """--delete-orphans removes orphan files from the media directory."""
        orphan_path = paperless_dirs.originals / "orphan.pdf"
        orphan_path.write_bytes(b"orphan data")

        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--no-progress-bar",
            "--delete-orphans",
            stdout=out,
            skip_checks=True,
            stdin=StringIO("y\n"),  # Confirm deletion
        )
        output = out.getvalue()
        assert "Cleaned up" in output
        assert not orphan_path.exists()

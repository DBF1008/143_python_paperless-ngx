"""Tests for the document_sanity_checker management command.

Verifies Rich rendering (table, panel, summary) and end-to-end CLI behavior.
"""

from __future__ import annotations

import os
import time
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.core.management import call_command
from rich.console import Console

from documents.management.commands.document_sanity_checker import Command
from documents.sanity_checker import OrphanAnalysis
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


def _render_orphan_to_string(orphans: OrphanAnalysis, *, removed: bool) -> str:
    """Render only the orphan report to a plain string for assertion."""
    buf = StringIO()
    cmd = Command()
    cmd.console = Console(file=buf, width=120, no_color=True)
    cmd._render_orphan_report(orphans, removed=removed)
    return buf.getvalue()


def _make_orphan(
    directory: Path,
    name: str = "orphan.pdf",
    *,
    age_seconds: float = 0.0,
) -> Path:
    """Create a non-empty orphan file, optionally backdating its mtime."""
    path = directory / name
    path.write_bytes(b"orphan data")
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(path, (past, past))
    return path


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


# ---------------------------------------------------------------------------
# Orphan report rendering (unit -- no DB, no command execution)
# ---------------------------------------------------------------------------


class TestRenderOrphanReport:
    """Unit tests for _render_orphan_report against hand-built analyses."""

    def test_no_orphans_renders_nothing(self) -> None:
        output = _render_orphan_to_string(OrphanAnalysis(), removed=False)
        assert output == ""

    def test_report_only_shows_detected_and_hint(self) -> None:
        orphans = OrphanAnalysis(orphans=[Path("/media/orphan.pdf")])
        output = _render_orphan_to_string(orphans, removed=False)
        assert "Orphaned Files" in output
        assert "Detected" in output
        # Report-only mode points the operator at the cleanup flag.
        assert "--remove-orphans" in output

    def test_removed_shows_stats_and_reclaimed(self) -> None:
        path = Path("/media/orphan.pdf")
        orphans = OrphanAnalysis(
            orphans=[path],
            removed=[path],
            reclaimed_bytes=2048,
        )
        output = _render_orphan_to_string(orphans, removed=True)
        assert "Removed" in output
        assert "Skipped" in output
        assert "Reclaimed" in output
        assert "2.0 KiB" in output
        # The cleanup hint is suppressed once removal has run.
        assert "--remove-orphans" not in output

    def test_removed_with_errors_shows_notice(self) -> None:
        path = Path("/media/orphan.pdf")
        orphans = OrphanAnalysis(
            orphans=[path],
            errors=[(path, "permission denied")],
        )
        output = _render_orphan_to_string(orphans, removed=True)
        assert "Errors" in output
        assert "could not" in output


# ---------------------------------------------------------------------------
# Orphan removal -- end-to-end command execution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.management
class TestOrphanRemovalCommand:
    """CLI behavior for orphan reporting and the optional cleanup pass."""

    def test_report_only_shows_detected_and_hint(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        """Without --remove-orphans the file is reported but never deleted."""
        orphan = _make_orphan(paperless_dirs.originals, age_seconds=10_000)
        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--no-progress-bar",
            stdout=out,
            skip_checks=True,
        )
        output = out.getvalue()
        assert orphan.exists()
        assert "Orphaned Files" in output
        assert "Detected" in output
        assert "--remove-orphans" in output

    def test_remove_grace_zero_deletes_orphan(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        orphan = _make_orphan(paperless_dirs.originals, age_seconds=10_000)
        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--remove-orphans",
            "--orphan-grace-seconds",
            "0",
            "--no-progress-bar",
            stdout=out,
            skip_checks=True,
        )
        output = out.getvalue()
        assert not orphan.exists()
        assert "Orphaned Files" in output
        assert "Removed" in output

    def test_remove_default_grace_skips_recent(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        """A file modified within the grace window is reported but kept."""
        orphan = _make_orphan(paperless_dirs.originals)  # mtime = now
        out = StringIO()
        call_command(
            "document_sanity_checker",
            "--remove-orphans",
            "--no-progress-bar",
            stdout=out,
            skip_checks=True,
        )
        output = out.getvalue()
        assert orphan.exists()
        assert "Skipped" in output

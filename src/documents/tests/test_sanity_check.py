"""Tests for the sanity checker module.

Tests exercise ``check_sanity`` as a whole, verifying document validation,
orphan detection, and the iter_wrapper contract.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from documents.sanity_checker import OrphanFileInfo
from documents.sanity_checker import OrphanSummary
from documents.sanity_checker import _build_orphan_summary
from documents.sanity_checker import _classify_orphan
from documents.sanity_checker import _format_size
from documents.sanity_checker import _verify_orphans
from documents.sanity_checker import check_sanity
from documents.sanity_checker import handle_orphan_cleanup

if TYPE_CHECKING:
    from collections.abc import Iterable

    from documents.models import Document
    from documents.tests.conftest import PaperlessDirs


@pytest.mark.django_db
class TestCheckSanityNoDocuments:
    """Sanity checks against an empty archive."""

    @pytest.mark.usefixtures("_media_settings")
    def test_no_documents(self) -> None:
        messages = check_sanity()
        assert not messages.has_error
        assert not messages.has_warning
        assert messages.total_issue_count == 0

    @pytest.mark.usefixtures("_media_settings")
    def test_no_issues_logs_clean(self, caplog: pytest.LogCaptureFixture) -> None:
        messages = check_sanity()
        with caplog.at_level(logging.INFO, logger="paperless.sanity_checker"):
            messages.log_messages()
        assert "Sanity checker detected no issues." in caplog.text


@pytest.mark.django_db
class TestCheckSanityHealthyDocument:
    def test_no_errors(self, sample_doc: Document) -> None:
        messages = check_sanity()
        assert not messages.has_error
        assert not messages.has_warning
        assert messages.total_issue_count == 0


@pytest.mark.django_db
class TestCheckSanityThumbnail:
    def test_missing(self, sample_doc: Document) -> None:
        Path(sample_doc.thumbnail_path).unlink()
        messages = check_sanity()
        assert messages.has_error
        assert any(
            "Thumbnail of document does not exist" in m["message"]
            for m in messages[sample_doc.pk]
        )

    def test_unreadable(self, sample_doc: Document) -> None:
        thumb = Path(sample_doc.thumbnail_path)
        thumb.chmod(0o000)
        try:
            messages = check_sanity()
            assert messages.has_error
            assert any(
                "Cannot read thumbnail" in m["message"] for m in messages[sample_doc.pk]
            )
        finally:
            thumb.chmod(0o644)


@pytest.mark.django_db
class TestCheckSanityOriginal:
    def test_missing(self, sample_doc: Document) -> None:
        Path(sample_doc.source_path).unlink()
        messages = check_sanity()
        assert messages.has_error
        assert any(
            "Original of document does not exist" in m["message"]
            for m in messages[sample_doc.pk]
        )

    def test_checksum_mismatch(self, sample_doc: Document) -> None:
        sample_doc.checksum = "badhash"
        sample_doc.save()
        messages = check_sanity()
        assert messages.has_error
        assert any(
            "Checksum mismatch" in m["message"] and "badhash" in m["message"]
            for m in messages[sample_doc.pk]
        )

    def test_unreadable(self, sample_doc: Document) -> None:
        src = Path(sample_doc.source_path)
        src.chmod(0o000)
        try:
            messages = check_sanity()
            assert messages.has_error
            assert any(
                "Cannot read original" in m["message"] for m in messages[sample_doc.pk]
            )
        finally:
            src.chmod(0o644)


@pytest.mark.django_db
class TestCheckSanityArchive:
    def test_checksum_without_filename(self, sample_doc: Document) -> None:
        sample_doc.archive_filename = None
        sample_doc.save()
        messages = check_sanity()
        assert messages.has_error
        assert any(
            "checksum, but no archive filename" in m["message"]
            for m in messages[sample_doc.pk]
        )

    def test_filename_without_checksum(self, sample_doc: Document) -> None:
        sample_doc.archive_checksum = None
        sample_doc.save()
        messages = check_sanity()
        assert messages.has_error
        assert any(
            "checksum is missing" in m["message"] for m in messages[sample_doc.pk]
        )

    def test_missing_file(self, sample_doc: Document) -> None:
        Path(sample_doc.archive_path).unlink()
        messages = check_sanity()
        assert messages.has_error
        assert any(
            "Archived version of document does not exist" in m["message"]
            for m in messages[sample_doc.pk]
        )

    def test_checksum_mismatch(self, sample_doc: Document) -> None:
        sample_doc.archive_checksum = "wronghash"
        sample_doc.save()
        messages = check_sanity()
        assert messages.has_error
        assert any(
            "Checksum mismatch of archived document" in m["message"]
            for m in messages[sample_doc.pk]
        )

    def test_unreadable(self, sample_doc: Document) -> None:
        archive = Path(sample_doc.archive_path)
        archive.chmod(0o000)
        try:
            messages = check_sanity()
            assert messages.has_error
            assert any(
                "Cannot read archive" in m["message"] for m in messages[sample_doc.pk]
            )
        finally:
            archive.chmod(0o644)

    def test_no_archive_at_all(self, sample_doc: Document) -> None:
        """Document with neither archive checksum nor filename is valid."""
        Path(sample_doc.archive_path).unlink()
        sample_doc.archive_checksum = None
        sample_doc.archive_filename = None
        sample_doc.save()
        messages = check_sanity()
        assert not messages.has_error


@pytest.mark.django_db
class TestCheckSanityContent:
    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="empty-string"),
        ],
    )
    def test_no_content(self, sample_doc: Document, content: str) -> None:
        sample_doc.content = content
        sample_doc.save()
        messages = check_sanity()
        assert not messages.has_error
        assert not messages.has_warning
        assert any("no OCR data" in m["message"] for m in messages[sample_doc.pk])


@pytest.mark.django_db
class TestCheckSanityOrphans:
    def test_orphaned_file(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        (paperless_dirs.originals / "orphan.pdf").touch()
        messages = check_sanity()
        assert messages.has_warning
        assert any("Orphaned file" in m["message"] for m in messages[None])

    @pytest.mark.usefixtures("_media_settings")
    def test_ignorable_files_not_flagged(
        self,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        (paperless_dirs.media / ".DS_Store").touch()
        (paperless_dirs.media / "desktop.ini").touch()
        messages = check_sanity()
        assert not messages.has_warning

    @pytest.mark.usefixtures("_media_settings")
    def test_share_link_bundle_not_flagged(
        self,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        """Files in SHARE_LINK_BUNDLE_DIR must not be flagged as orphans."""
        bundle_file = paperless_dirs.share_link_bundles / "my-bundle.zip"
        bundle_file.write_bytes(b"fake zip content")
        messages = check_sanity()
        assert not messages.has_warning
        assert not any(
            "my-bundle.zip" in m["message"]
            for pk in messages.document_pks()
            for m in messages[pk]
        )

    def test_orphan_message_includes_category(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        (paperless_dirs.originals / "orphan.pdf").touch()
        messages = check_sanity()
        assert any(
            "originals" in m["message"] for m in messages[None]
        )

    def test_orphan_summary_attached(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        (paperless_dirs.originals / "orphan.pdf").write_bytes(b"x" * 100)
        messages = check_sanity()
        orphan_summary: OrphanSummary = messages.orphan_summary
        assert orphan_summary is not None
        assert orphan_summary.total_count == 1
        assert orphan_summary.total_size == 100
        assert "originals" in orphan_summary.by_category


@pytest.mark.django_db
class TestCheckSanityIterWrapper:
    def test_wrapper_receives_documents(self, sample_doc: Document) -> None:
        seen: list[Document] = []

        def tracking(iterable: Iterable[Document]) -> Iterable[Document]:
            for item in iterable:
                seen.append(item)
                yield item

        check_sanity(iter_wrapper=tracking)
        assert len(seen) == 1
        assert seen[0].pk == sample_doc.pk

    def test_default_works_without_wrapper(self, sample_doc: Document) -> None:
        messages = check_sanity()
        assert not messages.has_error


@pytest.mark.django_db
class TestCheckSanityLogMessages:
    def test_logs_doc_issues(
        self,
        sample_doc: Document,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        Path(sample_doc.source_path).unlink()
        messages = check_sanity()
        with caplog.at_level(logging.INFO, logger="paperless.sanity_checker"):
            messages.log_messages()
        assert f"document #{sample_doc.pk}" in caplog.text
        assert "Original of document does not exist" in caplog.text

    def test_logs_global_issues(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (paperless_dirs.originals / "orphan.pdf").touch()
        messages = check_sanity()
        with caplog.at_level(logging.WARNING, logger="paperless.sanity_checker"):
            messages.log_messages()
        assert "Orphaned file" in caplog.text

    @pytest.mark.usefixtures("_media_settings")
    def test_logs_unknown_doc_pk(self, caplog: pytest.LogCaptureFixture) -> None:
        """A doc PK not in the DB logs 'Unknown' as the title."""
        messages = check_sanity()
        messages.error(99999, "Ghost document")
        with caplog.at_level(logging.INFO, logger="paperless.sanity_checker"):
            messages.log_messages()
        assert "#99999" in caplog.text
        assert "Unknown" in caplog.text


# ---------------------------------------------------------------------------
# Orphan analysis unit tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestClassifyOrphan:
    @pytest.mark.usefixtures("_media_settings")
    def test_classify_originals(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.originals / "stray.pdf"
        f.touch()
        assert _classify_orphan(f) == "originals"

    @pytest.mark.usefixtures("_media_settings")
    def test_classify_archive(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.archive / "stray.pdf"
        f.touch()
        assert _classify_orphan(f) == "archive"

    @pytest.mark.usefixtures("_media_settings")
    def test_classify_thumbnails(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.thumbnails / "stray.webp"
        f.touch()
        assert _classify_orphan(f) == "thumbnails"

    @pytest.mark.usefixtures("_media_settings")
    def test_classify_other(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.media / "random_file.txt"
        f.touch()
        assert _classify_orphan(f) == "other"


@pytest.mark.usefixtures("_media_settings")
class TestBuildOrphanSummary:
    def test_empty_set(self) -> None:
        summary = _build_orphan_summary(set())
        assert summary.total_count == 0
        assert summary.total_size == 0
        assert not summary.has_orphans

    def test_single_file(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.originals / "test.pdf"
        f.write_bytes(b"x" * 500)

        summary = _build_orphan_summary({f})
        assert summary.total_count == 1
        assert summary.total_size == 500
        assert summary.has_orphans
        assert "originals" in summary.by_category

    def test_multiple_categories(self, paperless_dirs: PaperlessDirs) -> None:
        f1 = paperless_dirs.originals / "orphan1.pdf"
        f2 = paperless_dirs.archive / "orphan2.pdf"
        f1.write_bytes(b"a" * 100)
        f2.write_bytes(b"b" * 200)

        summary = _build_orphan_summary({f1, f2})
        assert summary.total_count == 2
        assert summary.total_size == 300
        assert "originals" in summary.by_category
        assert "archive" in summary.by_category


class TestFormatSize:
    def test_bytes(self) -> None:
        assert _format_size(500) == "500 B"

    def test_kilobytes(self) -> None:
        assert _format_size(2048) == "2.0 KB"

    def test_megabytes(self) -> None:
        assert _format_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self) -> None:
        assert _format_size(3 * 1024 * 1024 * 1024) == "3.0 GB"


# ---------------------------------------------------------------------------
# Orphan verification tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_media_settings")
class TestVerifyOrphans:
    def test_empty_candidates(self, paperless_dirs: PaperlessDirs) -> None:
        result = _verify_orphans(set(), scan_start=0.0, lock_held=True)
        assert result == set()

    def test_nonexistent_file_filtered(self, paperless_dirs: PaperlessDirs) -> None:
        nonexistent = paperless_dirs.originals / "gone.pdf"
        result = _verify_orphans(
            {nonexistent}, scan_start=0.0, lock_held=True
        )
        assert nonexistent not in result

    def test_old_file_confirmed(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.originals / "old.pdf"
        f.write_bytes(b"data")
        # scan_start is in the future → file mtime is before scan start
        result = _verify_orphans(
            {f}, scan_start=time.time() + 100, lock_held=True
        )
        assert f in result

    def test_recent_file_skipped_when_lock_held(
        self, paperless_dirs: PaperlessDirs
    ) -> None:
        f = paperless_dirs.originals / "new.pdf"
        f.write_bytes(b"data")
        # scan_start is in the past → file mtime is after scan start
        result = _verify_orphans(
            {f}, scan_start=time.time() - 100, lock_held=True
        )
        assert f not in result


# ---------------------------------------------------------------------------
# Orphan cleanup tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_media_settings")
class TestHandleOrphanCleanup:
    def test_no_orphans(self) -> None:
        summary = OrphanSummary()
        result = handle_orphan_cleanup(summary)
        assert not result.cleaned_up

    def test_removes_files(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.originals / "orphan.pdf"
        f.write_bytes(b"x" * 100)

        summary = OrphanSummary(
            orphans=[OrphanFileInfo(path=f, category="originals", size=100)],
            total_count=1,
            total_size=100,
        )
        result = handle_orphan_cleanup(summary)
        assert result.cleaned_up
        assert result.freed_bytes == 100
        assert not f.exists()

    def test_skips_missing_files(self, paperless_dirs: PaperlessDirs) -> None:
        f = paperless_dirs.originals / "already_gone.pdf"
        # File doesn't exist

        summary = OrphanSummary(
            orphans=[OrphanFileInfo(path=f, category="originals", size=50)],
            total_count=1,
            total_size=50,
        )
        result = handle_orphan_cleanup(summary)
        assert result.cleaned_up
        assert result.freed_bytes == 0


# ---------------------------------------------------------------------------
# Progress callback tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCheckSanityProgressCallback:
    def test_phases_emitted(self, sample_doc: Document) -> None:
        phases: list[str] = []
        check_sanity(progress_callback=phases.append)

        assert "scan_start" in phases
        assert "scan_complete" in phases
        assert "documents_start" in phases
        assert "documents_complete" in phases
        assert "orphans_start" in phases
        assert "orphans_complete" in phases

    def test_cleanup_phases_only_when_deleting(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        (paperless_dirs.originals / "orphan.pdf").touch()

        phases_no_delete: list[str] = []
        check_sanity(progress_callback=phases_no_delete.append)
        assert "cleanup_start" not in phases_no_delete

        phases_with_delete: list[str] = []
        check_sanity(
            progress_callback=phases_with_delete.append,
            delete_orphans=True,
        )
        assert "cleanup_start" in phases_with_delete
        assert "cleanup_complete" in phases_with_delete

    def test_no_callback_is_safe(self, sample_doc: Document) -> None:
        """Calling without a progress_callback must not raise."""
        messages = check_sanity()
        assert not messages.has_error


# ---------------------------------------------------------------------------
# Delete orphans integration tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCheckSanityDeleteOrphans:
    def test_delete_removes_orphans(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        orphan_path = paperless_dirs.originals / "orphan.pdf"
        orphan_path.write_bytes(b"orphan content")

        messages = check_sanity(delete_orphans=True)
        orphan_summary: OrphanSummary = messages.orphan_summary

        assert orphan_summary.cleaned_up
        assert orphan_summary.freed_bytes > 0
        assert not orphan_path.exists()

    def test_no_delete_by_default(
        self,
        sample_doc: Document,
        paperless_dirs: PaperlessDirs,
    ) -> None:
        orphan_path = paperless_dirs.originals / "orphan.pdf"
        orphan_path.write_bytes(b"orphan content")

        messages = check_sanity()
        orphan_summary: OrphanSummary = messages.orphan_summary

        assert not orphan_summary.cleaned_up
        assert orphan_path.exists()

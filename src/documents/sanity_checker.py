"""
Sanity checker for the Paperless-ngx document archive.

Verifies that all documents have valid files, correct checksums,
and consistent metadata. Reports and optionally cleans up orphaned
files in the media directory.

Progress display is the caller's responsibility:

* Pass an ``iter_wrapper`` to wrap the document queryset (e.g., with
  a progress bar).  The default is an identity function that adds no
  overhead.
* Pass a ``progress_callback`` to receive coarse-grained phase
  notifications (e.g., ``"scan_start"``, ``"documents_complete"``).

Concurrency protection
----------------------
The checker acquires :data:`settings.MEDIA_LOCK` while scanning the
media directory to avoid false-positive orphan reports caused by
concurrent ``consume_file`` writes.  Orphan candidates are re-verified
after document processing using an mtime-based filter under a second
lock acquisition.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Final
from typing import TypedDict

from django.conf import settings
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from documents.models import Document
from documents.utils import IterWrapper
from documents.utils import compute_checksum
from documents.utils import identity
from paperless.config import GeneralConfig

logger = logging.getLogger("paperless.sanity_checker")

# Type alias for progress phase callbacks.
# The callback receives a short phase identifier string.
ProgressCallback = Callable[[str], None]

# Default timeout (seconds) when waiting for the media lock.
_LOCK_TIMEOUT_SECONDS: Final[float] = 30.0
_MAX_LOCK_ATTEMPTS: Final[int] = 2


# ---------------------------------------------------------------------------
# Orphan file analysis data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrphanFileInfo:
    """Metadata about a single orphaned file found in the media directory."""

    path: Path
    category: str  # "originals", "archive", "thumbnails", or "other"
    size: int


@dataclass
class OrphanSummary:
    """Aggregated analysis of all orphaned files detected during a check."""

    orphans: list[OrphanFileInfo] = field(default_factory=list)
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    total_count: int = 0
    total_size: int = 0
    cleaned_up: bool = False
    freed_bytes: int = 0

    @property
    def has_orphans(self) -> bool:
        return self.total_count > 0


# ---------------------------------------------------------------------------
# Internal helpers — filesystem scanning
# ---------------------------------------------------------------------------


def _build_present_files(lock: FileLock | None = None) -> set[Path]:
    """Collect all files in MEDIA_ROOT, excluding directories and ignorable files.

    When *lock* is provided and is currently held, the scan runs under
    the lock to avoid race conditions with concurrent file writes (e.g.,
    from ``consume_file``).

    Excludes:
    - Directories
    - Filenames listed in ``settings.IGNORABLE_FILES``
    - The ``MEDIA_LOCK`` file itself
    - The custom ``app_logo`` file
    - Files under ``SHARE_LINK_BUNDLE_DIR``
    """
    if lock is not None and lock.is_locked:
        with lock:
            return _scan_media_root()
    return _scan_media_root()


def _scan_media_root() -> set[Path]:
    """Perform the actual glob of MEDIA_ROOT, applying all exclusion filters."""
    present_files = {
        x.resolve()
        for x in Path(settings.MEDIA_ROOT).glob("**/*")
        if not x.is_dir() and x.name not in settings.IGNORABLE_FILES
    }

    # Exclude the lock file itself
    lockfile = Path(settings.MEDIA_LOCK).resolve()
    present_files.discard(lockfile)

    # Exclude share link bundle files
    bundle_dir = Path(settings.SHARE_LINK_BUNDLE_DIR).resolve()
    present_files = {f for f in present_files if not f.is_relative_to(bundle_dir)}

    # Exclude custom app logo
    general_config = GeneralConfig()
    app_logo = general_config.app_logo or settings.APP_LOGO
    if app_logo:
        logo_file = Path(settings.MEDIA_ROOT / Path(app_logo.lstrip("/"))).resolve()
        present_files.discard(logo_file)

    return present_files


# ---------------------------------------------------------------------------
# Internal helpers — orphan analysis
# ---------------------------------------------------------------------------


def _classify_orphan(file_path: Path) -> str:
    """Determine which media sub-directory an orphan file belongs to."""
    resolved = file_path.resolve()
    originals = Path(settings.ORIGINALS_DIR).resolve()
    archive = Path(settings.ARCHIVE_DIR).resolve()
    thumbnails = Path(settings.THUMBNAIL_DIR).resolve()

    if resolved.is_relative_to(originals):
        return "originals"
    if resolved.is_relative_to(archive):
        return "archive"
    if resolved.is_relative_to(thumbnails):
        return "thumbnails"
    return "other"


def _build_orphan_summary(orphan_files: set[Path]) -> OrphanSummary:
    """Build a categorized summary of orphaned files with sizes."""
    orphans: list[OrphanFileInfo] = []
    by_category: dict[str, dict[str, int]] = defaultdict(
        lambda: {"count": 0, "size": 0},
    )

    for file_path in orphan_files:
        resolved = file_path.resolve()
        category = _classify_orphan(resolved)
        try:
            size = resolved.stat().st_size
        except OSError:
            size = 0

        info = OrphanFileInfo(path=resolved, category=category, size=size)
        orphans.append(info)
        by_category[category]["count"] += 1
        by_category[category]["size"] += size

    total_count = len(orphans)
    total_size = sum(o.size for o in orphans)

    if total_count > 0:
        logger.info(
            "Found %d orphaned file(s) totaling %s",
            total_count,
            _format_size(total_size),
        )

    return OrphanSummary(
        orphans=orphans,
        by_category=dict(by_category),
        total_count=total_count,
        total_size=total_size,
    )


def _verify_orphans(
    orphan_candidates: set[Path],
    scan_start: float,
    lock_held: bool,
) -> set[Path]:
    """Re-verify orphan candidates to filter false positives.

    Acquires MEDIA_LOCK and re-checks each candidate:
    - File must still exist on disk
    - If the lock is held, the file's mtime must predate the scan start
      (files created by ``consume_file`` after the scan began are skipped)

    This protects against race conditions where ``consume_file`` writes
    a new file to MEDIA_ROOT between the initial scan and orphan reporting.
    """
    if not orphan_candidates:
        return set()

    lock_path = Path(settings.MEDIA_LOCK)
    lock = FileLock(lock_path)

    confirmed: set[Path] = set()

    try:
        lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS)
        lock_acquired = True
    except FileLockTimeout:
        logger.warning(
            "Could not acquire media lock for orphan re-verification; "
            "falling back to existence-only check.",
        )
        lock_acquired = False

    try:
        for candidate in orphan_candidates:
            if not candidate.is_file():
                continue
            if lock_acquired and lock_held:
                try:
                    mtime = candidate.stat().st_mtime
                    if mtime >= scan_start:
                        logger.debug(
                            "Skipping recently modified file during orphan "
                            "re-verification: %s (mtime=%.2f, scan_start=%.2f)",
                            candidate,
                            mtime,
                            scan_start,
                        )
                        continue
                except OSError:
                    continue
            confirmed.add(candidate)
    finally:
        if lock_acquired:
            lock.release()

    return confirmed


# ---------------------------------------------------------------------------
# Orphan cleanup
# ---------------------------------------------------------------------------


def handle_orphan_cleanup(orphan_summary: OrphanSummary) -> OrphanSummary:
    """Safely remove orphaned files from the media directory.

    Acquires MEDIA_LOCK before deleting to prevent conflicts with
    concurrent ``consume_file`` operations.  Files that no longer exist
    or cannot be removed are silently skipped (with a warning log).

    Returns the updated *orphan_summary* with ``cleaned_up=True`` and
    ``freed_bytes`` reflecting the actual bytes reclaimed.
    """
    if not orphan_summary.has_orphans:
        return orphan_summary

    lock = FileLock(Path(settings.MEDIA_LOCK))
    freed_bytes = 0
    removed_count = 0

    try:
        lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS)
    except FileLockTimeout:
        logger.warning(
            "Could not acquire media lock for orphan cleanup. "
            "Skipping cleanup to avoid conflicts with ongoing operations.",
        )
        return orphan_summary

    try:
        for orphan in orphan_summary.orphans:
            try:
                if orphan.path.is_file():
                    orphan.path.unlink()
                    freed_bytes += orphan.size
                    removed_count += 1
                    logger.debug("Removed orphaned file: %s", orphan.path)
            except OSError as e:
                logger.warning(
                    "Failed to remove orphaned file %s: %s",
                    orphan.path,
                    e,
                )
    finally:
        lock.release()

    logger.info(
        "Cleaned up %d orphaned file(s), freed %s.",
        removed_count,
        _format_size(freed_bytes),
    )

    orphan_summary.cleaned_up = True
    orphan_summary.freed_bytes = freed_bytes
    return orphan_summary


# ---------------------------------------------------------------------------
# Lock acquisition helper
# ---------------------------------------------------------------------------


@contextmanager
def _acquire_sanity_check_lock(
    lock_path: Path,
    timeout: float = _LOCK_TIMEOUT_SECONDS,
    max_attempts: int = _MAX_LOCK_ATTEMPTS,
):
    """Context manager that acquires MEDIA_LOCK for the sanity checker.

    Attempts up to *max_attempts* times with *timeout* seconds each.
    If the lock cannot be acquired after all attempts, yields ``None``
    and logs a warning — the checker proceeds without protection,
    relying on orphan re-verification to mitigate false positives.
    """
    lock = FileLock(lock_path)
    acquired = False

    for attempt in range(max_attempts):
        try:
            lock.acquire(timeout=timeout)
            acquired = True
            break
        except FileLockTimeout:
            if attempt < max_attempts - 1:
                logger.warning(
                    "Media lock busy (attempt %d/%d), retrying...",
                    attempt + 1,
                    max_attempts,
                )
            else:
                logger.warning(
                    "Could not acquire media lock after %d attempts. "
                    "Proceeding without lock protection; orphan results "
                    "may include false positives from concurrent operations.",
                    max_attempts,
                )

    try:
        yield lock if acquired else None
    finally:
        if acquired:
            lock.release()


# ---------------------------------------------------------------------------
# Internal helpers — formatting
# ---------------------------------------------------------------------------


def _format_size(size_bytes: int) -> str:
    """Return a human-readable file size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


# ---------------------------------------------------------------------------
# Internal helpers — message collection
# ---------------------------------------------------------------------------


class MessageEntry(TypedDict):
    """A single sanity check message with its severity level."""

    level: int
    message: str


class SanityCheckMessages:
    """Collects sanity check messages grouped by document primary key.

    Messages are categorized as error, warning, or info. ``None`` is used
    as the key for messages not associated with a specific document
    (e.g., orphaned files).
    """

    def __init__(self) -> None:
        self._messages: dict[int | None, list[MessageEntry]] = defaultdict(list)
        self.has_error: bool = False
        self.has_warning: bool = False
        self.has_info: bool = False
        self.document_count: int = 0
        self.document_error_count: int = 0
        self.document_warning_count: int = 0
        self.document_info_count: int = 0
        self.global_warning_count: int = 0

    # -- Recording ----------------------------------------------------------

    def error(self, doc_pk: int | None, message: str) -> None:
        self._messages[doc_pk].append({"level": logging.ERROR, "message": message})
        self.has_error = True
        if doc_pk is not None:
            self.document_count += 1
            self.document_error_count += 1

    def warning(self, doc_pk: int | None, message: str) -> None:
        self._messages[doc_pk].append({"level": logging.WARNING, "message": message})
        self.has_warning = True

        if doc_pk is not None:
            self.document_count += 1
            self.document_warning_count += 1
        else:
            # This is the only type of global message we do right now
            self.global_warning_count += 1

    def info(self, doc_pk: int | None, message: str) -> None:
        self._messages[doc_pk].append({"level": logging.INFO, "message": message})
        self.has_info = True

        if doc_pk is not None:
            self.document_count += 1
            self.document_info_count += 1

    # -- Iteration / query --------------------------------------------------

    def document_pks(self) -> list[int | None]:
        """Return all document PKs (including None for global messages)."""
        return list(self._messages.keys())

    def iter_messages(self) -> Iterator[tuple[int | None, list[MessageEntry]]]:
        """Iterate over (doc_pk, messages) pairs."""
        yield from self._messages.items()

    def __getitem__(self, item: int | None) -> list[MessageEntry]:
        return self._messages[item]

    # -- Summarize Helpers --------------------------------------------------

    @property
    def has_global_issues(self) -> bool:
        return None in self._messages

    @property
    def total_issue_count(self) -> int:
        """Total number of error and warning messages across all documents and global."""
        return (
            self.document_error_count
            + self.document_warning_count
            + self.global_warning_count
        )

    # -- Logging output (used by Celery task path) --------------------------

    def log_messages(self) -> None:
        """Write all messages to the ``paperless.sanity_checker`` logger.

        This is the output path for headless / Celery execution.
        Management commands use Rich rendering instead.
        """
        if len(self._messages) == 0:
            logger.info("Sanity checker detected no issues.")
            return

        doc_pks = [pk for pk in self._messages if pk is not None]
        titles: dict[int, str] = {}
        if doc_pks:
            titles = dict(
                Document.global_objects.filter(pk__in=doc_pks)
                .only("pk", "title")
                .values_list("pk", "title"),
            )

        for doc_pk, entries in self._messages.items():
            if doc_pk is not None:
                title = titles.get(doc_pk, "Unknown")
                logger.info(
                    "Detected following issue(s) with document #%s, titled %s",
                    doc_pk,
                    title,
                )
            for msg in entries:
                logger.log(msg["level"], msg["message"])


class SanityCheckFailedException(Exception):
    pass


# ---------------------------------------------------------------------------
# Internal helpers — per-document checks
# ---------------------------------------------------------------------------


def _check_thumbnail(
    doc: Document,
    messages: SanityCheckMessages,
    present_files: set[Path],
) -> None:
    """Verify the thumbnail exists and is readable."""
    # doc.thumbnail_path already returns a resolved Path; no need to re-resolve.
    thumbnail_path: Final[Path] = doc.thumbnail_path
    if not thumbnail_path.is_file():
        messages.error(doc.pk, "Thumbnail of document does not exist.")
        return

    present_files.discard(thumbnail_path)
    try:
        _ = thumbnail_path.read_bytes()
    except OSError as e:
        messages.error(doc.pk, f"Cannot read thumbnail file of document: {e}")


def _check_original(
    doc: Document,
    messages: SanityCheckMessages,
    present_files: set[Path],
) -> None:
    """Verify the original file exists, is readable, and has matching checksum."""
    # doc.source_path already returns a resolved Path; no need to re-resolve.
    source_path: Final[Path] = doc.source_path
    if not source_path.is_file():
        messages.error(doc.pk, "Original of document does not exist.")
        return

    present_files.discard(source_path)
    try:
        checksum = compute_checksum(source_path)
    except OSError as e:
        messages.error(doc.pk, f"Cannot read original file of document: {e}")
    else:
        if checksum != doc.checksum:
            messages.error(
                doc.pk,
                f"Checksum mismatch. Stored: {doc.checksum}, actual: {checksum}.",
            )


def _check_archive(
    doc: Document,
    messages: SanityCheckMessages,
    present_files: set[Path],
) -> None:
    """Verify archive file consistency: checksum/filename pairing and file integrity."""
    if doc.archive_checksum is not None and doc.archive_filename is None:
        messages.error(
            doc.pk,
            "Document has an archive file checksum, but no archive filename.",
        )
    elif doc.archive_checksum is None and doc.archive_filename is not None:
        messages.error(
            doc.pk,
            "Document has an archive file, but its checksum is missing.",
        )
    elif doc.has_archive_version:
        if TYPE_CHECKING:
            assert isinstance(doc.archive_path, Path)
        # doc.archive_path already returns a resolved Path; no need to re-resolve.
        archive_path: Final[Path] = doc.archive_path  # type: ignore[assignment]
        if not archive_path.is_file():
            messages.error(doc.pk, "Archived version of document does not exist.")
            return

        present_files.discard(archive_path)
        try:
            checksum = compute_checksum(archive_path)
        except OSError as e:
            messages.error(
                doc.pk,
                f"Cannot read archive file of document: {e}",
            )
        else:
            if checksum != doc.archive_checksum:
                messages.error(
                    doc.pk,
                    "Checksum mismatch of archived document. "
                    f"Stored: {doc.archive_checksum}, actual: {checksum}.",
                )


def _check_content(doc: Document, messages: SanityCheckMessages) -> None:
    """Flag documents with no OCR content."""
    if not doc.content:
        messages.info(doc.pk, "Document contains no OCR data")


def _check_document(
    doc: Document,
    messages: SanityCheckMessages,
    present_files: set[Path],
) -> None:
    """Run all checks for a single document."""
    _check_thumbnail(doc, messages, present_files)
    _check_original(doc, messages, present_files)
    _check_archive(doc, messages, present_files)
    _check_content(doc, messages)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_sanity(
    *,
    iter_wrapper: IterWrapper[Document] = identity,
    progress_callback: ProgressCallback | None = None,
    delete_orphans: bool = False,
) -> SanityCheckMessages:
    """Run a full sanity check on the document archive.

    Acquires the media lock while scanning the filesystem to prevent
    false-positive orphan reports from concurrent write operations.
    Orphan candidates are re-verified after document processing.

    Args:
        iter_wrapper: A callable that wraps the document iterable, e.g.,
            for progress bar display. Defaults to identity (no wrapping).
        progress_callback: Optional callback receiving a phase string
            (e.g., ``"scan_start"``, ``"documents_complete"``) for
            coarse-grained progress reporting.
        delete_orphans: If ``True``, remove detected orphan files after
            analysis (with lock protection).

    Returns:
        A SanityCheckMessages instance containing all detected issues.
        The ``orphan_summary`` attribute holds the orphan analysis.
    """
    scan_start = time.monotonic()

    def _notify(phase: str) -> None:
        if progress_callback:
            progress_callback(phase)

    messages = SanityCheckMessages()

    # Acquire media lock and scan filesystem
    lock_path = Path(settings.MEDIA_LOCK)
    with _acquire_sanity_check_lock(lock_path) as lock:
        _notify("scan_start")
        present_files = _build_present_files(lock)
        _notify("scan_complete")

    # Check each document (lock released — consumption can proceed)
    _notify("documents_start")
    documents = Document.global_objects.only(
        "pk",
        "filename",
        "mime_type",
        "checksum",
        "archive_checksum",
        "archive_filename",
        "content",
    ).iterator(chunk_size=500)
    for doc in iter_wrapper(documents):
        _check_document(doc, messages, present_files)
    _notify("documents_complete")

    # Orphan analysis with re-verification
    _notify("orphans_start")
    confirmed_orphans = _verify_orphans(
        present_files,
        scan_start=scan_start,
        lock_held=True,
    )
    orphan_summary = _build_orphan_summary(confirmed_orphans)
    messages.orphan_summary = orphan_summary

    # Report orphans as warnings
    for orphan in orphan_summary.orphans:
        try:
            rel_path = orphan.path.relative_to(settings.MEDIA_ROOT)
        except ValueError:
            rel_path = orphan.path
        messages.warning(
            None,
            f"Orphaned file in media dir: {rel_path} "
            f"({orphan.category}, {_format_size(orphan.size)})",
        )
    _notify("orphans_complete")

    # Optional cleanup
    if delete_orphans and orphan_summary.has_orphans:
        _notify("cleanup_start")
        handle_orphan_cleanup(orphan_summary)
        _notify("cleanup_complete")

    return messages

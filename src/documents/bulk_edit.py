from __future__ import annotations

import logging
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Literal
from typing import NamedTuple

from celery import chord
from celery import group
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import DocumentSource
from documents.models import BulkEditJob
from documents.models import BulkEditJobItem
from documents.models import Correspondent
from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import DocumentType
from documents.models import PaperlessTask
from documents.models import StoragePath
from documents.models import Tag
from documents.permissions import set_permissions_for_object
from documents.plugins.helpers import DocumentsStatusManager
from documents.tasks import bulk_update_documents
from documents.tasks import consume_file
from documents.tasks import update_document_content_maybe_archive_file
from documents.versioning import get_latest_version_for_root
from documents.versioning import get_root_document

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.contrib.auth.models import User

logger: logging.Logger = logging.getLogger("paperless.bulk_edit")

SourceMode = Literal["latest_version", "explicit_selection"]


class SourceModeChoices:
    LATEST_VERSION: SourceMode = "latest_version"
    EXPLICIT_SELECTION: SourceMode = "explicit_selection"


class ResolvedDocPair(NamedTuple):
    root_doc: Document
    source_doc: Document


class ConflictError(Exception):
    """
    Raised when one or more documents have been modified since the caller
    last read them, indicating a potential concurrent-edit conflict.

    Attributes:
        conflicts: mapping of document_id -> {
            "expected_modified": the ISO timestamp the caller supplied,
            "actual_modified": the current modified timestamp in the database,
        }
    """

    def __init__(self, conflicts: dict[int, dict[str, str]]) -> None:
        self.conflicts = conflicts
        ids = list(conflicts.keys())
        super().__init__(
            f"Concurrent modification detected for document(s) {ids}. "
            "Reload and retry.",
        )


def check_document_conflicts(
    doc_ids: list[int],
    expected_modified: dict[int, str],
) -> dict[int, dict[str, str]]:
    """
    Compare each document's current ``modified`` timestamp against the
    caller-supplied *expected_modified* mapping.

    Parameters
    ----------
    doc_ids:
        The document IDs that will be mutated.
    expected_modified:
        Mapping of ``{doc_id: ISO-8601 timestamp}`` representing the last
        known modification time as seen by the caller.

    Returns
    -------
    dict
        Mapping of conflicting ``doc_id`` ->
        ``{"expected_modified": ..., "actual_modified": ...}``.
        An empty dict means no conflicts.
    """
    if not expected_modified:
        return {}

    docs = Document.objects.filter(id__in=doc_ids).values("id", "modified")
    conflicts: dict[int, dict[str, str]] = {}

    for doc in docs:
        doc_id = doc["id"]
        if doc_id not in expected_modified:
            continue
        expected = expected_modified[doc_id]
        # Normalise both sides to comparable strings
        actual = doc["modified"]
        if isinstance(actual, datetime):
            actual_str = actual.isoformat()
        else:
            actual_str = str(actual)
        expected_str = (
            expected.isoformat() if isinstance(expected, datetime) else str(expected)
        )
        if actual_str != expected_str:
            conflicts[doc_id] = {
                "expected_modified": expected_str,
                "actual_modified": actual_str,
            }

    return conflicts


def _update_job_item(
    job: BulkEditJob | None,
    doc_id: int,
    status: str,
    error_message: str = "",
) -> None:
    """Helper to update a single BulkEditJobItem without raising."""
    if job is None:
        return
    try:
        BulkEditJobItem.objects.filter(job=job, document_id=doc_id).update(
            status=status,
            error_message=error_message,
            date_done=timezone.now(),
        )
    except Exception:
        logger.exception(
            f"Failed to update job item for job={job.pk} doc={doc_id}",
        )


def _finalize_job(job: BulkEditJob | None) -> None:
    """Mark the job as complete/failed and set date_done."""
    if job is None:
        return
    try:
        job.refresh_from_db()
        if job.failed_documents > 0:
            job.status = BulkEditJob.Status.FAILED
        else:
            job.status = BulkEditJob.Status.COMPLETE
        job.date_done = timezone.now()
        job.save(
            update_fields=["status", "date_done", "completed_documents", "failed_documents"],
        )
    except Exception:
        logger.exception(f"Failed to finalize job {job.pk}")


@shared_task(bind=True)
def run_bulk_edit(
    self,
    method_name: str,
    doc_ids: list[int],
    parameters: dict[str, Any],
    *,
    use_transaction: bool = False,
    expected_modified: dict[int, str] | None = None,
    job_id: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """
    Celery task that orchestrates a bulk edit operation with optional:
    - **Transaction mode**: wraps the entire operation in ``transaction.atomic()``
      so any failure rolls back all changes.
    - **Progress tracking**: creates/updates ``BulkEditJob`` and
      ``BulkEditJobItem`` rows so callers can poll for fine-grained progress.
    - **Conflict detection**: compares each document's ``modified`` timestamp
      against *expected_modified* before applying changes; raises
      ``ConflictError`` if any document has been concurrently modified.

    Parameters
    ----------
    method_name:
        Name of the bulk_edit function to call (e.g. ``"set_correspondent"``).
    doc_ids:
        List of document IDs to operate on.
    parameters:
        Keyword arguments forwarded to the bulk_edit function.
    use_transaction:
        If ``True``, the entire batch runs inside a single database
        transaction.  A failure on any document rolls back all changes.
    expected_modified:
        Optional mapping of ``{doc_id: ISO-8601 timestamp}`` for optimistic
        locking.  If any document's current ``modified`` differs from the
        supplied value, the operation aborts with ``ConflictError``.
    job_id:
        Optional ``BulkEditJob`` primary key.  If supplied, per-document
        progress is written to ``BulkEditJobItem`` rows.
    user_id:
        Optional user ID to set as the job owner.

    Returns
    -------
    dict
        ``{"result": "OK", "job_id": <int|None>}`` on success.

    Raises
    ------
    ConflictError
        If conflict detection finds concurrently modified documents.
    """
    from documents import bulk_edit as _be

    # Resolve the target function
    method_fn: Callable[..., Literal["OK"]] = getattr(_be, method_name, None)
    if method_fn is None:
        raise ValueError(f"Unknown bulk_edit method: {method_name}")

    job: BulkEditJob | None = None
    if job_id is not None:
        try:
            job = BulkEditJob.objects.get(pk=job_id)
            job.status = BulkEditJob.Status.STARTED
            job.save(update_fields=["status"])
        except BulkEditJob.DoesNotExist:
            logger.warning(f"BulkEditJob {job_id} not found, proceeding without tracking")

    # -- Conflict detection ------------------------------------------------
    if expected_modified:
        conflicts = check_document_conflicts(doc_ids, expected_modified)
        if conflicts:
            if job is not None:
                for doc_id in conflicts:
                    _update_job_item(
                        job,
                        doc_id,
                        BulkEditJobItem.Status.CONFLICT,
                        error_message="Document was modified concurrently",
                    )
                job.failed_documents = len(conflicts)
                job.status = BulkEditJob.Status.FAILED
                job.date_done = timezone.now()
                job.error_message = (
                    f"Conflict detected for {len(conflicts)} document(s)"
                )
                job.save()
            raise ConflictError(conflicts)

    # -- Execute with optional transaction wrapping -------------------------
    completed = 0
    failed = 0

    def _do_work() -> Literal["OK"]:
        nonlocal completed, failed
        if job is not None:
            # Per-document progress tracking: execute the function once for
            # the whole batch (existing functions are batch-oriented), then
            # update all items to success/failure.
            try:
                result = method_fn(doc_ids, **parameters)
                now = timezone.now()
                BulkEditJobItem.objects.filter(
                    job=job,
                    status=BulkEditJobItem.Status.PENDING,
                ).update(
                    status=BulkEditJobItem.Status.SUCCESS,
                    date_done=now,
                )
                completed = len(doc_ids)
                return result
            except Exception as exc:
                now = timezone.now()
                BulkEditJobItem.objects.filter(
                    job=job,
                    status=BulkEditJobItem.Status.PENDING,
                ).update(
                    status=BulkEditJobItem.Status.FAILURE,
                    error_message=str(exc)[:1000],
                    date_done=now,
                )
                failed = len(doc_ids)
                raise
        else:
            return method_fn(doc_ids, **parameters)

    try:
        if use_transaction:
            with transaction.atomic():
                result = _do_work()
        else:
            result = _do_work()

        if job is not None:
            job.completed_documents = completed
            job.failed_documents = failed
            _finalize_job(job)

        return {"result": result, "job_id": job_id}

    except ConflictError:
        # Already handled above, re-raise
        raise
    except Exception as exc:
        if job is not None:
            job.failed_documents = failed or len(doc_ids)
            job.error_message = f"{exc}\n{traceback.format_exc()}"[:2000]
            job.status = BulkEditJob.Status.FAILED
            job.date_done = timezone.now()
            job.save()
        raise


@shared_task(bind=True)
def restore_archive_serial_numbers_task(
    self,
    backup: dict[int, int | None],
    *args,
    **kwargs,
) -> None:
    restore_archive_serial_numbers(backup)


def release_archive_serial_numbers(doc_ids: list[int]) -> dict[int, int | None]:
    """
    Clears ASNs on documents that are about to be replaced so new documents
    can be assigned ASNs without uniqueness collisions. Returns a backup map
    of doc_id -> previous ASN for potential restoration.
    """
    qs = Document.objects.filter(
        id__in=doc_ids,
        archive_serial_number__isnull=False,
    ).only("pk", "archive_serial_number")
    backup = dict(qs.values_list("pk", "archive_serial_number"))
    qs.update(archive_serial_number=None)
    logger.info(f"Released archive serial numbers for documents {list(backup.keys())}")
    return backup


def restore_archive_serial_numbers(backup: dict[int, int | None]) -> None:
    """
    Restores ASNs using the provided backup map, intended for
    rollback when replacement consumption fails.
    """
    for doc_id, asn in backup.items():
        Document.objects.filter(pk=doc_id).update(archive_serial_number=asn)
    logger.info(f"Restored archive serial numbers for documents {list(backup.keys())}")


def _resolve_root_and_source_doc(
    doc: Document,
    *,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
) -> ResolvedDocPair:
    root_doc = get_root_document(doc)

    if source_mode == SourceModeChoices.EXPLICIT_SELECTION:
        return ResolvedDocPair(root_doc=root_doc, source_doc=doc)

    # Version IDs are explicit by default, only a selected root resolves to latest
    if doc.root_document_id is not None:
        return ResolvedDocPair(root_doc=root_doc, source_doc=doc)

    return ResolvedDocPair(
        root_doc=root_doc,
        source_doc=get_latest_version_for_root(root_doc),
    )


def set_correspondent(
    doc_ids: list[int],
    correspondent: Correspondent,
) -> Literal["OK"]:
    if correspondent:
        correspondent = Correspondent.objects.only("pk").get(id=correspondent)

    qs = (
        Document.objects.filter(Q(id__in=doc_ids) & ~Q(correspondent=correspondent))
        .select_related("correspondent")
        .only("pk", "correspondent__id")
    )
    affected_docs = list(qs.values_list("pk", flat=True))
    qs.update(correspondent=correspondent)

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def set_storage_path(doc_ids: list[int], storage_path: StoragePath) -> Literal["OK"]:
    if storage_path:
        storage_path = StoragePath.objects.only("pk").get(id=storage_path)

    qs = (
        Document.objects.filter(
            Q(id__in=doc_ids) & ~Q(storage_path=storage_path),
        )
        .select_related("storage_path")
        .only("pk", "storage_path__id")
    )
    affected_docs = list(qs.values_list("pk", flat=True))
    qs.update(storage_path=storage_path)

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def set_document_type(doc_ids: list[int], document_type: DocumentType) -> Literal["OK"]:
    if document_type:
        document_type = DocumentType.objects.only("pk").get(id=document_type)

    qs = (
        Document.objects.filter(Q(id__in=doc_ids) & ~Q(document_type=document_type))
        .select_related("document_type")
        .only("pk", "document_type__id")
    )
    affected_docs = list(qs.values_list("pk", flat=True))
    qs.update(document_type=document_type)

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def add_tag(doc_ids: list[int], tag: int) -> Literal["OK"]:
    tag_obj = Tag.objects.get(pk=tag)
    tags_to_add = [tag_obj, *tag_obj.get_ancestors()]

    DocumentTagRelationship = Document.tags.through
    to_create = []
    affected_docs: set[int] = set()

    for t in tags_to_add:
        qs = Document.objects.filter(Q(id__in=doc_ids) & ~Q(tags__id=t.id)).only("pk")
        doc_ids_missing_tag = list(qs.values_list("pk", flat=True))
        affected_docs.update(doc_ids_missing_tag)
        to_create.extend(
            DocumentTagRelationship(document_id=doc, tag_id=t.id)
            for doc in doc_ids_missing_tag
        )

    if to_create:
        DocumentTagRelationship.objects.bulk_create(to_create)

    if affected_docs:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": list(affected_docs)},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )

    return "OK"


def remove_tag(doc_ids: list[int], tag: int) -> Literal["OK"]:
    tag_obj = Tag.objects.get(pk=tag)
    tag_ids = [tag_obj.id, *tag_obj.get_descendants_pks()]

    DocumentTagRelationship = Document.tags.through
    qs = DocumentTagRelationship.objects.filter(
        document_id__in=doc_ids,
        tag_id__in=tag_ids,
    )
    affected_docs = list(qs.values_list("document_id", flat=True).distinct())
    qs.delete()

    if affected_docs:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": affected_docs},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )

    return "OK"


def modify_tags(
    doc_ids: list[int],
    add_tags: list[int],
    remove_tags: list[int],
) -> Literal["OK"]:
    qs = Document.objects.filter(id__in=doc_ids).only("pk")
    affected_docs = list(qs.values_list("pk", flat=True))
    DocumentTagRelationship = Document.tags.through

    # add with all ancestors
    expanded_add_tags: set[int] = set()
    add_tag_objects = Tag.objects.filter(pk__in=add_tags)
    for t in add_tag_objects:
        expanded_add_tags.add(int(t.id))
        expanded_add_tags.update(int(pk) for pk in t.get_ancestors_pks())

    # remove with all descendants
    expanded_remove_tags: set[int] = set()
    remove_tag_objects = Tag.objects.filter(pk__in=remove_tags)
    for t in remove_tag_objects:
        expanded_remove_tags.add(int(t.id))
        expanded_remove_tags.update(int(pk) for pk in t.get_descendants_pks())

    with transaction.atomic():
        if expanded_remove_tags:
            DocumentTagRelationship.objects.filter(
                document_id__in=affected_docs,
                tag_id__in=expanded_remove_tags,
            ).delete()

        to_create = []
        if expanded_add_tags:
            existing_pairs = set(
                DocumentTagRelationship.objects.filter(
                    document_id__in=affected_docs,
                    tag_id__in=expanded_add_tags,
                ).values_list("document_id", "tag_id"),
            )

            to_create = [
                DocumentTagRelationship(document_id=doc, tag_id=tag)
                for doc in affected_docs
                for tag in expanded_add_tags
                if (doc, tag) not in existing_pairs
            ]

            if to_create:
                DocumentTagRelationship.objects.bulk_create(
                    to_create,
                    ignore_conflicts=True,
                )

    if affected_docs:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": affected_docs},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )

    return "OK"


def modify_custom_fields(
    doc_ids: list[int],
    add_custom_fields: list[int] | dict,
    remove_custom_fields: list[int],
) -> Literal["OK"]:
    qs = Document.objects.filter(id__in=doc_ids).only("pk")
    affected_docs = list(qs.values_list("pk", flat=True))
    # Ensure add_custom_fields is a list of tuples, supports old API
    add_custom_fields = (
        add_custom_fields.items()
        if isinstance(add_custom_fields, dict)
        else [(field, None) for field in add_custom_fields]
    )

    custom_fields = CustomField.objects.filter(
        id__in=[int(field) for field, _ in add_custom_fields],
    ).distinct()
    for field_id, value in add_custom_fields:
        for doc_id in affected_docs:
            defaults = {}
            custom_field = custom_fields.get(id=field_id)
            if custom_field:
                value_field = CustomFieldInstance.TYPE_TO_DATA_STORE_NAME_MAP[
                    custom_field.data_type
                ]
                defaults[value_field] = value
                if (
                    custom_field.data_type == CustomField.FieldDataType.DOCUMENTLINK
                    and value
                    and doc_id in value
                ):
                    # Prevent self-linking
                    continue
            CustomFieldInstance.objects.update_or_create(
                document_id=doc_id,
                field_id=field_id,
                defaults=defaults,
            )
            if custom_field.data_type == CustomField.FieldDataType.DOCUMENTLINK:
                doc = Document.objects.get(id=doc_id)
                reflect_doclinks(doc, custom_field, value)

    # For doc link fields that are being removed, remove symmetrical links
    for doclink_being_removed_instance in CustomFieldInstance.objects.filter(
        document_id__in=affected_docs,
        field__id__in=remove_custom_fields,
        field__data_type=CustomField.FieldDataType.DOCUMENTLINK,
        value_document_ids__isnull=False,
    ):
        for target_doc_id in doclink_being_removed_instance.value:
            remove_doclink(
                document=Document.objects.get(
                    id=doclink_being_removed_instance.document.id,
                ),
                field=doclink_being_removed_instance.field,
                target_doc_id=target_doc_id,
            )

    # Finally, remove the custom fields
    CustomFieldInstance.objects.filter(
        document_id__in=affected_docs,
        field_id__in=remove_custom_fields,
    ).hard_delete()

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


@shared_task
def delete(doc_ids: list[int]) -> Literal["OK"]:
    try:
        root_ids = (
            Document.objects.filter(id__in=doc_ids, root_document__isnull=True)
            .values_list("id", flat=True)
            .distinct()
        )
        version_ids = (
            Document.objects.filter(root_document_id__in=root_ids)
            .exclude(id__in=doc_ids)
            .values_list("id", flat=True)
            .distinct()
        )
        delete_ids = list({*doc_ids, *version_ids})

        Document.objects.filter(id__in=delete_ids).delete()

        from documents.search import get_backend

        with get_backend().batch_update() as batch:
            for id in delete_ids:
                batch.remove(id)

        status_mgr = DocumentsStatusManager()
        status_mgr.send_documents_deleted(delete_ids)
    except Exception as e:
        if "Data too long for column" in str(e):
            logger.warning(
                "Detected a possible incompatible database column. See https://docs.paperless-ngx.com/troubleshooting/#convert-uuid-field",
            )
        logger.error(f"Error deleting documents: {e!s}")

    return "OK"


def reprocess(doc_ids: list[int]) -> Literal["OK"]:
    for document_id in doc_ids:
        update_document_content_maybe_archive_file.apply_async(
            kwargs={"document_id": document_id},
            headers={"trigger_source": PaperlessTask.TriggerSource.MANUAL},
        )

    return "OK"


def set_permissions(
    doc_ids: list[int],
    set_permissions: dict,
    *,
    owner: User | None = None,
    merge: bool = False,
) -> Literal["OK"]:
    qs = Document.objects.filter(id__in=doc_ids).select_related("owner")

    if merge:
        # If merging, only set owner for documents that don't have an owner
        qs.filter(owner__isnull=True).update(owner=owner)
    else:
        qs.update(owner=owner)

    for doc in qs:
        set_permissions_for_object(permissions=set_permissions, object=doc, merge=merge)

    affected_docs = list(qs.values_list("pk", flat=True))

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def rotate(
    doc_ids: list[int],
    degrees: int,
    *,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    logger.info(
        f"Attempting to rotate {len(doc_ids)} documents by {degrees} degrees.",
    )
    docs_by_id = {
        doc.id: doc
        for doc in Document.objects.select_related("root_document").filter(
            id__in=doc_ids,
        )
    }
    docs_by_root_id: dict[int, ResolvedDocPair] = {}
    for doc_id in doc_ids:
        doc = docs_by_id.get(doc_id)
        if doc is None:
            continue
        pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
        docs_by_root_id.setdefault(pair.root_doc.id, pair)

    import pikepdf

    for pair in docs_by_root_id.values():
        if pair.source_doc.mime_type != "application/pdf":
            logger.warning(
                f"Document {pair.root_doc.id} is not a PDF, skipping rotation.",
            )
            continue
        try:
            # Write rotated output to a temp file and create a new version via consume pipeline
            filepath: Path = (
                Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR))
                / f"{pair.root_doc.id}_rotated.pdf"
            )
            with pikepdf.open(pair.source_doc.source_path) as pdf:
                for page in pdf.pages:
                    page.rotate(degrees, relative=True)
                pdf.remove_unreferenced_resources()
                pdf.save(filepath)

            # Preserve metadata/permissions via overrides; mark as new version
            overrides = DocumentMetadataOverrides().from_document(pair.root_doc)
            if user is not None:
                overrides.actor_id = user.id

            consume_file.apply_async(
                kwargs={
                    "input_doc": ConsumableDocument(
                        source=DocumentSource.ConsumeFolder,
                        original_file=filepath,
                        root_document_id=pair.root_doc.id,
                    ),
                    "overrides": overrides,
                },
                headers={"trigger_source": trigger_source},
            )
            logger.info(
                f"Queued new rotated version for document {pair.root_doc.id} by {degrees} degrees",
            )
        except Exception as e:
            logger.exception(f"Error rotating document {pair.root_doc.id}: {e}")

    return "OK"


def merge(
    doc_ids: list[int],
    *,
    metadata_document_id: int | None = None,
    delete_originals: bool = False,
    archive_fallback: bool = False,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    logger.info(
        f"Attempting to merge {len(doc_ids)} documents into a single document.",
    )
    qs = Document.objects.select_related("root_document").filter(id__in=doc_ids)
    docs_by_id = {doc.id: doc for doc in qs}
    affected_docs: list[int] = []
    import pikepdf

    merged_pdf = pikepdf.new()
    version: str = merged_pdf.pdf_version
    handoff_asn: int | None = None
    # use doc_ids to preserve order
    for doc_id in doc_ids:
        doc = docs_by_id.get(doc_id)
        if doc is None:
            continue
        pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
        try:
            doc_path = (
                pair.source_doc.archive_path
                if archive_fallback
                and pair.source_doc.mime_type != "application/pdf"
                and pair.source_doc.has_archive_version
                else pair.source_doc.source_path
            )
            with pikepdf.open(str(doc_path)) as pdf:
                version = max(version, pdf.pdf_version)
                merged_pdf.pages.extend(pdf.pages)
            affected_docs.append(doc.id)
            if handoff_asn is None and doc.archive_serial_number is not None:
                handoff_asn = doc.archive_serial_number
        except Exception as e:
            logger.exception(
                f"Error merging document {doc.id}, it will not be included in the merge: {e}",
            )
    if len(affected_docs) == 0:
        logger.warning("No documents were merged")
        return "OK"

    filepath = (
        Path(
            tempfile.mkdtemp(dir=settings.SCRATCH_DIR),
        )
        / f"{'_'.join([str(doc_id) for doc_id in affected_docs])[:100]}_merged.pdf"
    )
    merged_pdf.remove_unreferenced_resources()
    merged_pdf.save(filepath, min_version=version)
    merged_pdf.close()

    if metadata_document_id:
        metadata_document = qs.get(id=metadata_document_id)
        if metadata_document is not None:
            overrides: DocumentMetadataOverrides = (
                DocumentMetadataOverrides.from_document(metadata_document)
            )
            overrides.title = metadata_document.title + " (merged)"
            if metadata_document.archive_serial_number is not None:
                handoff_asn = metadata_document.archive_serial_number
        else:
            overrides = DocumentMetadataOverrides()
    else:
        overrides = DocumentMetadataOverrides()

    if user is not None:
        overrides.owner_id = user.id
    if not delete_originals:
        overrides.skip_asn_if_exists = True

    if delete_originals and handoff_asn is not None:
        overrides.asn = handoff_asn

    logger.info("Adding merged document to the task queue.")

    consume_task = consume_file.s(
        input_doc=ConsumableDocument(
            source=DocumentSource.ConsumeFolder,
            original_file=filepath,
        ),
        overrides=overrides,
    ).set(headers={"trigger_source": trigger_source})

    if delete_originals:
        backup = release_archive_serial_numbers(affected_docs)
        logger.info(
            "Queueing removal of original documents after consumption of merged document",
        )
        try:
            consume_task.apply_async(
                link=[delete.si(affected_docs)],
                link_error=[restore_archive_serial_numbers_task.s(backup)],
            )
        except Exception:
            restore_archive_serial_numbers(backup)
            raise
    else:
        consume_task.apply_async()

    return "OK"


def split(
    doc_ids: list[int],
    pages: list[list[int]],
    *,
    delete_originals: bool = False,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    logger.info(
        f"Attempting to split document {doc_ids[0]} into {len(pages)} documents",
    )
    doc = Document.objects.select_related("root_document").get(id=doc_ids[0])
    pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
    import pikepdf

    consume_tasks = []

    try:
        with pikepdf.open(pair.source_doc.source_path) as pdf:
            for idx, split_doc in enumerate(pages):
                dst: pikepdf.Pdf = pikepdf.new()
                for page in split_doc:
                    dst.pages.append(pdf.pages[page - 1])
                filepath: Path = (
                    Path(
                        tempfile.mkdtemp(dir=settings.SCRATCH_DIR),
                    )
                    / f"{doc.id}_{split_doc[0]}-{split_doc[-1]}.pdf"
                )
                dst.remove_unreferenced_resources()
                dst.save(filepath)
                dst.close()

                overrides: DocumentMetadataOverrides = (
                    DocumentMetadataOverrides().from_document(doc)
                )
                overrides.title = f"{doc.title} (split {idx + 1})"
                if user is not None:
                    overrides.owner_id = user.id
                if not delete_originals:
                    overrides.skip_asn_if_exists = True
                logger.info(
                    f"Adding split document with pages {split_doc} to the task queue.",
                )
                consume_tasks.append(
                    consume_file.s(
                        input_doc=ConsumableDocument(
                            source=DocumentSource.ConsumeFolder,
                            original_file=filepath,
                        ),
                        overrides=overrides,
                    ).set(headers={"trigger_source": trigger_source}),
                )

            if delete_originals:
                backup = release_archive_serial_numbers([doc.id])
                logger.info(
                    "Queueing removal of original document after consumption of the split documents",
                )
                try:
                    chord(
                        header=consume_tasks,
                        body=delete.si([doc.id]),
                    ).on_error(
                        restore_archive_serial_numbers_task.s(backup),
                    ).apply_async()
                except Exception:
                    restore_archive_serial_numbers(backup)
                    raise
            else:
                group(consume_tasks).delay()

    except Exception as e:
        logger.exception(f"Error splitting document {doc.id}: {e}")

    return "OK"


def delete_pages(
    doc_ids: list[int],
    pages: list[int],
    *,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    logger.info(
        f"Attempting to delete pages {pages} from {len(doc_ids)} documents",
    )
    doc = Document.objects.select_related("root_document").get(id=doc_ids[0])
    pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
    pages = sorted(pages)  # sort pages to avoid index issues
    import pikepdf

    try:
        # Produce edited PDF to a temp file and create a new version
        filepath: Path = (
            Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR))
            / f"{pair.root_doc.id}_pages_deleted.pdf"
        )
        with pikepdf.open(pair.source_doc.source_path) as pdf:
            offset = 1  # pages are 1-indexed
            for page_num in pages:
                pdf.pages.remove(pdf.pages[page_num - offset])
                offset += 1  # remove() changes the index of the pages
            pdf.remove_unreferenced_resources()
            pdf.save(filepath)

        overrides = DocumentMetadataOverrides().from_document(pair.root_doc)
        if user is not None:
            overrides.actor_id = user.id
        consume_file.apply_async(
            kwargs={
                "input_doc": ConsumableDocument(
                    source=DocumentSource.ConsumeFolder,
                    original_file=filepath,
                    root_document_id=pair.root_doc.id,
                ),
                "overrides": overrides,
            },
            headers={"trigger_source": trigger_source},
        )
        logger.info(
            f"Queued new version for document {pair.root_doc.id} after deleting pages {pages}",
        )
    except Exception as e:
        logger.exception(f"Error deleting pages from document {pair.root_doc.id}: {e}")

    return "OK"


def edit_pdf(
    doc_ids: list[int],
    operations: list[dict[str, int]],
    *,
    delete_original: bool = False,
    update_document: bool = False,
    include_metadata: bool = True,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    """
    Operations is a list of dictionaries describing the final PDF pages.
    Each entry must contain the original page number in `page` and may
    specify `rotate` in degrees and `doc` indicating the output
    document index (for splitting). Pages omitted from the list are
    discarded.
    """

    logger.info(
        f"Editing PDF of document {doc_ids[0]} with {len(operations)} operations",
    )
    doc = Document.objects.select_related("root_document").get(id=doc_ids[0])
    pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
    import pikepdf

    pdf_docs: list[pikepdf.Pdf] = []

    try:
        with pikepdf.open(pair.source_doc.source_path) as src:
            # prepare output documents
            max_idx = max(op.get("doc", 0) for op in operations)
            pdf_docs = [pikepdf.new() for _ in range(max_idx + 1)]

            if update_document and len(pdf_docs) > 1:
                logger.error(
                    "Update requested but multiple output documents specified",
                )
                raise ValueError("Multiple output documents specified")

            for op in operations:
                dst = pdf_docs[op.get("doc", 0)]
                page = src.pages[op["page"] - 1]
                dst.pages.append(page)
                if op.get("rotate"):
                    dst.pages[-1].rotate(op["rotate"], relative=True)

        if update_document:
            # Create a new version from the edited PDF rather than replacing in-place
            pdf = pdf_docs[0]
            pdf.remove_unreferenced_resources()
            filepath: Path = (
                Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR))
                / f"{pair.root_doc.id}_edited.pdf"
            )
            pdf.save(filepath)
            overrides = (
                DocumentMetadataOverrides().from_document(pair.root_doc)
                if include_metadata
                else DocumentMetadataOverrides()
            )
            if user is not None:
                overrides.owner_id = user.id
                overrides.actor_id = user.id
            consume_file.apply_async(
                kwargs={
                    "input_doc": ConsumableDocument(
                        source=DocumentSource.ConsumeFolder,
                        original_file=filepath,
                        root_document_id=pair.root_doc.id,
                    ),
                    "overrides": overrides,
                },
                headers={"trigger_source": trigger_source},
            )
        else:
            consume_tasks = []
            overrides = (
                DocumentMetadataOverrides().from_document(pair.root_doc)
                if include_metadata
                else DocumentMetadataOverrides()
            )
            if user is not None:
                overrides.owner_id = user.id
                overrides.actor_id = user.id
            if not delete_original:
                overrides.skip_asn_if_exists = True
            if delete_original and len(pdf_docs) == 1:
                overrides.asn = pair.root_doc.archive_serial_number
            for idx, pdf in enumerate(pdf_docs, start=1):
                version_filepath: Path = (
                    Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR))
                    / f"{pair.root_doc.id}_edit_{idx}.pdf"
                )
                pdf.remove_unreferenced_resources()
                pdf.save(version_filepath)
                consume_tasks.append(
                    consume_file.s(
                        input_doc=ConsumableDocument(
                            source=DocumentSource.ConsumeFolder,
                            original_file=version_filepath,
                        ),
                        overrides=overrides,
                    ).set(headers={"trigger_source": trigger_source}),
                )

            if delete_original:
                backup = release_archive_serial_numbers([doc.id])
                try:
                    chord(
                        header=consume_tasks,
                        body=delete.si([doc.id]),
                    ).on_error(
                        restore_archive_serial_numbers_task.s(backup),
                    ).apply_async()
                except Exception:
                    restore_archive_serial_numbers(backup)
                    raise
            else:
                group(consume_tasks).delay()

    except Exception as e:
        logger.exception(f"Error editing document {pair.root_doc.id}: {e}")
        raise ValueError(
            f"An error occurred while editing the document: {e}",
        ) from e

    return "OK"


def remove_password(
    doc_ids: list[int],
    password: str,
    *,
    update_document: bool = False,
    delete_original: bool = False,
    include_metadata: bool = True,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
    source_paths_by_id: Mapping[int, Path] | None = None,
) -> Literal["OK"]:
    """
    Remove password protection from PDF documents.
    """
    import pikepdf

    for doc_id in doc_ids:
        doc = Document.objects.select_related("root_document").get(id=doc_id)
        pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
        try:
            logger.info(
                f"Attempting password removal from document {pair.root_doc.id}",
            )
            # The caller may supply an explicit source path (e.g. the staged
            # file during consumption, before source_path is populated).
            source_path = (source_paths_by_id or {}).get(
                doc.id,
                pair.source_doc.source_path,
            )
            with pikepdf.open(source_path, password=password) as pdf:
                filepath: Path = (
                    Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR))
                    / f"{pair.root_doc.id}_unprotected.pdf"
                )
                pdf.remove_unreferenced_resources()
                pdf.save(filepath)

                if update_document:
                    # Create a new version rather than modifying the root/original in place.
                    overrides = (
                        DocumentMetadataOverrides().from_document(pair.root_doc)
                        if include_metadata
                        else DocumentMetadataOverrides()
                    )
                    if user is not None:
                        overrides.owner_id = user.id
                        overrides.actor_id = user.id
                    consume_file.apply_async(
                        kwargs={
                            "input_doc": ConsumableDocument(
                                source=DocumentSource.ConsumeFolder,
                                original_file=filepath,
                                root_document_id=pair.root_doc.id,
                            ),
                            "overrides": overrides,
                        },
                        headers={"trigger_source": trigger_source},
                    )
                else:
                    consume_tasks = []
                    overrides = (
                        DocumentMetadataOverrides().from_document(pair.root_doc)
                        if include_metadata
                        else DocumentMetadataOverrides()
                    )
                    if user is not None:
                        overrides.owner_id = user.id
                        overrides.actor_id = user.id

                    consume_tasks.append(
                        consume_file.s(
                            input_doc=ConsumableDocument(
                                source=DocumentSource.ConsumeFolder,
                                original_file=filepath,
                            ),
                            overrides=overrides,
                        ).set(headers={"trigger_source": trigger_source}),
                    )

                    if delete_original:
                        chord(
                            header=consume_tasks,
                            body=delete.si([doc.id]),
                        ).delay()
                    else:
                        group(consume_tasks).delay()

        except Exception as e:
            logger.exception(
                f"Error removing password from document {pair.root_doc.id}: {e}",
            )
            raise ValueError(
                f"An error occurred while removing the password: {e}",
            ) from e

    return "OK"


def reflect_doclinks(
    document: Document,
    field: CustomField,
    target_doc_ids: list[int],
) -> None:
    """
    Add or remove 'symmetrical' links to `document` on all `target_doc_ids`
    """

    if target_doc_ids is None:
        target_doc_ids = []

    # Check if any documents are going to be removed from the current list of links and remove the symmetrical links
    current_field_instance = CustomFieldInstance.objects.filter(
        field=field,
        document=document,
    ).first()
    if current_field_instance is not None and current_field_instance.value is not None:
        for doc_id in current_field_instance.value:
            if doc_id not in target_doc_ids:
                remove_doclink(
                    document=document,
                    field=field,
                    target_doc_id=doc_id,
                )

    # Create an instance if target doc doesn't have this field or append it to an existing one
    existing_custom_field_instances = {
        custom_field.document_id: custom_field
        for custom_field in CustomFieldInstance.objects.filter(
            field=field,
            document_id__in=target_doc_ids,
        )
    }
    custom_field_instances_to_create = []
    custom_field_instances_to_update = []
    for target_doc_id in target_doc_ids:
        target_doc_field_instance = existing_custom_field_instances.get(
            target_doc_id,
        )
        if target_doc_field_instance is None:
            custom_field_instances_to_create.append(
                CustomFieldInstance(
                    document_id=target_doc_id,
                    field=field,
                    value_document_ids=[document.id],
                ),
            )
        elif target_doc_field_instance.value is None:
            target_doc_field_instance.value_document_ids = [document.id]
            custom_field_instances_to_update.append(target_doc_field_instance)
        elif document.id not in target_doc_field_instance.value:
            target_doc_field_instance.value_document_ids.append(document.id)
            custom_field_instances_to_update.append(target_doc_field_instance)

    CustomFieldInstance.objects.bulk_create(custom_field_instances_to_create)
    CustomFieldInstance.objects.bulk_update(
        custom_field_instances_to_update,
        ["value_document_ids"],
    )
    Document.objects.filter(id__in=target_doc_ids).update(modified=timezone.now())


def remove_doclink(
    document: Document,
    field: CustomField,
    target_doc_id: int,
) -> None:
    """
    Removes a 'symmetrical' link to `document` from the target document's existing custom field instance
    """
    target_doc_field_instance = CustomFieldInstance.objects.filter(
        document_id=target_doc_id,
        field=field,
    ).first()
    if (
        target_doc_field_instance is not None
        and document.id in target_doc_field_instance.value
    ):
        target_doc_field_instance.value.remove(document.id)
        target_doc_field_instance.save()
    Document.objects.filter(id=target_doc_id).update(modified=timezone.now())

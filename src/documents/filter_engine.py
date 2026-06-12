"""
Composable filter expression engine for paperless-ngx.

Provides a unified, JSON-based DSL for filtering documents that combines
text matching (content/title) with metadata matching (tags, correspondent,
dates, ASN, page count, etc.) and supports explicit AND/OR/NOT composition.

Used by both MatchingModel and WorkflowTrigger to replace their flat filter
fields with a tree-structured expression.

Public API:
    evaluate_filter_expression(expression, document, context) -> bool
    validate_filter_expression(expression) -> None
    build_queryset_from_expression(queryset, expression) -> QuerySet
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, ClassVar

from django.db.models import Q, QuerySet
from django.utils import timezone

if TYPE_CHECKING:
    from documents.data_models import ConsumableDocument
    from documents.models import Document

logger = logging.getLogger("paperless.filter_engine")

# ---------------------------------------------------------------------------
# Sentinel & exceptions
# ---------------------------------------------------------------------------

class _SKIP:
    """Returned by a condition handler when it cannot evaluate against the
    given document type (e.g. a ``tag`` condition against a
    ``ConsumableDocument`` that has no tags yet)."""

    def __bool__(self) -> bool:
        raise TypeError("SKIP must not be used as a boolean directly")

    def __repr__(self) -> str:
        return "SKIP"


SKIP = _SKIP()


class FilterExpressionValidationError(Exception):
    """Raised when a filter expression is structurally invalid."""

    def __init__(self, message: str, path: str = "") -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


# ---------------------------------------------------------------------------
# Filter context
# ---------------------------------------------------------------------------

@dataclass
class FilterContext:
    """Runtime context passed to condition evaluators.

    Attributes:
        fail_closed: When ``True``, conditions that return ``SKIP`` are
            treated as ``False`` (strict).  When ``False`` (default),
            ``SKIP`` is treated as ``True`` (lenient).  Consumption-time
            triggers use lenient mode because metadata may not exist yet.
        _document_tag_ids: Lazily-loaded set of tag IDs for the document.
    """

    fail_closed: bool = False
    _document_tag_ids: set[int] | None = field(default=None, repr=False)

    # -- helpers ---------------------------------------------------------

    def get_document_tag_ids(self, document: "Document") -> set[int]:
        if self._document_tag_ids is None:
            self._document_tag_ids = set(
                document.tags.values_list("id", flat=True),
            )
        return self._document_tag_ids

    def resolve_skip(self) -> bool:
        """Convert a SKIP into a boolean based on fail_closed policy."""
        return not self.fail_closed  # lenient → True; strict → False


# Maximum nesting depth and total leaf-condition count.
MAX_DEPTH = 10
MAX_CONDITIONS = 50

# ---------------------------------------------------------------------------
# Condition handler registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, "ConditionHandler"] = {}


class ConditionHandler:
    """Base class for condition handlers."""

    # Subclasses set this via @register_condition.
    condition_type: ClassVar[str] = ""

    def evaluate(
        self,
        condition: dict[str, Any],
        document: Any,
        context: FilterContext,
    ) -> bool | _SKIP:
        """Return ``True``/``False`` if the document matches, or ``SKIP``
        if the condition cannot be evaluated against this document type."""
        raise NotImplementedError

    def to_q(self, condition: dict[str, Any]) -> Q | _SKIP:
        """Translate the condition into a Django ``Q`` object for
        queryset-level pre-filtering.  Return ``SKIP`` if the condition
        cannot be expressed as SQL."""
        return SKIP

    def validate(self, condition: dict[str, Any]) -> None:
        """Raise ``FilterExpressionValidationError`` if the condition
        dict is invalid."""
        pass


def register_condition(type_name: str):
    """Decorator that registers a condition handler class."""

    def decorator(cls: type[ConditionHandler]) -> type[ConditionHandler]:
        cls.condition_type = type_name
        _REGISTRY[type_name] = cls()
        return cls

    return decorator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_document(obj: Any) -> bool:
    """Return True if *obj* is a saved ``Document`` (not a ConsumableDocument)."""
    from documents.models import Document as DocModel

    return isinstance(obj, DocModel)


def _is_consumable(obj: Any) -> bool:
    from documents.data_models import ConsumableDocument as CD

    return isinstance(obj, CD)


def _require_field(condition: dict, key: str, path: str) -> Any:
    if key not in condition:
        raise FilterExpressionValidationError(
            f"Missing required field '{key}'", path=path,
        )
    return condition[key]


def _require_int_list(condition: dict, key: str, path: str) -> list[int]:
    val = _require_field(condition, key, path)
    if not isinstance(val, list) or not all(isinstance(v, int) for v in val):
        raise FilterExpressionValidationError(
            f"'{key}' must be a list of integers", path=path,
        )
    return val


_COMPARISON_OPS = {
    "exact": lambda a, b: a == b,
    "gt": lambda a, b: a is not None and a > b,
    "gte": lambda a, b: a is not None and a >= b,
    "lt": lambda a, b: a is not None and a < b,
    "lte": lambda a, b: a is not None and a <= b,
    "isnull": lambda a, b: (a is None) == b,
}


def _compare(actual: Any, op: str, value: Any) -> bool:
    """Generic comparison for scalar fields."""
    if op == "range":
        if not isinstance(value, list | tuple) or len(value) != 2:
            return False
        return actual is not None and value[0] <= actual <= value[1]
    fn = _COMPARISON_OPS.get(op)
    if fn is None:
        return False
    return fn(actual, value)


def _compare_to_q(field_name: str, op: str, value: Any) -> Q:
    """Translate a comparison into a Django Q object."""
    if op == "range":
        return Q(**{f"{field_name}__range": value})
    if op == "isnull":
        return Q(**{f"{field_name}__isnull": value})
    if op == "exact":
        return Q(**{field_name: value})
    return Q(**{f"{field_name}__{op}": value})


def _parse_date(value: Any) -> datetime.date | datetime.datetime:
    """Parse a date or datetime string."""
    if isinstance(value, datetime.datetime | datetime.date):
        return value
    if not isinstance(value, str):
        raise FilterExpressionValidationError(
            f"Expected date string, got {type(value).__name__}",
        )
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise FilterExpressionValidationError(f"Cannot parse date: {value!r}")


# ---------------------------------------------------------------------------
# Core evaluator
# ---------------------------------------------------------------------------

def _evaluate_node(
    expression: dict[str, Any],
    document: Any,
    context: FilterContext,
    depth: int,
    counter: list[int],
) -> bool:
    """Recursively evaluate a single expression node."""
    if depth > MAX_DEPTH:
        raise FilterExpressionValidationError(
            f"Maximum nesting depth ({MAX_DEPTH}) exceeded",
        )

    if not isinstance(expression, dict):
        raise FilterExpressionValidationError(
            f"Expression must be a dict, got {type(expression).__name__}",
        )

    node_type = expression.get("type")
    if not node_type:
        raise FilterExpressionValidationError("Missing 'type' field")

    # -- logical operators ------------------------------------------------
    if node_type == "and":
        conditions = expression.get("conditions", [])
        if not isinstance(conditions, list):
            raise FilterExpressionValidationError(
                "'and' requires a 'conditions' list",
            )
        if not conditions:
            return True  # vacuous truth
        return all(
            _evaluate_node(c, document, context, depth + 1, counter)
            for c in conditions
        )

    if node_type == "or":
        conditions = expression.get("conditions", [])
        if not isinstance(conditions, list):
            raise FilterExpressionValidationError(
                "'or' requires a 'conditions' list",
            )
        if not conditions:
            return False  # vacuous false
        return any(
            _evaluate_node(c, document, context, depth + 1, counter)
            for c in conditions
        )

    if node_type == "not":
        condition = expression.get("condition")
        if condition is None:
            raise FilterExpressionValidationError(
                "'not' requires a 'condition' field",
            )
        return not _evaluate_node(condition, document, context, depth + 1, counter)

    # -- leaf condition ---------------------------------------------------
    handler = _REGISTRY.get(node_type)
    if handler is None:
        raise FilterExpressionValidationError(
            f"Unknown condition type: {node_type!r}",
        )

    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise FilterExpressionValidationError(
            f"Maximum number of conditions ({MAX_CONDITIONS}) exceeded",
        )

    result = handler.evaluate(expression, document, context)
    if isinstance(result, _SKIP):
        return context.resolve_skip()
    return bool(result)


def evaluate_filter_expression(
    expression: dict[str, Any],
    document: Any,
    context: FilterContext | None = None,
) -> bool:
    """Evaluate a filter expression against a document instance.

    Args:
        expression: The JSON filter expression tree.
        document: A ``Document`` or ``ConsumableDocument`` instance.
        context: Optional ``FilterContext``.  If ``None``, a default
            lenient context is created.

    Returns:
        ``True`` if the document matches the expression.
    """
    if context is None:
        context = FilterContext()
    return _evaluate_node(expression, document, context, depth=0, counter=[0])


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def _validate_node(expression: dict[str, Any], depth: int, counter: list[int]) -> None:
    """Recursively validate an expression tree without evaluating it."""
    if depth > MAX_DEPTH:
        raise FilterExpressionValidationError(
            f"Maximum nesting depth ({MAX_DEPTH}) exceeded",
        )
    if not isinstance(expression, dict):
        raise FilterExpressionValidationError(
            f"Expression must be a dict, got {type(expression).__name__}",
        )

    node_type = expression.get("type")
    if not node_type:
        raise FilterExpressionValidationError("Missing 'type' field")

    if node_type == "and" or node_type == "or":
        conditions = expression.get("conditions", [])
        if not isinstance(conditions, list):
            raise FilterExpressionValidationError(
                f"'{node_type}' requires a 'conditions' list",
            )
        for c in conditions:
            _validate_node(c, depth + 1, counter)
        return

    if node_type == "not":
        condition = expression.get("condition")
        if condition is None:
            raise FilterExpressionValidationError(
                "'not' requires a 'condition' field",
            )
        _validate_node(condition, depth + 1, counter)
        return

    handler = _REGISTRY.get(node_type)
    if handler is None:
        raise FilterExpressionValidationError(
            f"Unknown condition type: {node_type!r}",
        )
    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise FilterExpressionValidationError(
            f"Maximum number of conditions ({MAX_CONDITIONS}) exceeded",
        )
    handler.validate(expression)


def validate_filter_expression(expression: dict[str, Any]) -> None:
    """Validate a filter expression without evaluating it.

    Raises ``FilterExpressionValidationError`` on invalid input.
    """
    _validate_node(expression, depth=0, counter=[0])


# ---------------------------------------------------------------------------
# Queryset builder
# ---------------------------------------------------------------------------

def _node_to_q(expression: dict[str, Any], depth: int) -> Q:
    """Recursively translate an expression tree into a Django Q object."""
    if depth > MAX_DEPTH:
        raise FilterExpressionValidationError(
            f"Maximum nesting depth ({MAX_DEPTH}) exceeded",
        )
    if not isinstance(expression, dict):
        raise FilterExpressionValidationError(
            f"Expression must be a dict, got {type(expression).__name__}",
        )

    node_type = expression.get("type")
    if not node_type:
        raise FilterExpressionValidationError("Missing 'type' field")

    if node_type == "and":
        conditions = expression.get("conditions", [])
        if not isinstance(conditions, list):
            raise FilterExpressionValidationError(
                "'and' requires a 'conditions' list",
            )
        q = Q()
        for c in conditions:
            q &= _node_to_q(c, depth + 1)
        return q

    if node_type == "or":
        conditions = expression.get("conditions", [])
        if not isinstance(conditions, list):
            raise FilterExpressionValidationError(
                "'or' requires a 'conditions' list",
            )
        if not conditions:
            # Empty OR → match nothing
            return Q(pk__isnull=True)
        q = Q()
        first = True
        for c in conditions:
            sub = _node_to_q(c, depth + 1)
            if first:
                q = sub
                first = False
            else:
                q |= sub
        return q

    if node_type == "not":
        condition = expression.get("condition")
        if condition is None:
            raise FilterExpressionValidationError(
                "'not' requires a 'condition' field",
            )
        return ~_node_to_q(condition, depth + 1)

    handler = _REGISTRY.get(node_type)
    if handler is None:
        raise FilterExpressionValidationError(
            f"Unknown condition type: {node_type!r}",
        )

    result = handler.to_q(expression)
    if isinstance(result, _SKIP):
        # Cannot translate to SQL → match everything (will be filtered
        # at instance level later).
        return Q()
    return result


def build_queryset_from_expression(
    queryset: QuerySet,
    expression: dict[str, Any],
) -> QuerySet:
    """Apply a filter expression to a queryset using Django Q objects.

    Conditions that cannot be translated to SQL are silently skipped
    (they match everything at the queryset level and must be checked
    at the instance level separately).
    """
    q = _node_to_q(expression, depth=0)
    return queryset.filter(q)


# ===================================================================
# CONDITION HANDLERS
# ===================================================================

# ---------------------------------------------------------------------------
# Text matching (content)
# ---------------------------------------------------------------------------

@register_condition("text_match")
class TextMatchHandler(ConditionHandler):
    """Match against document content (OCR text).

    Schema::

        {"type": "text_match", "algorithm": "any", "value": "invoice receipt",
         "is_insensitive": true}

    Algorithms: ``any``, ``all``, ``literal``, ``regex``, ``fuzzy``
    """

    ALGORITHMS = {"any", "all", "literal", "regex", "fuzzy"}

    def evaluate(self, condition, document, context):
        value = condition.get("value", "")
        if not value or not value.strip():
            return False

        algorithm = condition.get("algorithm", "any")
        is_insensitive = condition.get("is_insensitive", True)
        flags = re.IGNORECASE if is_insensitive else 0

        content = self._get_content(document)
        if not content:
            return False

        if algorithm == "any":
            for word in self._split(value):
                if re.search(rf"\b{word}\b", content, flags=flags):
                    return True
            return False

        if algorithm == "all":
            for word in self._split(value):
                if not re.search(rf"\b{word}\b", content, flags=flags):
                    return False
            return True

        if algorithm == "literal":
            return bool(
                re.search(rf"\b{re.escape(value)}\b", content, flags=flags),
            )

        if algorithm == "regex":
            from documents.regex import safe_regex_search

            return bool(safe_regex_search(value, content, flags=flags))

        if algorithm == "fuzzy":
            from rapidfuzz import fuzz

            match_str = re.sub(r"[^\w\s]", "", value)
            text = re.sub(r"[^\w\s]", "", content)
            if is_insensitive:
                match_str = match_str.lower()
                text = text.lower()
            return bool(fuzz.partial_ratio(match_str, text, score_cutoff=90))

        raise FilterExpressionValidationError(
            f"Unknown text_match algorithm: {algorithm!r}",
        )

    def to_q(self, condition):
        # Content matching cannot be efficiently translated to SQL
        # (regex with word boundaries, fuzzy, etc.)
        return SKIP

    def validate(self, condition):
        if "value" not in condition:
            raise FilterExpressionValidationError(
                "text_match requires 'value' field",
            )
        algo = condition.get("algorithm", "any")
        if algo not in self.ALGORITHMS:
            raise FilterExpressionValidationError(
                f"Unknown algorithm '{algo}'. Must be one of: {self.ALGORITHMS}",
            )

    @staticmethod
    def _get_content(document: Any) -> str:
        if _is_document(document):
            return document.get_effective_content() or ""
        # ConsumableDocument doesn't have content yet
        return ""

    @staticmethod
    def _split(value: str) -> list[str]:
        findterms = re.compile(r'"([^"]+)"|(\S+)').findall
        normspace = re.compile(r"\s+").sub
        return [
            re.escape(normspace(" ", (t[0] or t[1]).strip())).replace(
                r"\ ", r"\s+",
            )
            for t in findterms(value)
        ]


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------

@register_condition("title")
class TitleMatchHandler(ConditionHandler):
    """Match against document title.

    Schema::

        {"type": "title", "op": "contains", "value": "Invoice"}

    Ops: ``exact``, ``contains``, ``icontains``, ``startswith``,
    ``istartswith``, ``endswith``, ``iendswith``
    """

    STRING_OPS = {
        "exact",
        "contains",
        "icontains",
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
    }

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            # ConsumableDocument has original_file but not title
            return SKIP

        title = document.title or ""
        op = condition.get("op", "contains")
        value = condition.get("value", "")

        if op == "exact":
            return title == value
        if op == "contains":
            return value in title
        if op == "icontains":
            return value.lower() in title.lower()
        if op == "startswith":
            return title.startswith(value)
        if op == "istartswith":
            return title.lower().startswith(value.lower())
        if op == "endswith":
            return title.endswith(value)
        if op == "iendswith":
            return title.lower().endswith(value.lower())
        return False

    def to_q(self, condition):
        op = condition.get("op", "contains")
        value = condition.get("value", "")
        if op == "exact":
            return Q(title=value)
        if op in self.STRING_OPS:
            return Q(**{f"title__{op}": value})
        return SKIP

    def validate(self, condition):
        if "value" not in condition:
            raise FilterExpressionValidationError(
                "title requires 'value' field",
            )
        op = condition.get("op", "contains")
        if op not in self.STRING_OPS:
            raise FilterExpressionValidationError(
                f"Unknown title op: {op!r}",
            )


# ---------------------------------------------------------------------------
# Tag conditions
# ---------------------------------------------------------------------------

@register_condition("tag")
class TagHandler(ConditionHandler):
    """Document has any (or all) of the specified tags.

    Schema::

        {"type": "tag", "ids": [1, 2], "all": false}
    """

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP

        ids = set(condition.get("ids", []))
        if not ids:
            return True  # empty set → vacuously true

        doc_tag_ids = context.get_document_tag_ids(document)
        if condition.get("all", False):
            return ids.issubset(doc_tag_ids)
        return bool(doc_tag_ids & ids)

    def to_q(self, condition):
        ids = condition.get("ids", [])
        if not ids:
            return Q()
        if condition.get("all", False):
            # Must have ALL tags → chain filters
            q = Q()
            for tag_id in ids:
                q &= Q(tags=tag_id)
            return q
        return Q(tags__in=ids)

    def validate(self, condition):
        _require_int_list(condition, "ids", "tag")


@register_condition("not_tag")
class NotTagHandler(ConditionHandler):
    """Document has none of the specified tags.

    Schema::

        {"type": "not_tag", "ids": [3, 4]}
    """

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP

        ids = set(condition.get("ids", []))
        if not ids:
            return True

        doc_tag_ids = context.get_document_tag_ids(document)
        return not bool(doc_tag_ids & ids)

    def to_q(self, condition):
        ids = condition.get("ids", [])
        if not ids:
            return Q()
        return ~Q(tags__in=ids)

    def validate(self, condition):
        _require_int_list(condition, "ids", "not_tag")


# ---------------------------------------------------------------------------
# Correspondent conditions
# ---------------------------------------------------------------------------

@register_condition("correspondent")
class CorrespondentHandler(ConditionHandler):
    """Document correspondent is one of the specified IDs.

    Schema::

        {"type": "correspondent", "ids": [1, 2]}
    """

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP

        ids = condition.get("ids", [])
        if not ids:
            return True
        return document.correspondent_id in ids

    def to_q(self, condition):
        ids = condition.get("ids", [])
        if not ids:
            return Q()
        return Q(correspondent__in=ids)

    def validate(self, condition):
        _require_int_list(condition, "ids", "correspondent")


@register_condition("not_correspondent")
class NotCorrespondentHandler(ConditionHandler):
    """Document correspondent is not any of the specified IDs.

    Schema::

        {"type": "not_correspondent", "ids": [3]}
    """

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP

        ids = condition.get("ids", [])
        if not ids:
            return True
        return document.correspondent_id not in ids

    def to_q(self, condition):
        ids = condition.get("ids", [])
        if not ids:
            return Q()
        return ~Q(correspondent__in=ids)

    def validate(self, condition):
        _require_int_list(condition, "ids", "not_correspondent")


# ---------------------------------------------------------------------------
# Document type conditions
# ---------------------------------------------------------------------------

@register_condition("document_type")
class DocumentTypeHandler(ConditionHandler):
    """Document type is one of the specified IDs."""

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP
        ids = condition.get("ids", [])
        if not ids:
            return True
        return document.document_type_id in ids

    def to_q(self, condition):
        ids = condition.get("ids", [])
        return Q(document_type__in=ids) if ids else Q()

    def validate(self, condition):
        _require_int_list(condition, "ids", "document_type")


@register_condition("not_document_type")
class NotDocumentTypeHandler(ConditionHandler):
    """Document type is not any of the specified IDs."""

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP
        ids = condition.get("ids", [])
        if not ids:
            return True
        return document.document_type_id not in ids

    def to_q(self, condition):
        ids = condition.get("ids", [])
        return ~Q(document_type__in=ids) if ids else Q()

    def validate(self, condition):
        _require_int_list(condition, "ids", "not_document_type")


# ---------------------------------------------------------------------------
# Storage path conditions
# ---------------------------------------------------------------------------

@register_condition("storage_path")
class StoragePathHandler(ConditionHandler):
    """Storage path is one of the specified IDs."""

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP
        ids = condition.get("ids", [])
        if not ids:
            return True
        return document.storage_path_id in ids

    def to_q(self, condition):
        ids = condition.get("ids", [])
        return Q(storage_path__in=ids) if ids else Q()

    def validate(self, condition):
        _require_int_list(condition, "ids", "storage_path")


@register_condition("not_storage_path")
class NotStoragePathHandler(ConditionHandler):
    """Storage path is not any of the specified IDs."""

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP
        ids = condition.get("ids", [])
        if not ids:
            return True
        return document.storage_path_id not in ids

    def to_q(self, condition):
        ids = condition.get("ids", [])
        return ~Q(storage_path__in=ids) if ids else Q()

    def validate(self, condition):
        _require_int_list(condition, "ids", "not_storage_path")


# ---------------------------------------------------------------------------
# Filename condition
# ---------------------------------------------------------------------------

@register_condition("filename")
class FilenameHandler(ConditionHandler):
    """Match document original_filename using fnmatch glob pattern.

    Schema::

        {"type": "filename", "pattern": "*.pdf"}
    """

    def evaluate(self, condition, document, context):
        pattern = condition.get("pattern", "")
        if not pattern:
            return True

        if _is_consumable(document):
            name = document.original_file.name
        elif _is_document(document):
            name = document.original_filename or ""
        else:
            return SKIP

        return fnmatch(name.lower(), pattern.lower())

    def to_q(self, condition):
        pattern = condition.get("pattern", "")
        if not pattern:
            return Q()
        from fnmatch import translate as fnmatch_translate

        regex = fnmatch_translate(pattern).lstrip("^").rstrip("$")
        return Q(original_filename__iregex=regex)

    def validate(self, condition):
        if "pattern" not in condition:
            raise FilterExpressionValidationError(
                "filename requires 'pattern' field",
            )
        if not isinstance(condition["pattern"], str):
            raise FilterExpressionValidationError(
                "filename 'pattern' must be a string",
            )


# ---------------------------------------------------------------------------
# Path condition (for consumable documents)
# ---------------------------------------------------------------------------

@register_condition("path")
class PathHandler(ConditionHandler):
    """Match document file path using fnmatch glob pattern.

    Schema::

        {"type": "path", "pattern": "/invoices/*"}
    """

    def evaluate(self, condition, document, context):
        pattern = condition.get("pattern", "")
        if not pattern:
            return True

        if _is_consumable(document):
            path = str(
                document.original_path
                if document.original_path is not None
                else document.original_file,
            )
            return fnmatch(path, pattern)

        # For saved Documents, path is not directly available
        return SKIP

    def to_q(self, condition):
        # Path filtering only applies at consumption time
        return SKIP

    def validate(self, condition):
        if "pattern" not in condition:
            raise FilterExpressionValidationError(
                "path requires 'pattern' field",
            )


# ---------------------------------------------------------------------------
# Source condition (for consumable documents)
# ---------------------------------------------------------------------------

@register_condition("source")
class SourceHandler(ConditionHandler):
    """Match document ingestion source.

    Schema::

        {"type": "source", "values": ["consume_folder", "api_upload"]}

    Valid values: ``consume_folder``, ``api_upload``, ``mail_fetch``, ``web_ui``
    """

    SOURCE_MAP = {
        "consume_folder": 1,
        "api_upload": 2,
        "mail_fetch": 3,
        "web_ui": 4,
    }

    def evaluate(self, condition, document, context):
        if not _is_consumable(document):
            return SKIP

        values = condition.get("values", [])
        if not values:
            return True

        int_values = [self.SOURCE_MAP.get(v) for v in values]
        int_values = [v for v in int_values if v is not None]
        return document.source.value in int_values

    def to_q(self, condition):
        return SKIP

    def validate(self, condition):
        values = condition.get("values")
        if not isinstance(values, list):
            raise FilterExpressionValidationError(
                "source requires 'values' as a list",
            )
        for v in values:
            if v not in self.SOURCE_MAP:
                raise FilterExpressionValidationError(
                    f"Unknown source: {v!r}. Must be one of: {list(self.SOURCE_MAP.keys())}",
                )


# ---------------------------------------------------------------------------
# Mail rule condition (for consumable documents)
# ---------------------------------------------------------------------------

@register_condition("mail_rule")
class MailRuleHandler(ConditionHandler):
    """Match document mail rule ID.

    Schema::

        {"type": "mail_rule", "id": 1}
    """

    def evaluate(self, condition, document, context):
        if not _is_consumable(document):
            return SKIP

        rule_id = condition.get("id")
        if rule_id is None:
            return True
        return document.mailrule_id == rule_id

    def to_q(self, condition):
        return SKIP

    def validate(self, condition):
        if "id" not in condition:
            raise FilterExpressionValidationError(
                "mail_rule requires 'id' field",
            )
        if not isinstance(condition["id"], int):
            raise FilterExpressionValidationError(
                "mail_rule 'id' must be an integer",
            )


# ---------------------------------------------------------------------------
# ASN condition
# ---------------------------------------------------------------------------

@register_condition("asn")
class AsnHandler(ConditionHandler):
    """Match document archive serial number.

    Schema::

        {"type": "asn", "op": "gte", "value": 100}

    Ops: ``exact``, ``gt``, ``gte``, ``lt``, ``lte``, ``range``, ``isnull``
    """

    VALID_OPS = {"exact", "gt", "gte", "lt", "lte", "range", "isnull"}

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP

        op = condition.get("op", "exact")
        value = condition.get("value")
        actual = document.archive_serial_number
        return _compare(actual, op, value)

    def to_q(self, condition):
        op = condition.get("op", "exact")
        value = condition.get("value")
        return _compare_to_q("archive_serial_number", op, value)

    def validate(self, condition):
        op = condition.get("op", "exact")
        if op not in self.VALID_OPS:
            raise FilterExpressionValidationError(
                f"Unknown asn op: {op!r}",
            )
        if "value" not in condition:
            raise FilterExpressionValidationError(
                "asn requires 'value' field",
            )


# ---------------------------------------------------------------------------
# Date conditions (created, added, modified)
# ---------------------------------------------------------------------------

class _DateHandlerBase(ConditionHandler):
    """Base class for date field handlers."""

    field_name: ClassVar[str] = ""
    VALID_OPS = {"exact", "gt", "gte", "lt", "lte", "range", "isnull"}

    def _get_value(self, document: Any) -> Any:
        if _is_consumable(document):
            return SKIP
        return getattr(document, self.field_name, None)

    def evaluate(self, condition, document, context):
        actual = self._get_value(document)
        if isinstance(actual, _SKIP):
            return SKIP

        op = condition.get("op", "exact")
        value = condition.get("value")

        # Parse the comparison value
        if op != "isnull" and value is not None:
            value = _parse_date(value)
            # Normalize types for comparison
            if isinstance(actual, datetime.datetime) and isinstance(
                value, datetime.date,
            ) and not isinstance(value, datetime.datetime):
                value = datetime.datetime(
                    value.year, value.month, value.day, tzinfo=timezone.utc,
                )
            elif isinstance(actual, datetime.date) and not isinstance(
                actual, datetime.datetime,
            ) and isinstance(value, datetime.datetime):
                value = value.date()

        if op == "range" and value is not None:
            if isinstance(condition.get("value"), list) and len(condition["value"]) == 2:
                parsed = [_parse_date(v) for v in condition["value"]]
                value = parsed

        return _compare(actual, op, value)

    def to_q(self, condition):
        op = condition.get("op", "exact")
        value = condition.get("value")
        if op != "isnull" and value is not None:
            if op == "range" and isinstance(value, list):
                value = [_parse_date(v) for v in value]
            elif not isinstance(value, list):
                value = _parse_date(value)
        return _compare_to_q(self.field_name, op, value)

    def validate(self, condition):
        op = condition.get("op", "exact")
        if op not in self.VALID_OPS:
            raise FilterExpressionValidationError(
                f"Unknown {self.condition_type} op: {op!r}",
            )
        if "value" not in condition:
            raise FilterExpressionValidationError(
                f"{self.condition_type} requires 'value' field",
            )


@register_condition("created")
class CreatedHandler(_DateHandlerBase):
    """Match document created date.

    Schema::

        {"type": "created", "op": "gte", "value": "2024-01-01"}
    """

    field_name = "created"


@register_condition("added")
class AddedHandler(_DateHandlerBase):
    """Match document added datetime.

    Schema::

        {"type": "added", "op": "lte", "value": "2024-06-01"}
    """

    field_name = "added"


@register_condition("modified")
class ModifiedHandler(_DateHandlerBase):
    """Match document modified datetime.

    Schema::

        {"type": "modified", "op": "gte", "value": "2024-01-01T00:00:00"}
    """

    field_name = "modified"


# ---------------------------------------------------------------------------
# Page count condition
# ---------------------------------------------------------------------------

@register_condition("page_count")
class PageCountHandler(ConditionHandler):
    """Match document page count.

    Schema::

        {"type": "page_count", "op": "range", "value": [1, 10]}

    Ops: ``exact``, ``gt``, ``gte``, ``lt``, ``lte``, ``range``, ``isnull``
    """

    VALID_OPS = {"exact", "gt", "gte", "lt", "lte", "range", "isnull"}

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP

        op = condition.get("op", "exact")
        value = condition.get("value")
        actual = document.page_count
        return _compare(actual, op, value)

    def to_q(self, condition):
        op = condition.get("op", "exact")
        value = condition.get("value")
        return _compare_to_q("page_count", op, value)

    def validate(self, condition):
        op = condition.get("op", "exact")
        if op not in self.VALID_OPS:
            raise FilterExpressionValidationError(
                f"Unknown page_count op: {op!r}",
            )
        if "value" not in condition:
            raise FilterExpressionValidationError(
                "page_count requires 'value' field",
            )


# ---------------------------------------------------------------------------
# MIME type condition
# ---------------------------------------------------------------------------

@register_condition("mime_type")
class MimeTypeHandler(ConditionHandler):
    """Match document MIME type (substring match).

    Schema::

        {"type": "mime_type", "value": "application/pdf"}
    """

    def evaluate(self, condition, document, context):
        value = condition.get("value", "")
        if not value:
            return True

        if _is_consumable(document):
            mime = getattr(document, "mime_type", None) or ""
        elif _is_document(document):
            mime = document.mime_type or ""
        else:
            return SKIP

        return value.lower() in mime.lower()

    def to_q(self, condition):
        value = condition.get("value", "")
        if not value:
            return Q()
        return Q(mime_type__icontains=value)

    def validate(self, condition):
        if "value" not in condition:
            raise FilterExpressionValidationError(
                "mime_type requires 'value' field",
            )


# ---------------------------------------------------------------------------
# Custom field condition
# ---------------------------------------------------------------------------

@register_condition("custom_field")
class CustomFieldHandler(ConditionHandler):
    """Match a custom field value.

    Schema::

        {"type": "custom_field", "field": "invoice_number",
         "op": "icontains", "value": "123"}

    ``field`` may be a field name (str) or field ID (int).
    """

    VALID_OPS = {
        "exact",
        "in",
        "isnull",
        "exists",
        "icontains",
        "istartswith",
        "iendswith",
        "gt",
        "gte",
        "lt",
        "lte",
        "range",
        "contains",
    }

    def evaluate(self, condition, document, context):
        if _is_consumable(document):
            return SKIP

        field_ref = condition.get("field")
        op = condition.get("op", "exact")
        value = condition.get("value")

        from documents.models import CustomField, CustomFieldInstance

        # Find the custom field definition
        if isinstance(field_ref, int):
            try:
                cf = CustomField.objects.get(pk=field_ref)
            except CustomField.DoesNotExist:
                return False
        else:
            try:
                cf = CustomField.objects.get(name=field_ref)
            except CustomField.DoesNotExist:
                return False

        # Check existence
        if op == "exists":
            return CustomFieldInstance.objects.filter(
                document=document, field=cf,
            ).exists() == (value is not False and value is not None and value != 0)

        # Get the instance
        try:
            cfi = CustomFieldInstance.objects.get(document=document, field=cf)
        except CustomFieldInstance.DoesNotExist:
            if op == "isnull":
                return value is True or value == True  # noqa: E712
            return False

        value_field_name = CustomFieldInstance.get_value_field_name(cf.data_type)
        actual = getattr(cfi, value_field_name, None)

        return _compare(actual, op, value)

    def to_q(self, condition):
        # Custom field queries are complex; delegate to the existing
        # CustomFieldQueryParser for SQL translation.
        field_ref = condition.get("field")
        op = condition.get("op", "exact")
        value = condition.get("value")

        try:
            from documents.filters import CustomFieldQueryParser

            atom = [field_ref, op, value]
            parser = CustomFieldQueryParser("filter_expression.custom_field")
            q, _ = parser.parse(json.dumps(atom))
            return q
        except Exception:
            return SKIP

    def validate(self, condition):
        if "field" not in condition:
            raise FilterExpressionValidationError(
                "custom_field requires 'field' field",
            )
        op = condition.get("op", "exact")
        if op not in self.VALID_OPS:
            raise FilterExpressionValidationError(
                f"Unknown custom_field op: {op!r}",
            )



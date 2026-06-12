"""
Built-in consumption plugin registrations.

This module defines the default plugin chain for the document consumption
pipeline. The registrations here replace the previously hard-coded list
in ``consume_file``.

Execution order (default chain)
-------------------------------
1. preflight          — file existence, duplicate detection, scratch dirs
2. asn_check_pre      — ASN validation before barcode processing
3. collate            — double-sided scan collation
4. barcode            — barcode detection and document splitting
5. asn_check_post     — ASN re-validation after barcode reading
6. workflow_trigger   — pre-consumption workflow execution
7. consumer           — document parsing, storage, post-consume hooks

Execution order (version_upload chain)
--------------------------------------
1. preflight          — same as above
2. consumer_version   — parsing + storage (no barcode/workflow processing)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documents.plugins.registry import ConsumePluginRegistry

logger = logging.getLogger("paperless.plugins")


def register_builtin_consume_plugins(registry: ConsumePluginRegistry) -> None:
    """Register all built-in consumption plugins on the given registry.

    This function is called once during registry initialisation. It imports
    the concrete plugin classes and registers them with explicit dependency
    declarations and ordering hints.

    Parameters
    ----------
    registry:
        The ``ConsumePluginRegistry`` instance to populate.
    """
    from documents.barcodes import BarcodePlugin
    from documents.consumer import AsnCheckPlugin
    from documents.consumer import ConsumerPlugin
    from documents.consumer import ConsumerPreflightPlugin
    from documents.consumer import WorkflowTriggerPlugin
    from documents.double_sided import CollatePlugin

    # ------------------------------------------------------------------
    # Shared plugins (both chains)
    # ------------------------------------------------------------------

    registry.register(
        ConsumerPreflightPlugin,
        name="preflight",
        order_hint=10,
        chain="both",
    )

    # ------------------------------------------------------------------
    # Default chain: full consumption pipeline
    # ------------------------------------------------------------------

    registry.register(
        AsnCheckPlugin,
        name="asn_check_pre",
        dependencies=["preflight"],
        order_hint=20,
        chain="default",
    )

    registry.register(
        CollatePlugin,
        name="collate",
        dependencies=["asn_check_pre"],
        order_hint=30,
        chain="default",
    )

    registry.register(
        BarcodePlugin,
        name="barcode",
        dependencies=["collate"],
        order_hint=40,
        chain="default",
    )

    registry.register(
        AsnCheckPlugin,
        name="asn_check_post",
        dependencies=["barcode"],
        order_hint=50,
        chain="default",
    )

    registry.register(
        WorkflowTriggerPlugin,
        name="workflow_trigger",
        dependencies=["asn_check_post"],
        order_hint=60,
        chain="default",
    )

    registry.register(
        ConsumerPlugin,
        name="consumer",
        dependencies=["workflow_trigger"],
        order_hint=70,
        chain="default",
    )

    # ------------------------------------------------------------------
    # Version upload chain: lightweight path
    # ------------------------------------------------------------------

    registry.register(
        ConsumerPlugin,
        name="consumer_version",
        dependencies=["preflight"],
        order_hint=20,
        chain="version_upload",
    )

    # ------------------------------------------------------------------
    # Apply runtime disable configuration
    # ------------------------------------------------------------------

    _apply_disabled_from_settings(registry)


def _apply_disabled_from_settings(registry: ConsumePluginRegistry) -> None:
    """Read ``CONSUMER_DISABLED_PLUGINS`` from Django settings and disable
    the listed plugins.

    The setting is expected to be a list of plugin name strings::

        CONSUMER_DISABLED_PLUGINS = ["collate", "barcode"]

    If the setting is absent or empty, no plugins are disabled.
    """
    try:
        from django.conf import settings

        disabled_names: list[str] = getattr(
            settings, "CONSUMER_DISABLED_PLUGINS", []
        )
    except Exception:
        # Django not configured (e.g. during standalone tests)
        return

    for name in disabled_names:
        if name in registry:
            registry.disable(name)
            logger.info(
                "Plugin %r disabled via CONSUMER_DISABLED_PLUGINS setting.",
                name,
            )
        else:
            logger.warning(
                "CONSUMER_DISABLED_PLUGINS references unknown plugin %r — "
                "ignoring.",
                name,
            )

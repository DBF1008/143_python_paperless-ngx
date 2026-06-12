"""
Document consumption plugin system.

Public API
----------
ConsumePluginRegistry
    The registry class that manages plugin registrations, dependencies,
    and execution chain resolution.

PluginRegistration
    Immutable data class representing a single registered plugin slot.

get_consume_plugin_registry
    Return the shared singleton registry instance.

reset_consume_plugin_registry
    Reset the singleton (tests only).

Exceptions
----------
PluginRegistryError
    Base exception for registry errors.

PluginDependencyError
    Raised on dependency resolution failures (cycles, missing deps).

PluginRegistrationError
    Raised on registration/unregistration failures.
"""

from documents.plugins.base import (
    AlwaysRunPluginMixin,
    ConsumeTaskPlugin,
    NoCleanupPluginMixin,
    NoSetupPluginMixin,
    StopConsumeTaskError,
)
from documents.plugins.registry import (
    ConsumePluginRegistry,
    PluginDependencyError,
    PluginRegistration,
    PluginRegistrationError,
    PluginRegistryError,
    get_consume_plugin_registry,
    reset_consume_plugin_registry,
)

__all__ = [
    # Base classes
    "ConsumeTaskPlugin",
    "AlwaysRunPluginMixin",
    "NoSetupPluginMixin",
    "NoCleanupPluginMixin",
    "StopConsumeTaskError",
    # Registry
    "ConsumePluginRegistry",
    "PluginRegistration",
    "get_consume_plugin_registry",
    "reset_consume_plugin_registry",
    # Exceptions
    "PluginRegistryError",
    "PluginDependencyError",
    "PluginRegistrationError",
]

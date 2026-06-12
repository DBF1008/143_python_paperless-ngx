"""
Configurable plugin registry for the document consumption pipeline.

Replaces the previously hard-coded plugin chain in ``consume_file`` with a
dynamic, dependency-aware execution chain that supports:

- Runtime enable/disable of individual plugins
- Explicit dependency declarations between plugins
- Topological ordering with stable fallback via ``order_hint``
- Two pre-defined chains: ``"default"`` (full consumption) and
  ``"version_upload"`` (lightweight version upload path)

Entrypoint
----------
get_consume_plugin_registry
    Lazy-initialise and return the shared ``ConsumePluginRegistry`` singleton.

reset_consume_plugin_registry
    Reset module-level state. For tests only.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documents.plugins.base import ConsumeTaskPlugin

logger = logging.getLogger("paperless.plugins")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PluginRegistryError(Exception):
    """Base exception for plugin registry errors."""


class PluginDependencyError(PluginRegistryError):
    """Raised when dependency resolution fails (cycle or missing dep)."""


class PluginRegistrationError(PluginRegistryError):
    """Raised when registration or unregistration fails."""


# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------

_registry: ConsumePluginRegistry | None = None
_lock = threading.Lock()


def get_consume_plugin_registry() -> ConsumePluginRegistry:
    """Return the shared ``ConsumePluginRegistry`` singleton.

    On the first call, creates the registry and registers all built-in
    consumption plugins. Subsequent calls return the same instance.
    """
    global _registry

    with _lock:
        if _registry is None:
            from documents.plugins.defaults import (
                register_builtin_consume_plugins,
            )

            r = ConsumePluginRegistry()
            register_builtin_consume_plugins(r)
            _registry = r

    return _registry


def reset_consume_plugin_registry() -> None:
    """Reset the module-level registry to its initial state.

    **FOR TESTS ONLY.**
    """
    global _registry

    _registry = None


# ---------------------------------------------------------------------------
# Plugin registration data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PluginRegistration:
    """Immutable record describing one registered plugin slot."""

    plugin_class: type[ConsumeTaskPlugin]
    name: str
    dependencies: tuple[str, ...] = ()
    order_hint: int = 0
    chain: str = "both"  # "default" | "version_upload" | "both"


# ---------------------------------------------------------------------------
# Registry class
# ---------------------------------------------------------------------------


class ConsumePluginRegistry:
    """Registry that manages consumption pipeline plugins.

    Plugins are registered with a unique ``name``, optional ``dependencies``
    (other plugin names this plugin depends on), an ``order_hint`` for stable
    ordering among peers, and a ``chain`` indicating which execution chain(s)
    the plugin belongs to.

    The same plugin *class* may be registered under different names with
    different dependencies — this is how the double ``AsnCheckPlugin``
    invocation is modelled.
    """

    # Valid chain names
    VALID_CHAINS = frozenset({"default", "version_upload", "both"})

    def __init__(self) -> None:
        self._plugins: dict[str, PluginRegistration] = {}
        self._disabled: set[str] = set()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        plugin_class: type[ConsumeTaskPlugin],
        *,
        name: str,
        dependencies: list[str] | None = None,
        order_hint: int = 0,
        chain: str = "both",
    ) -> None:
        """Register a plugin class under the given ``name``.

        Parameters
        ----------
        plugin_class:
            The plugin class to register.
        name:
            Unique identifier for this registration slot.
        dependencies:
            Names of other plugins this plugin depends on.
        order_hint:
            Ordering weight for stable topological sort (lower = earlier).
        chain:
            Which execution chain(s) this registration belongs to.
            One of ``"default"``, ``"version_upload"``, or ``"both"``.

        Raises
        ------
        PluginRegistrationError
            If ``name`` is already registered or ``chain`` is invalid.
        """
        if chain not in self.VALID_CHAINS:
            msg = (
                f"Invalid chain {chain!r} for plugin {name!r}. "
                f"Must be one of {sorted(self.VALID_CHAINS)}."
            )
            raise PluginRegistrationError(msg)

        if name in self._plugins:
            msg = (
                f"Plugin {name!r} is already registered. "
                f"Unregister it first or use a different name."
            )
            raise PluginRegistrationError(msg)

        self._plugins[name] = PluginRegistration(
            plugin_class=plugin_class,
            name=name,
            dependencies=tuple(dependencies or []),
            order_hint=order_hint,
            chain=chain,
        )
        logger.debug("Registered consume plugin %r (chain=%s)", name, chain)

    def unregister(self, name: str) -> None:
        """Remove a plugin registration.

        Raises
        ------
        PluginRegistrationError
            If ``name`` is not currently registered.
        """
        if name not in self._plugins:
            msg = f"Plugin {name!r} is not registered."
            raise PluginRegistrationError(msg)

        del self._plugins[name]
        self._disabled.discard(name)
        logger.debug("Unregistered consume plugin %r", name)

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    def disable(self, name: str) -> None:
        """Disable a registered plugin by name.

        Disabled plugins (and their dependents) will be excluded from
        execution chains.  Disabling an already-disabled plugin is a no-op.

        Raises
        ------
        PluginRegistrationError
            If ``name`` is not registered.
        """
        if name not in self._plugins:
            msg = f"Cannot disable unknown plugin {name!r}."
            raise PluginRegistrationError(msg)

        if name not in self._disabled:
            self._disabled.add(name)
            logger.info("Disabled consume plugin %r", name)

    def enable(self, name: str) -> None:
        """Re-enable a previously disabled plugin.

        Raises
        ------
        PluginRegistrationError
            If ``name`` is not registered.
        """
        if name not in self._plugins:
            msg = f"Cannot enable unknown plugin {name!r}."
            raise PluginRegistrationError(msg)

        if name in self._disabled:
            self._disabled.discard(name)
            logger.info("Enabled consume plugin %r", name)

    @property
    def disabled_plugins(self) -> frozenset[str]:
        """Return the set of currently disabled plugin names."""
        return frozenset(self._disabled)

    # ------------------------------------------------------------------
    # Execution chain resolution
    # ------------------------------------------------------------------

    def get_execution_chain(
        self,
        chain_name: str,
    ) -> list[type[ConsumeTaskPlugin]]:
        """Return an ordered list of plugin classes for the given chain.

        The returned list is topologically sorted respecting declared
        dependencies, with ties broken by ``order_hint`` (ascending).
        Disabled plugins and their transitive dependents are excluded.

        Parameters
        ----------
        chain_name:
            ``"default"`` or ``"version_upload"``.

        Raises
        ------
        PluginDependencyError
            If a cycle is detected or a dependency is missing.
        """
        if chain_name not in ("default", "version_upload"):
            msg = (
                f"Unknown chain {chain_name!r}. "
                f"Must be 'default' or 'version_upload'."
            )
            raise PluginRegistryError(msg)

        resolved = self._resolve(chain_name)
        return [entry.plugin_class for entry in resolved]

    def _resolve(self, chain_name: str) -> list[PluginRegistration]:
        """Topological sort (Kahn's algorithm) for the given chain.

        Disabled plugins are excluded, and any plugin whose (transitive)
        dependency is disabled is also excluded (cascade disable).
        """
        # 1. Collect eligible registrations for this chain
        eligible: dict[str, PluginRegistration] = {}
        for name, reg in self._plugins.items():
            if reg.chain in (chain_name, "both"):
                eligible[name] = reg

        # 2. Compute cascade-disabled set: any plugin that transitively
        #    depends on a disabled plugin.
        cascade_disabled: set[str] = set()
        # Build adjacency: name -> set of names that depend on it
        dependents: dict[str, set[str]] = {n: set() for n in eligible}
        for name, reg in eligible.items():
            for dep in reg.dependencies:
                if dep in dependents:
                    dependents[dep].add(name)

        # BFS from directly-disabled plugins
        queue: deque[str] = deque()
        for name in self._disabled:
            if name in eligible:
                queue.append(name)
                cascade_disabled.add(name)

        while queue:
            current = queue.popleft()
            for dependent in dependents.get(current, set()):
                if dependent not in cascade_disabled:
                    cascade_disabled.add(dependent)
                    queue.append(dependent)

        # Warn about cascade-disabled plugins
        for name in cascade_disabled:
            if name not in self._disabled:
                logger.warning(
                    "Plugin %r is cascade-disabled because a dependency "
                    "is disabled.",
                    name,
                )

        # 3. Build subgraph excluding disabled plugins
        active: dict[str, PluginRegistration] = {
            name: reg
            for name, reg in eligible.items()
            if name not in cascade_disabled
        }

        # 4. Validate dependencies and build in-degree map
        in_degree: dict[str, int] = {name: 0 for name in active}
        adjacency: dict[str, list[str]] = {name: [] for name in active}

        for name, reg in active.items():
            for dep in reg.dependencies:
                if dep not in active:
                    # Dependency might be disabled or in a different chain
                    if dep in self._disabled or dep in cascade_disabled:
                        # This plugin should have been cascade-disabled too
                        # (handled above), but if not, flag it
                        continue
                    msg = (
                        f"Plugin {name!r} depends on {dep!r} which is not "
                        f"registered in chain {chain_name!r}."
                    )
                    raise PluginDependencyError(msg)
                in_degree[name] += 1
                adjacency[dep].append(name)

        # 5. Kahn's algorithm with order_hint-based priority
        #    Use a sorted insertion to maintain stable ordering.
        ready = sorted(
            [name for name, deg in in_degree.items() if deg == 0],
            key=lambda n: active[n].order_hint,
        )
        result: list[PluginRegistration] = []

        while ready:
            current = ready.pop(0)
            result.append(active[current])

            # Collect newly ready neighbours, sort by order_hint
            newly_ready: list[str] = []
            for neighbour in adjacency[current]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    newly_ready.append(neighbour)

            if newly_ready:
                newly_ready.sort(key=lambda n: active[n].order_hint)
                # Merge into ready list maintaining sort order
                merged: list[str] = []
                i, j = 0, 0
                while i < len(ready) and j < len(newly_ready):
                    if active[ready[i]].order_hint <= active[newly_ready[j]].order_hint:
                        merged.append(ready[i])
                        i += 1
                    else:
                        merged.append(newly_ready[j])
                        j += 1
                merged.extend(ready[i:])
                merged.extend(newly_ready[j:])
                ready = merged

        # 6. Cycle detection
        if len(result) != len(active):
            remaining = set(active) - {r.name for r in result}
            msg = (
                f"Circular dependency detected among plugins: "
                f"{sorted(remaining)}"
            )
            raise PluginDependencyError(msg)

        return result

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def all_registrations(self) -> list[PluginRegistration]:
        """Return all registered plugin entries (insertion order)."""
        return list(self._plugins.values())

    def get_registration(self, name: str) -> PluginRegistration | None:
        """Return the registration for ``name``, or None."""
        return self._plugins.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._plugins

    def __len__(self) -> int:
        return len(self._plugins)

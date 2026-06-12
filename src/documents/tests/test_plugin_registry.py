"""
Tests for the consumption plugin registry system.

Covers:
- Registration / unregistration / duplicate registration
- Dependency resolution and topological ordering
- Cycle detection
- Enable / disable with cascade
- Default vs version_upload chains
- Missing dependency detection
- CONSUMER_DISABLED_PLUGINS setting integration
"""

import logging
from pathlib import Path
from unittest import mock

import pytest

from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.plugins.base import AlwaysRunPluginMixin
from documents.plugins.base import ConsumeTaskPlugin
from documents.plugins.base import NoCleanupPluginMixin
from documents.plugins.base import NoSetupPluginMixin
from documents.plugins.helpers import ProgressManager
from documents.plugins.registry import (
    ConsumePluginRegistry,
    PluginDependencyError,
    PluginRegistration,
    PluginRegistrationError,
    PluginRegistryError,
    get_consume_plugin_registry,
    reset_consume_plugin_registry,
)


# ---------------------------------------------------------------------------
# Test plugin stubs
# ---------------------------------------------------------------------------


class _StubPluginA(
    AlwaysRunPluginMixin,
    NoSetupPluginMixin,
    NoCleanupPluginMixin,
    ConsumeTaskPlugin,
):
    NAME = "StubPluginA"


class _StubPluginB(
    AlwaysRunPluginMixin,
    NoSetupPluginMixin,
    NoCleanupPluginMixin,
    ConsumeTaskPlugin,
):
    NAME = "StubPluginB"


class _StubPluginC(
    AlwaysRunPluginMixin,
    NoSetupPluginMixin,
    NoCleanupPluginMixin,
    ConsumeTaskPlugin,
):
    NAME = "StubPluginC"


class _StubPluginD(
    AlwaysRunPluginMixin,
    NoSetupPluginMixin,
    NoCleanupPluginMixin,
    ConsumeTaskPlugin,
):
    NAME = "StubPluginD"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the singleton registry before and after each test."""
    reset_consume_plugin_registry()
    yield
    reset_consume_plugin_registry()


@pytest.fixture()
def registry() -> ConsumePluginRegistry:
    """Return a fresh, empty registry instance."""
    return ConsumePluginRegistry()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_single_plugin(self, registry: ConsumePluginRegistry):
        registry.register(_StubPluginA, name="a")
        assert "a" in registry
        assert len(registry) == 1

    def test_register_multiple_plugins(self, registry: ConsumePluginRegistry):
        registry.register(_StubPluginA, name="a")
        registry.register(_StubPluginB, name="b")
        assert len(registry) == 2

    def test_register_same_class_different_names(
        self, registry: ConsumePluginRegistry
    ):
        """The same class can be registered under different names."""
        registry.register(_StubPluginA, name="a1")
        registry.register(_StubPluginA, name="a2")
        assert len(registry) == 2

    def test_duplicate_name_raises(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a")
        with pytest.raises(PluginRegistrationError, match="already registered"):
            registry.register(_StubPluginB, name="a")

    def test_invalid_chain_raises(
        self, registry: ConsumePluginRegistry
    ):
        with pytest.raises(PluginRegistrationError, match="Invalid chain"):
            registry.register(_StubPluginA, name="a", chain="bogus")

    def test_unregister(self, registry: ConsumePluginRegistry):
        registry.register(_StubPluginA, name="a")
        registry.unregister("a")
        assert "a" not in registry
        assert len(registry) == 0

    def test_unregister_unknown_raises(
        self, registry: ConsumePluginRegistry
    ):
        with pytest.raises(PluginRegistrationError, match="not registered"):
            registry.unregister("nonexistent")

    def test_register_with_dependencies(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a")
        registry.register(_StubPluginB, name="b", dependencies=["a"])
        reg = registry.get_registration("b")
        assert reg is not None
        assert reg.dependencies == ("a",)

    def test_all_registrations_returns_insertion_order(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a")
        registry.register(_StubPluginB, name="b")
        registry.register(_StubPluginC, name="c")
        names = [r.name for r in registry.all_registrations()]
        assert names == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Topological sort / execution chain tests
# ---------------------------------------------------------------------------


class TestExecutionChain:
    def test_empty_chain(self, registry: ConsumePluginRegistry):
        chain = registry.get_execution_chain("default")
        assert chain == []

    def test_single_plugin_no_deps(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a")
        chain = registry.get_execution_chain("default")
        assert chain == [_StubPluginA]

    def test_linear_dependency_chain(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a", order_hint=10)
        registry.register(
            _StubPluginB, name="b", dependencies=["a"], order_hint=20
        )
        registry.register(
            _StubPluginC, name="c", dependencies=["b"], order_hint=30
        )
        chain = registry.get_execution_chain("default")
        assert chain == [_StubPluginA, _StubPluginB, _StubPluginC]

    def test_diamond_dependency(
        self, registry: ConsumePluginRegistry
    ):
        """A -> B, A -> C, B -> D, C -> D"""
        registry.register(_StubPluginA, name="a", order_hint=10)
        registry.register(
            _StubPluginB, name="b", dependencies=["a"], order_hint=20
        )
        registry.register(
            _StubPluginC, name="c", dependencies=["a"], order_hint=30
        )
        registry.register(
            _StubPluginD, name="d", dependencies=["b", "c"], order_hint=40
        )
        chain = registry.get_execution_chain("default")
        # a first, then b before c (order_hint), then d
        assert chain == [_StubPluginA, _StubPluginB, _StubPluginC, _StubPluginD]

    def test_order_hint_breaks_ties(
        self, registry: ConsumePluginRegistry
    ):
        """Plugins with no dependencies are ordered by order_hint."""
        registry.register(_StubPluginC, name="c", order_hint=30)
        registry.register(_StubPluginA, name="a", order_hint=10)
        registry.register(_StubPluginB, name="b", order_hint=20)
        chain = registry.get_execution_chain("default")
        assert chain == [_StubPluginA, _StubPluginB, _StubPluginC]

    def test_chain_filtering_default(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a", chain="default")
        registry.register(_StubPluginB, name="b", chain="version_upload")
        registry.register(_StubPluginC, name="c", chain="both")
        default_chain = registry.get_execution_chain("default")
        assert default_chain == [_StubPluginA, _StubPluginC]

    def test_chain_filtering_version_upload(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a", chain="default")
        registry.register(_StubPluginB, name="b", chain="version_upload")
        registry.register(_StubPluginC, name="c", chain="both")
        vu_chain = registry.get_execution_chain("version_upload")
        assert vu_chain == [_StubPluginB, _StubPluginC]

    def test_invalid_chain_name_raises(
        self, registry: ConsumePluginRegistry
    ):
        with pytest.raises(PluginRegistryError, match="Unknown chain"):
            registry.get_execution_chain("nonexistent")


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    def test_direct_cycle(self, registry: ConsumePluginRegistry):
        registry.register(
            _StubPluginA, name="a", dependencies=["b"]
        )
        registry.register(
            _StubPluginB, name="b", dependencies=["a"]
        )
        with pytest.raises(PluginDependencyError, match="Circular dependency"):
            registry.get_execution_chain("default")

    def test_indirect_cycle(self, registry: ConsumePluginRegistry):
        registry.register(
            _StubPluginA, name="a", dependencies=["c"]
        )
        registry.register(
            _StubPluginB, name="b", dependencies=["a"]
        )
        registry.register(
            _StubPluginC, name="c", dependencies=["b"]
        )
        with pytest.raises(PluginDependencyError, match="Circular dependency"):
            registry.get_execution_chain("default")

    def test_self_cycle(self, registry: ConsumePluginRegistry):
        registry.register(
            _StubPluginA, name="a", dependencies=["a"]
        )
        with pytest.raises(PluginDependencyError, match="Circular dependency"):
            registry.get_execution_chain("default")


# ---------------------------------------------------------------------------
# Missing dependency detection
# ---------------------------------------------------------------------------


class TestMissingDependency:
    def test_missing_dep_raises(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(
            _StubPluginA, name="a", dependencies=["nonexistent"]
        )
        with pytest.raises(PluginDependencyError, match="not registered"):
            registry.get_execution_chain("default")

    def test_dep_in_different_chain_raises(
        self, registry: ConsumePluginRegistry
    ):
        """If a dependency exists only in a different chain, it's missing."""
        registry.register(
            _StubPluginA, name="a", chain="version_upload"
        )
        registry.register(
            _StubPluginB,
            name="b",
            dependencies=["a"],
            chain="default",
        )
        with pytest.raises(PluginDependencyError, match="not registered"):
            registry.get_execution_chain("default")


# ---------------------------------------------------------------------------
# Enable / Disable tests
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_disable_plugin(self, registry: ConsumePluginRegistry):
        registry.register(_StubPluginA, name="a")
        registry.disable("a")
        assert "a" in registry.disabled_plugins
        chain = registry.get_execution_chain("default")
        assert chain == []

    def test_enable_plugin(self, registry: ConsumePluginRegistry):
        registry.register(_StubPluginA, name="a")
        registry.disable("a")
        registry.enable("a")
        assert "a" not in registry.disabled_plugins
        chain = registry.get_execution_chain("default")
        assert chain == [_StubPluginA]

    def test_disable_unknown_raises(
        self, registry: ConsumePluginRegistry
    ):
        with pytest.raises(PluginRegistrationError, match="unknown plugin"):
            registry.disable("nonexistent")

    def test_enable_unknown_raises(
        self, registry: ConsumePluginRegistry
    ):
        with pytest.raises(PluginRegistrationError, match="unknown plugin"):
            registry.enable("nonexistent")

    def test_cascade_disable(
        self, registry: ConsumePluginRegistry
    ):
        """Disabling a plugin cascades to its dependents."""
        registry.register(_StubPluginA, name="a", order_hint=10)
        registry.register(
            _StubPluginB, name="b", dependencies=["a"], order_hint=20
        )
        registry.register(
            _StubPluginC, name="c", dependencies=["b"], order_hint=30
        )
        registry.disable("a")
        chain = registry.get_execution_chain("default")
        assert chain == []

    def test_cascade_disable_logs_warning(
        self, registry: ConsumePluginRegistry, caplog
    ):
        registry.register(_StubPluginA, name="a")
        registry.register(
            _StubPluginB, name="b", dependencies=["a"]
        )
        registry.disable("a")
        with caplog.at_level(logging.WARNING, logger="paperless.plugins"):
            registry.get_execution_chain("default")
        assert "cascade-disabled" in caplog.text

    def test_partial_disable_preserves_independent_plugins(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a", order_hint=10)
        registry.register(_StubPluginB, name="b", order_hint=20)
        registry.register(_StubPluginC, name="c", order_hint=30)
        registry.disable("b")
        chain = registry.get_execution_chain("default")
        assert chain == [_StubPluginA, _StubPluginC]

    def test_disable_double_is_noop(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a")
        registry.disable("a")
        registry.disable("a")  # should not raise
        assert "a" in registry.disabled_plugins

    def test_unregister_clears_disabled(
        self, registry: ConsumePluginRegistry
    ):
        registry.register(_StubPluginA, name="a")
        registry.disable("a")
        registry.unregister("a")
        assert "a" not in registry.disabled_plugins


# ---------------------------------------------------------------------------
# Singleton / get_consume_plugin_registry tests
# ---------------------------------------------------------------------------


class TestSingleton:
    @mock.patch("documents.plugins.defaults._apply_disabled_from_settings")
    def test_get_returns_same_instance(
        self, mock_apply
    ):
        r1 = get_consume_plugin_registry()
        r2 = get_consume_plugin_registry()
        assert r1 is r2

    @mock.patch("documents.plugins.defaults._apply_disabled_from_settings")
    def test_reset_creates_new_instance(
        self, mock_apply
    ):
        r1 = get_consume_plugin_registry()
        reset_consume_plugin_registry()
        r2 = get_consume_plugin_registry()
        assert r1 is not r2


# ---------------------------------------------------------------------------
# Default chain ordering (integration-level, verifies builtin registration)
# ---------------------------------------------------------------------------


class TestBuiltinChain:
    """Verify the built-in plugin chain matches the expected execution order."""

    @mock.patch("documents.plugins.defaults._apply_disabled_from_settings")
    def test_default_chain_order(
        self, mock_apply
    ):
        registry = get_consume_plugin_registry()
        chain = registry.get_execution_chain("default")
        # Extract class names for readability
        names = [cls.__name__ for cls in chain]
        assert names == [
            "ConsumerPreflightPlugin",
            "AsnCheckPlugin",
            "CollatePlugin",
            "BarcodePlugin",
            "AsnCheckPlugin",
            "WorkflowTriggerPlugin",
            "ConsumerPlugin",
        ]

    @mock.patch("documents.plugins.defaults._apply_disabled_from_settings")
    def test_version_upload_chain_order(
        self, mock_apply
    ):
        registry = get_consume_plugin_registry()
        chain = registry.get_execution_chain("version_upload")
        names = [cls.__name__ for cls in chain]
        assert names == [
            "ConsumerPreflightPlugin",
            "ConsumerPlugin",
        ]

    @mock.patch("documents.plugins.defaults._apply_disabled_from_settings")
    def test_builtin_registration_count(
        self, mock_apply
    ):
        registry = get_consume_plugin_registry()
        # preflight + 6 default-only + 1 version-only = 8 registrations
        assert len(registry) == 8


# ---------------------------------------------------------------------------
# CONSUMER_DISABLED_PLUGINS setting integration
# ---------------------------------------------------------------------------


class TestDisabledPluginsSetting:
    def test_setting_disables_plugins(self):
        """CONSUMER_DISABLED_PLUGINS setting disables listed plugins."""
        reset_consume_plugin_registry()
        with mock.patch(
            "documents.plugins.defaults.settings"
        ) as mock_settings:
            mock_settings.CONSUMER_DISABLED_PLUGINS = ["collate"]
            from documents.plugins.defaults import (
                _apply_disabled_from_settings,
            )

            registry = ConsumePluginRegistry()
            # Register a minimal set to test
            registry.register(_StubPluginA, name="collate")
            registry.register(_StubPluginB, name="other")
            _apply_disabled_from_settings(registry)
            assert "collate" in registry.disabled_plugins

    def test_unknown_plugin_in_setting_warns(self, caplog):
        reset_consume_plugin_registry()
        with mock.patch(
            "documents.plugins.defaults.settings"
        ) as mock_settings:
            mock_settings.CONSUMER_DISABLED_PLUGINS = ["nonexistent"]
            from documents.plugins.defaults import (
                _apply_disabled_from_settings,
            )

            registry = ConsumePluginRegistry()
            with caplog.at_level(logging.WARNING, logger="paperless.plugins"):
                _apply_disabled_from_settings(registry)
            assert "unknown plugin" in caplog.text


# ---------------------------------------------------------------------------
# PluginRegistration dataclass tests
# ---------------------------------------------------------------------------


class TestPluginRegistration:
    def test_frozen(self):
        reg = PluginRegistration(
            plugin_class=_StubPluginA, name="a"
        )
        with pytest.raises(AttributeError):
            reg.name = "b"  # type: ignore[misc]

    def test_defaults(self):
        reg = PluginRegistration(
            plugin_class=_StubPluginA, name="a"
        )
        assert reg.dependencies == ()
        assert reg.order_hint == 0
        assert reg.chain == "both"

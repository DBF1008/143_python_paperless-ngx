from types import SimpleNamespace

from django.test import SimpleTestCase
from django.test import override_settings

from documents.barcodes import BarcodePlugin
from documents.consumer import AsnCheckPlugin
from documents.consumer import ConsumerPlugin
from documents.consumer import ConsumerPreflightPlugin
from documents.consumer import WorkflowTriggerPlugin
from documents.double_sided import CollatePlugin
from documents.plugins.registry import PluginRegistry
from documents.tasks import consume_task_registry


def _doc(root_document_id=None):
    """
    Lightweight stand-in for a ConsumableDocument.

    build_pipeline only reads `root_document_id`, so this avoids constructing a real
    ConsumableDocument (which would require a file on disk for mime detection).
    """
    return SimpleNamespace(root_document_id=root_document_id)


def _plugin(name: str) -> type:
    """Create a throwaway plugin class with the given NAME (never instantiated)."""
    return type(f"Plugin_{name}", (), {"NAME": name})


class TestBuiltinConsumePipeline(SimpleTestCase):
    """
    Guards the refactor: the registry must reproduce the previously hardcoded chains
    exactly, including AsnCheckPlugin running twice (before and after the barcode step).
    """

    def test_full_pipeline_matches_legacy_order(self) -> None:
        pipeline = consume_task_registry.build_pipeline(_doc(root_document_id=None))

        self.assertEqual(
            [(step.name, step.plugin_class) for step in pipeline],
            [
                ("preflight", ConsumerPreflightPlugin),
                ("asn_check_pre", AsnCheckPlugin),
                ("collate", CollatePlugin),
                ("barcode", BarcodePlugin),
                ("asn_check_post", AsnCheckPlugin),
                ("workflow_trigger", WorkflowTriggerPlugin),
                ("consumer", ConsumerPlugin),
            ],
        )

    def test_child_pipeline_matches_legacy_order(self) -> None:
        pipeline = consume_task_registry.build_pipeline(_doc(root_document_id=5))

        self.assertEqual(
            [(step.name, step.plugin_class) for step in pipeline],
            [
                ("preflight", ConsumerPreflightPlugin),
                ("consumer", ConsumerPlugin),
            ],
        )

    @override_settings(CONSUMER_DISABLED_PLUGINS=["barcode"])
    def test_disabling_builtin_step_drops_it_but_keeps_dependents(self) -> None:
        names = [
            step.name
            for step in consume_task_registry.build_pipeline(
                _doc(root_document_id=None),
            )
        ]

        # barcode removed; asn_check_post (after=barcode) survives via the soft
        # dependency rule and stays in the right relative position.
        self.assertEqual(
            names,
            [
                "preflight",
                "asn_check_pre",
                "collate",
                "asn_check_post",
                "workflow_trigger",
                "consumer",
            ],
        )


class TestPluginRegistryMechanism(SimpleTestCase):
    def test_register_duplicate_name_raises(self) -> None:
        reg = PluginRegistry()
        reg.register("a", _plugin("A"))
        with self.assertRaises(ValueError):
            reg.register("a", _plugin("A2"))

    def test_unregister_removes_step(self) -> None:
        reg = PluginRegistry()
        reg.register("a", _plugin("A"))
        reg.unregister("a")
        self.assertNotIn("a", reg)
        with self.assertRaises(KeyError):
            reg.unregister("a")

    def test_topological_order_respects_dependencies(self) -> None:
        reg = PluginRegistry()
        reg.register("consumer", _plugin("C"), after=("preflight",))
        reg.register("preflight", _plugin("P"))

        names = [step.name for step in reg.build_pipeline(_doc())]
        self.assertEqual(names, ["preflight", "consumer"])

    def test_registration_order_breaks_ties(self) -> None:
        reg = PluginRegistry()
        reg.register("a", _plugin("A"))
        reg.register("b", _plugin("B"), after=("a",))
        reg.register("c", _plugin("C"), after=("a",))

        # b and c both become ready after a; registration order decides b before c.
        self.assertEqual(
            [step.name for step in reg.build_pipeline(_doc())],
            ["a", "b", "c"],
        )

        reg2 = PluginRegistry()
        reg2.register("a", _plugin("A"))
        reg2.register("c", _plugin("C"), after=("a",))
        reg2.register("b", _plugin("B"), after=("a",))
        self.assertEqual(
            [step.name for step in reg2.build_pipeline(_doc())],
            ["a", "c", "b"],
        )

    def test_cycle_raises(self) -> None:
        reg = PluginRegistry()
        reg.register("x", _plugin("X"), after=("y",))
        reg.register("y", _plugin("Y"), after=("x",))
        with self.assertRaises(ValueError):
            reg.build_pipeline(_doc())

    def test_condition_excludes_step(self) -> None:
        reg = PluginRegistry()
        reg.register("on", _plugin("ON"))
        reg.register("off", _plugin("OFF"), condition=lambda doc: False)

        self.assertEqual(
            [step.name for step in reg.build_pipeline(_doc())],
            ["on"],
        )

    def test_condition_receives_input_doc(self) -> None:
        reg = PluginRegistry()
        reg.register(
            "child_only",
            _plugin("CHILD"),
            condition=lambda doc: doc.root_document_id is not None,
        )
        reg.register("always", _plugin("ALWAYS"))

        self.assertEqual(
            [step.name for step in reg.build_pipeline(_doc(root_document_id=None))],
            ["always"],
        )
        self.assertEqual(
            {step.name for step in reg.build_pipeline(_doc(root_document_id=1))},
            {"child_only", "always"},
        )

    def test_dependency_on_excluded_step_is_ignored(self) -> None:
        reg = PluginRegistry()
        reg.register("base", _plugin("BASE"))
        reg.register("opt", _plugin("OPT"), condition=lambda doc: False)
        reg.register("dep", _plugin("DEP"), after=("opt", "base"))

        # opt is excluded; dep's dependency on it is treated as satisfied.
        self.assertEqual(
            [step.name for step in reg.build_pipeline(_doc())],
            ["base", "dep"],
        )

    @override_settings(CONSUMER_DISABLED_PLUGINS=["drop"])
    def test_disabled_setting_filters_steps(self) -> None:
        reg = PluginRegistry()
        reg.register("keep", _plugin("KEEP"))
        reg.register("drop", _plugin("DROP"))

        self.assertEqual(
            [step.name for step in reg.build_pipeline(_doc())],
            ["keep"],
        )

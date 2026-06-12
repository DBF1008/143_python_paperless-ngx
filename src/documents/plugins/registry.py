from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from django.conf import settings

# Forward-referenced types only (annotations are strings via `from __future__`), so this
# module performs no runtime import of concrete plugin classes and stays cycle-free.
if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable

    from documents.data_models import ConsumableDocument
    from documents.plugins.base import ConsumeTaskPlugin

logger = logging.getLogger("paperless.plugins")


@dataclass(frozen=True, slots=True)
class PipelineStep:
    """
    A single, ordered step in the document consume pipeline.

    The registration unit is a *step*, not a plugin class, so the same plugin class
    may be registered under several step names (e.g. an ASN check before and after
    barcode reading).
    """

    name: str
    plugin_class: type[ConsumeTaskPlugin]
    # Names of steps this step must run *after*. Used to derive the execution order
    # via a stable topological sort. A dependency on a step that is not part of the
    # current pipeline (excluded by condition or disabled) is ignored.
    after: tuple[str, ...] = ()
    # Decides whether this step is part of the pipeline for a given input document.
    # None means the step always applies. Note this is separate from the plugin's
    # own `able_to_run`, which decides whether an included step actually executes.
    condition: Callable[[ConsumableDocument], bool] | None = field(default=None)


class PluginRegistry:
    """
    Registry of consume-task pipeline steps.

    Replaces the previously hardcoded plugin chains in `consume_file`. Steps declare
    their dependencies explicitly; the execution order is derived by a stable
    topological sort (registration order breaks ties, so the order matches the legacy
    hardcoded chains exactly). Steps can be excluded per-document via `condition` and
    disabled by name via the `PAPERLESS_CONSUMER_DISABLED_PLUGINS` setting.
    """

    def __init__(self) -> None:
        # Insertion order is preserved and used as the topological-sort tie-breaker.
        self._steps: dict[str, PipelineStep] = {}

    def register(
        self,
        name: str,
        plugin_class: type[ConsumeTaskPlugin],
        *,
        after: Iterable[str] = (),
        condition: Callable[[ConsumableDocument], bool] | None = None,
    ) -> None:
        """
        Register a pipeline step. Raises ValueError if the name is already taken.
        """
        if name in self._steps:
            raise ValueError(
                f"A consume task plugin step named '{name}' is already registered",
            )
        self._steps[name] = PipelineStep(
            name=name,
            plugin_class=plugin_class,
            after=tuple(after),
            condition=condition,
        )

    def unregister(self, name: str) -> None:
        """
        Remove a previously registered step. Raises KeyError if it is not registered.
        """
        if name not in self._steps:
            raise KeyError(
                f"No consume task plugin step named '{name}' is registered",
            )
        del self._steps[name]

    def __contains__(self, name: object) -> bool:
        return name in self._steps

    @property
    def registered_names(self) -> list[str]:
        """Step names in registration order."""
        return list(self._steps)

    @staticmethod
    def _disabled_names() -> set[str]:
        return set(getattr(settings, "CONSUMER_DISABLED_PLUGINS", []) or [])

    def build_pipeline(self, input_doc: ConsumableDocument) -> list[PipelineStep]:
        """
        Build the ordered list of steps to run for this document.

        1. Exclude steps disabled by name or whose `condition` returns False.
        2. Topologically sort by `after`, ignoring dependencies on excluded steps,
           using registration order as a stable tie-breaker.
        3. Raise ValueError if the remaining dependencies contain a cycle.
        """
        disabled = self._disabled_names()

        included: dict[str, PipelineStep] = {}
        for name, step in self._steps.items():
            if name in disabled:
                logger.debug("Consume plugin step '%s' is disabled, skipping", name)
                continue
            if step.condition is not None and not step.condition(input_doc):
                continue
            included[name] = step

        # Build the dependency graph over included steps only (soft dependencies:
        # an `after` pointing at an excluded step is treated as already satisfied).
        order_index = {name: i for i, name in enumerate(included)}
        indegree: dict[str, int] = {}
        dependents: dict[str, list[str]] = {name: [] for name in included}
        for name, step in included.items():
            deps = [dep for dep in step.after if dep in included]
            indegree[name] = len(deps)
            for dep in deps:
                dependents[dep].append(name)

        # Kahn's algorithm; each round take the ready step with the lowest
        # registration index so the result is deterministic and matches the
        # legacy hardcoded order. The step count is tiny, so O(n^2) is fine.
        ready = [name for name in included if indegree[name] == 0]
        ordered: list[PipelineStep] = []
        while ready:
            ready.sort(key=lambda name: order_index[name])
            current = ready.pop(0)
            ordered.append(included[current])
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

        if len(ordered) != len(included):
            remaining = sorted(set(included) - {step.name for step in ordered})
            raise ValueError(
                "Cycle detected in consume task plugin dependencies among: "
                f"{remaining}",
            )

        return ordered


# Module-level singleton used by consume_file. Built-in steps are registered in
# documents.tasks (which already imports every plugin class). Additional plugins can
# be added from anywhere via consume_task_registry.register(...) without touching the
# consume_file control flow.
consume_task_registry = PluginRegistry()

"""Management command to check the document archive for issues."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from documents.management.commands.base import PaperlessCommand
from documents.models import Document
from documents.sanity_checker import DEFAULT_ORPHAN_GRACE_SECONDS
from documents.sanity_checker import OrphanAnalysis
from documents.sanity_checker import SanityCheckMessages
from documents.sanity_checker import check_sanity
from documents.utils import IterWrapper
from documents.utils import identity

if TYPE_CHECKING:
    from django.core.management import CommandParser

_LEVEL_STYLE: dict[int, tuple[str, str]] = {
    logging.ERROR: ("bold red", "ERROR"),
    logging.WARNING: ("yellow", "WARN"),
    logging.INFO: ("dim", "INFO"),
}


def _human_bytes(num: int) -> str:
    """Format a byte count as a human-readable string."""
    size = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


class Command(PaperlessCommand):
    help = "This command checks your document archive for issues."

    supports_progress_bar = True
    supports_multiprocessing = False

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--remove-orphans",
            default=False,
            action="store_true",
            help=(
                "Delete orphaned files found in the media directory. Deletion "
                "runs under the media lock and skips files modified within the "
                "grace period to avoid removing in-flight uploads."
            ),
        )
        parser.add_argument(
            "--orphan-grace-seconds",
            default=DEFAULT_ORPHAN_GRACE_SECONDS,
            type=int,
            help=(
                "When removing orphans, skip files modified within this many "
                f"seconds (default: {DEFAULT_ORPHAN_GRACE_SECONDS})."
            ),
        )

    def _render_results(self, messages: SanityCheckMessages) -> None:
        """Render sanity check results as a Rich table."""

        if (
            not messages.has_error
            and not messages.has_warning
            and not messages.has_info
        ):
            self.console.print(
                Panel(
                    "[green]No issues detected.[/green]",
                    title="Sanity Check",
                    border_style="green",
                ),
            )
            return

        # Build a lookup for document titles
        doc_pks = [pk for pk in messages.document_pks() if pk is not None]
        titles: dict[int, str] = {}
        if doc_pks:
            titles = dict(
                Document.global_objects.filter(pk__in=doc_pks)
                .only("pk", "title")
                .values_list("pk", "title"),
            )

        table = Table(
            title="Sanity Check Results",
            show_lines=True,
            title_style="bold",
        )
        table.add_column("Level", width=7, no_wrap=True)
        table.add_column("Document", min_width=20)
        table.add_column("Issue", ratio=1)

        for doc_pk, doc_messages in messages.iter_messages():
            if doc_pk is not None:
                title = titles.get(doc_pk, "Unknown")
                doc_label = f"#{doc_pk} {title}"
            else:
                doc_label = "(global)"

            for msg in doc_messages:
                style, label = _LEVEL_STYLE.get(
                    msg["level"],
                    ("dim", "INFO"),
                )
                table.add_row(
                    Text(label, style=style),
                    Text(doc_label),
                    Text(str(msg["message"])),
                )

        self.console.print(table)

        parts: list[str] = []

        if messages.document_error_count:
            parts.append(
                f"{messages.document_error_count} document(s) with [bold red]errors[/bold red]",
            )
        if messages.document_warning_count:
            parts.append(
                f"{messages.document_warning_count} document(s) with [yellow]warnings[/yellow]",
            )
        if messages.document_info_count:
            parts.append(f"{messages.document_info_count} document(s) with infos")
        if messages.global_warning_count:
            parts.append(
                f"{messages.global_warning_count} global [yellow]warning(s)[/yellow]",
            )

        if parts:
            if len(parts) > 1:
                summary = ", ".join(parts[:-1]) + " and " + parts[-1]
            else:
                summary = parts[0]
            self.console.print(f"\nFound {summary}.")
        else:
            self.console.print("\nNo issues found.")

    def _render_orphan_report(
        self,
        orphans: OrphanAnalysis,
        *,
        removed: bool,
    ) -> None:
        """Render the orphaned-file analysis (and cleanup outcome, if any)."""
        if orphans.orphan_count == 0:
            return

        table = Table(
            title="Orphaned Files",
            show_lines=False,
            title_style="bold",
            box=box.SIMPLE,
        )
        table.add_column("Metric", no_wrap=True)
        table.add_column("Value", ratio=1)

        table.add_row("Detected", str(orphans.orphan_count))
        if removed:
            table.add_row("Removed", str(orphans.removed_count))
            table.add_row(
                "Skipped (recently modified)",
                str(orphans.skipped_count),
            )
            table.add_row("Errors", str(orphans.error_count))
            table.add_row("Reclaimed", _human_bytes(orphans.reclaimed_bytes))

        self.console.print(table)

        if not removed:
            self.console.print(
                "\n[dim]Re-run with [bold]--remove-orphans[/bold] to delete "
                "these files. Deletion is performed under the media lock and "
                "skips files modified within the grace period.[/dim]",
            )
        elif orphans.error_count:
            self.console.print(
                f"\n[yellow]{orphans.error_count} orphaned file(s) could not "
                "be removed; see logs for details.[/yellow]",
            )

    def handle(self, *args: Any, **options: Any) -> None:
        remove_orphans: bool = options["remove_orphans"]
        grace_seconds: int = options["orphan_grace_seconds"]

        orphan_iter_wrapper: IterWrapper[Path] = (
            (lambda paths: self.track(paths, description="Removing orphans..."))
            if remove_orphans
            else identity
        )

        messages = check_sanity(
            iter_wrapper=lambda docs: self.track(
                docs,
                description="Checking documents...",
            ),
            remove_orphans=remove_orphans,
            orphan_grace_seconds=grace_seconds,
            orphan_iter_wrapper=orphan_iter_wrapper,
        )
        self._render_results(messages)
        self._render_orphan_report(messages.orphans, removed=remove_orphans)

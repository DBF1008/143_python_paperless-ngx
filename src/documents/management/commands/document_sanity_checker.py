"""Management command to check the document archive for issues."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from documents.management.commands.base import PaperlessCommand
from documents.models import Document
from documents.sanity_checker import OrphanSummary
from documents.sanity_checker import SanityCheckMessages
from documents.sanity_checker import _format_size
from documents.sanity_checker import check_sanity

if TYPE_CHECKING:
    from django.core.management import CommandParser


_LEVEL_STYLE: dict[int, tuple[str, str]] = {
    logging.ERROR: ("bold red", "ERROR"),
    logging.WARNING: ("yellow", "WARN"),
    logging.INFO: ("dim", "INFO"),
}

_PHASE_DESCRIPTIONS: dict[str, str] = {
    "scan_start": "Scanning media directory...",
    "documents_start": "Checking documents...",
    "orphans_start": "Analyzing orphan files...",
    "cleanup_start": "Cleaning up orphan files...",
}


class Command(PaperlessCommand):
    help = "This command checks your document archive for issues."

    supports_progress_bar = True
    supports_multiprocessing = False

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--delete-orphans",
            default=False,
            action="store_true",
            help="Delete orphaned files found in the media directory.",
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

    def _render_orphan_report(self, orphan_summary: OrphanSummary) -> None:
        """Render orphan analysis as a Rich panel with a detail table."""
        if not orphan_summary.has_orphans:
            return

        # Detail table
        table = Table(title="Orphaned Files", show_lines=True, title_style="bold")
        table.add_column("Path", ratio=2, no_wrap=False)
        table.add_column("Category", width=14)
        table.add_column("Size", width=10, justify="right")

        media_root = str(settings.MEDIA_ROOT)
        for orphan in orphan_summary.orphans:
            try:
                rel_path = str(orphan.path.relative_to(media_root))
            except ValueError:
                rel_path = str(orphan.path)
            table.add_row(
                Text(rel_path),
                Text(orphan.category, style="yellow"),
                Text(_format_size(orphan.size)),
            )

        self.console.print(table)

        # Category breakdown
        category_parts: list[str] = []
        for cat, stats in sorted(orphan_summary.by_category.items()):
            category_parts.append(
                f"  {cat}: {stats['count']} file(s), {_format_size(stats['size'])}",
            )

        if orphan_summary.cleaned_up:
            footer = (
                f"[green]Cleaned up {orphan_summary.total_count} orphaned file(s), "
                f"freed {_format_size(orphan_summary.freed_bytes)}.[/green]"
            )
        else:
            footer = (
                f"[yellow]Total: {orphan_summary.total_count} orphaned file(s), "
                f"{_format_size(orphan_summary.total_size)}.[/yellow]\n"
                f"Use [bold]--delete-orphans[/bold] to clean up."
            )

        self.console.print(
            Panel(
                "\n".join(category_parts) + "\n\n" + footer,
                title="Orphan Summary",
                border_style="yellow",
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        delete_orphans: bool = options.get("delete_orphans", False)

        # Confirm deletion if interactive
        if delete_orphans and sys.stdin.isatty():
            self.console.print(
                "[yellow]Orphaned files will be permanently deleted.[/yellow]",
            )
            response = input("Continue? [y/N] ")
            if response.lower() not in ("y", "yes"):
                self.console.print("Aborted.")
                return

        # Phase callback updates progress bar description
        if not self.no_progress_bar:
            progress = self._create_progress("Starting sanity check...")
            progress.start()
            task_id = progress.add_task("Starting...", total=None)

            def phase_callback(phase: str) -> None:
                desc = _PHASE_DESCRIPTIONS.get(phase)
                if desc:
                    progress.update(task_id, description=desc)

        else:
            progress = None

            def phase_callback(phase: str) -> None:
                pass

        try:
            messages = check_sanity(
                iter_wrapper=lambda docs: self.track(
                    docs,
                    description="Checking documents...",
                ),
                progress_callback=phase_callback,
                delete_orphans=delete_orphans,
            )
        finally:
            if progress:
                progress.stop()

        self._render_results(messages)

        # Render orphan report if any orphans were found
        orphan_summary: OrphanSummary | None = getattr(
            messages, "orphan_summary", None
        )
        if orphan_summary:
            self._render_orphan_report(orphan_summary)

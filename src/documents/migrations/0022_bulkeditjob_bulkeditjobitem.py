import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("documents", "0021_widen_workflow_integer_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="BulkEditJob",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("started", "Started"),
                            ("complete", "Complete"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "method",
                    models.CharField(
                        help_text="The bulk edit method name, e.g. 'set_correspondent'",
                        max_length=50,
                        verbose_name="Method",
                    ),
                ),
                (
                    "total_documents",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="Total documents",
                    ),
                ),
                (
                    "completed_documents",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="Completed documents",
                    ),
                ),
                (
                    "failed_documents",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="Failed documents",
                    ),
                ),
                (
                    "use_transaction",
                    models.BooleanField(
                        default=False,
                        help_text="Whether the entire operation is wrapped in a database transaction",
                        verbose_name="Use transaction",
                    ),
                ),
                (
                    "date_created",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        db_index=True,
                    ),
                ),
                (
                    "date_done",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                    ),
                ),
                (
                    "error_message",
                    models.TextField(
                        blank=True,
                        default="",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        default=None,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="owner",
                    ),
                ),
            ],
            options={
                "verbose_name": "bulk edit job",
                "verbose_name_plural": "bulk edit jobs",
                "ordering": ["-date_created"],
            },
        ),
        migrations.CreateModel(
            name="BulkEditJobItem",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("success", "Success"),
                            ("failure", "Failure"),
                            ("skipped", "Skipped"),
                            ("conflict", "Conflict"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "error_message",
                    models.TextField(
                        blank=True,
                        default="",
                    ),
                ),
                (
                    "date_done",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bulk_edit_items",
                        to="documents.document",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="documents.bulkeditjob",
                    ),
                ),
            ],
            options={
                "verbose_name": "bulk edit job item",
                "verbose_name_plural": "bulk edit job items",
                "unique_together": {("job", "document")},
            },
        ),
    ]

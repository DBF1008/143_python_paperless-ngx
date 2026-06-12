from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0021_widen_workflow_integer_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="correspondent",
            name="filter_expression",
            field=models.JSONField(
                blank=True,
                help_text=(
                    "JSON-encoded composable filter expression. "
                    "When set, takes priority over match/matching_algorithm fields."
                ),
                null=True,
                verbose_name="filter expression",
            ),
        ),
        migrations.AddField(
            model_name="documenttype",
            name="filter_expression",
            field=models.JSONField(
                blank=True,
                help_text=(
                    "JSON-encoded composable filter expression. "
                    "When set, takes priority over match/matching_algorithm fields."
                ),
                null=True,
                verbose_name="filter expression",
            ),
        ),
        migrations.AddField(
            model_name="storagepath",
            name="filter_expression",
            field=models.JSONField(
                blank=True,
                help_text=(
                    "JSON-encoded composable filter expression. "
                    "When set, takes priority over match/matching_algorithm fields."
                ),
                null=True,
                verbose_name="filter expression",
            ),
        ),
        migrations.AddField(
            model_name="tag",
            name="filter_expression",
            field=models.JSONField(
                blank=True,
                help_text=(
                    "JSON-encoded composable filter expression. "
                    "When set, takes priority over match/matching_algorithm fields."
                ),
                null=True,
                verbose_name="filter expression",
            ),
        ),
        migrations.AddField(
            model_name="workflowtrigger",
            name="filter_expression",
            field=models.JSONField(
                blank=True,
                help_text=(
                    "JSON-encoded composable filter expression. "
                    "When set, takes priority over flat filter fields."
                ),
                null=True,
                verbose_name="filter expression",
            ),
        ),
    ]

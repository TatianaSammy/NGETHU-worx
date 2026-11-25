from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0030_remove_committee_member_name"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chemicalbatch",
            name="unit",
            field=models.CharField(default="KG", max_length=10),
        ),
        migrations.AddField(
            model_name="chemicalbatch",
            name="package_weight",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                help_text="Supplier-declared quantity or net weight per package, verified during committee inspection.",
                max_digits=18,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="chemicalbatch",
            name="package_type",
            field=models.CharField(
                blank=True,
                choices=[("BAGS", "BAGS"), ("DRUMS", "DRUMS"), ("TINS", "TINS")],
                default="",
                max_length=8,
            ),
        ),
    ]

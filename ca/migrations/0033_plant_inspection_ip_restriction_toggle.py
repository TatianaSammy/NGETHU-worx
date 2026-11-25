from django.db import migrations, models
import ca.models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0032_alter_chemicalinspectioncommitteemember_options"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chemicalinspectionplantnetwork",
            name="lan_cidr",
            field=models.CharField(
                blank=True,
                help_text="Required while IP restriction is enabled. Enter the plant LAN in CIDR notation, for example 192.168.10.0/24.",
                max_length=64,
                validators=[ca.models.validate_inspection_lan_cidr],
            ),
        ),
        migrations.AddField(
            model_name="chemicalinspectionplantnetwork",
            name="ip_restriction_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When enabled, committee attendance acknowledgements count only from this plant's LAN.",
                verbose_name="Require plant LAN",
            ),
        ),
        migrations.AlterField(
            model_name="chemicalinspectionattendance",
            name="client_ip",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
    ]

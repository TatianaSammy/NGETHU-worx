from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('heavy_metal', '0003_result_entry_status'),
        ('sc', '0003_alter_shiftsupervisor_options_and_more'),
        ('t', '0003_filter_status_and_capacity'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='heavymetalanalysis',
            unique_together={('treatment_plant', 'treatment_plant_phase', 'analysis_date', 'sampling_time', 'shift')},
        ),
    ]

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('analysis_methods', '0003_chemical_method_rounding_mode'),
        ('microbial', '0003_result_entry_status'),
        ('sc', '0003_alter_shiftsupervisor_options_and_more'),
        ('t', '0003_filter_status_and_capacity'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='microbialanalysis',
            unique_together={('treatment_plant', 'treatment_plant_phase', 'method', 'analysis_date', 'sampling_time', 'shift')},
        ),
    ]

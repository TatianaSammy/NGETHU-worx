from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0008_alter_alertconfig_alert_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="alertconfig",
            name="alert_type",
            field=models.CharField(
                choices=[
                    ("nonconformity", "Nonconformity"),
                    ("auto_verification", "Automatic Verification"),
                    ("sampling_delay", "Sampling Delay"),
                    ("analysis_missed", "Analysis Missed"),
                    ("maintenance_due", "Maintenance Due"),
                    ("verification_pending", "Verification Pending"),
                    ("performance_deviation", "Performance Deviation"),
                    ("water_quality_limit", "Water Quality Treatment Limit"),
                    ("sensor_threshold", "Instrument Sensor Threshold"),
                    ("sensor_health", "Instrument Sensor Health"),
                    ("derived_metric", "Instrument Derived Metric"),
                    ("phase_anomaly", "Treatment Phase Anomaly"),
                    ("ca_assignment_pending", "CA analyst assignment pending"),
                    ("ca_assignment_escalation", "CA analyst assignment escalation"),
                    ("ca_qc_failure", "Chemical analysis method QC failure"),
                    ("ca_qc_drift", "Chemical analysis QC drift"),
                    ("ca_interlab_disagreement", "Chemical interlaboratory disagreement"),
                    ("ca_interlab_results_ready", "Chemical interlaboratory results ready"),
                    ("ca_oos_result", "Chemical analysis out-of-specification result"),
                    ("ca_instrument_unavailable", "Chemical laboratory instrument unavailable"),
                ],
                max_length=100,
            ),
        ),
    ]

from datetime import timedelta

from django.db.models import F, Q

APP_LABEL = "microbial"

DASHBOARD_ANALYSIS_SPECS = [
    {
        "key": "microbial-assay",
        "model": "MicrobialAnalysis",
        "title": "Microbial assay",
        "date_path": "analysis_date",
        "plant_path": "treatment_plant",
        "status_field": "state",
        "analysis_datetime_path": "created_at",
        "reference_path": "reference_id",
        "sample_title_path": "reference_id",
        "sample_fields": [
            ("Plant", "treatment_plant__name"),
            ("Treatment phase", "treatment_plant_phase__name"),
            ("Method", "method__name"),
            ("Sampling slot", "sampling_time__time"),
            ("Incubation ends", "incubation_end"),
        ],
        "select_related": ["treatment_plant", "treatment_plant_phase", "method", "shift", "sampling_time", "analysed_by"],
        "result_description": "saved microbial and treated-water values against their configured plant limits",
        "result_specs": [
            {
                "model": "MicrobialParameterResult",
                "title_prefix": "Microbial · ",
                "value_field": "value",
                "parameter_name_path": "parameter__parameter__name",
                "unit_path": "parameter__parameter__unit",
                "minimum_path": "parameter__min_value",
                "maximum_path": "parameter__max_value",
                "filters": {"entry_status": "recorded"},
                "method_path": "analysis__method__name",
                "reference_path": "analysis__method__kebs_reference",
            },
            {
                "model": "TreatedWaterParameterResult",
                "title_prefix": "Treated water · ",
                "value_field": "value",
                "parameter_name_path": "parameter__parameter__name",
                "unit_path": "parameter__parameter__unit",
                "minimum_path": "parameter__min_value",
                "maximum_path": "parameter__max_value",
                "include_filter": Q(manually_editable=False) | Q(updated_at__gt=F("created_at") + timedelta(seconds=2)),
                "method_path": "analysis__method__name",
                "reference_path": "analysis__method__kebs_reference",
                "allow_create": False,
                "read_only": True,
            },
        ],
    },
]

SCHEDULE_CONFIG = {
    "schedule_model": "MicrobialSamplingSchedule",
    "time_model": "MicrobialSamplingTime",
    "analysis_model": "MicrobialAnalysis",
    "result_generator": "microbial",
    "lookback_minutes": 722,
}

APP_LABEL = "heavy_metal"

DASHBOARD_ANALYSIS_SPECS = [
    {
        "key": "trace-metals",
        "model": "HeavyMetalAnalysis",
        "title": "Heavy metal analysis",
        "date_path": "analysis_date",
        "plant_path": "treatment_plant",
        "status_field": "state",
        "analysis_datetime_path": "created_at",
        "reference_path": "reference_id",
        "sample_title_path": "reference_id",
        "sample_fields": [
            ("Plant", "treatment_plant__name"),
            ("Treatment phase", "treatment_plant_phase__name"),
            ("Sampling slot", "sampling_time__time"),
            ("Shift", "shift__name"),
            ("Analysis date", "analysis_date"),
        ],
        "select_related": ["treatment_plant", "treatment_plant_phase", "sampling_time", "shift", "analysed_by"],
        "result_description": "saved trace-metal values and treatment-plant HeavyMetalLimit ranges",
        "result_specs": [
            {
                "model": "HeavyMetalParameter",
                "title_prefix": "",
                "value_field": "value",
                "parameter_name_path": "parameter__parameter__name",
                "unit_path": "parameter__parameter__unit",
                "minimum_path": "parameter__min_value",
                "maximum_path": "parameter__max_value",
                "filters": {"entry_status": "recorded"},
            },
        ],
    },
]

SCHEDULE_CONFIG = {
    "schedule_model": "HeavyMetalSamplingSchedule",
    "time_model": "HeavyMetalSamplingTime",
    "analysis_model": "HeavyMetalAnalysis",
    "result_generator": "heavy_metal",
    "lookback_minutes": 242,
}

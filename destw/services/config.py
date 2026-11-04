APP_LABEL = "destw"
DASHBOARD_ANALYSIS_SPECS = [
    {
        "model": "WasteWaterAnalysis",
        "result_model": "WasteWaterParameterResult",
        "sample_model": "WasteWaterSample",
        "title": "Wastewater analysis",
        "plant_path": "sample__treatment_plant",
        "sample_date_path": "sample__sampling_date",
        "analysis_relation": "destw_analysis",
        "sample_fields": [
            ("Sample", "sample__reference_id"),
            ("Sampled", "sample__sampling_date"),
            ("Time", "sample__sampling_time"),
            ("Sampling site", "sample__sampling_site__name"),
            ("Shift", "sample__shift__name"),
        ],
        "select_related": [
            "sample", "sample__treatment_plant", "sample__sampling_site",
            "sample__shift", "sample__assignment", "analysed_by",
        ],
    },
    {
        "model": "WasteWaterPondAnalysis",
        "result_model": "WasteWaterPondParameterResult",
        "sample_model": "WasteWaterPondSample",
        "title": "Stabilization pond analysis",
        "plant_path": "sample__site__pond__treatment_plant",
        "sample_date_path": "sample__sampling_date",
        "analysis_relation": "destw_pond_analysis",
        "sample_fields": [
            ("Sample", "sample__reference_id"),
            ("Sampled", "sample__sampling_date"),
            ("Time", "sample__sampling_time"),
            ("Pond", "sample__site__pond__name"),
            ("Sampling site", "sample__site__site_name"),
        ],
        "select_related": [
            "sample", "sample__site", "sample__site__pond",
            "sample__site__pond__treatment_plant", "analysed_by",
        ],
    },
]

ANALYSIS_GENERATOR_SPECS = [
    {
        "app_label": APP_LABEL,
        "sample_model": spec["sample_model"],
        "analysis_model": spec["model"],
        "analysis_relation": spec["analysis_relation"],
        "plant_path": spec["plant_path"].removeprefix("sample__"),
    }
    for spec in DASHBOARD_ANALYSIS_SPECS
]

RESULT_GENERATOR_SPECS = [
    {"app_label": APP_LABEL, "result_model": spec["result_model"], "analysis_model": spec["model"]}
    for spec in DASHBOARD_ANALYSIS_SPECS
]

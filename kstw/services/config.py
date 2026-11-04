APP_LABEL = "kstw"
DASHBOARD_ANALYSIS_SPECS = [
    {
        "model": "WasteWaterAnalysis",
        "result_model": "WasteWaterParameterResult",
        "sample_model": "WasteWaterSample",
        "title": "Wastewater analysis",
        "plant_path": "sample__treatment_plant",
        "sample_date_path": "sample__sampling_date",
        "analysis_relation": "analysis",
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
        "model": "IndustrialSampleAnalysis",
        "result_model": "IndustrialSampleParameterResult",
        "sample_model": "IndustrialSample",
        "title": "Industrial sample analysis",
        "plant_path": "sample__assignment__shift__treatment_plant",
        "sample_date_path": "sample__sampling_date",
        "analysis_relation": "analysis",
        "sample_fields": [
            ("Sample", "sample__reference_id"),
            ("Client", "sample__client_name"),
            ("Location", "sample__location"),
            ("Sampled", "sample__sampling_date"),
            ("Shift", "sample__assignment__shift__name"),
        ],
        "select_related": [
            "sample", "sample__shift", "sample__assignment",
            "sample__assignment__shift", "sample__assignment__shift__treatment_plant",
            "analysed_by",
        ],
    },
    {
        "model": "TreatmentPlantSampleAnalysis",
        "result_model": "TreatmentPlantSampleParameterResult",
        "sample_model": "TreatmentPlantSample",
        "title": "Treatment plant sample analysis",
        "plant_path": "sample__treatment_plant",
        "sample_date_path": "sample__sampling_date",
        "analysis_relation": "analysis",
        "sample_fields": [
            ("Sample", "sample__reference_id"),
            ("Sampling point", "sample__sampling_point_description"),
            ("Sampled", "sample__sampling_date"),
            ("Time", "sample__sampling_time"),
            ("Shift", "sample__shift__name"),
        ],
        "select_related": [
            "sample", "sample__treatment_plant", "sample__shift",
            "sample__assignment", "analysed_by",
        ],
    },
    {
        "model": "ExhausterSampleAnalysis",
        "result_model": "ExhausterSampleParameterResult",
        "sample_model": "ExhausterSample",
        "title": "Exhauster sample analysis",
        "plant_path": "sample__assignment__shift__treatment_plant",
        "sample_date_path": "sample__sampling_date",
        "analysis_relation": "analysis",
        "sample_fields": [
            ("Sample", "sample__reference_id"),
            ("Vehicle", "sample__exhauster_reg_number"),
            ("Driver", "sample__driver_name"),
            ("Sampled", "sample__sampling_date"),
            ("Shift", "sample__assignment__shift__name"),
        ],
        "select_related": [
            "sample", "sample__shift", "sample__assignment",
            "sample__assignment__shift", "sample__assignment__shift__treatment_plant",
            "analysed_by",
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

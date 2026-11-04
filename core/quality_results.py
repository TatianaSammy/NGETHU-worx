def parameter_result_metadata(parameter, analysis_method, value):
    minimum = parameter.min_standard
    maximum = parameter.max_standard
    unit = getattr(parameter, "unit", "")
    standard = f"{minimum} - {maximum}{f' {unit}' if unit else ''}"
    compliance = "Complying" if minimum <= value <= maximum else "Not Complying"
    return {
        "effluent_standard": standard,
        "kebs_reference": analysis_method.reference,
        "quality_compliance": compliance,
    }

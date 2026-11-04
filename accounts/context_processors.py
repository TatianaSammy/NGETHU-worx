# accounts/context_processors.py

from accounts.utils import get_user_landing_url


APP_LABELS = {
    "accounts": "Account & User Management",
    "alerts": "Alerts",
    "analysis_methods": "Analysis Methods",
    "audit_logs": "Audit Logs",
    "backwashing": "Backwashing",
    "ca": "Chemical Analysis",
    "central_lab": "Central Laboratory",
    "chlorinedemand": "Chlorine Demand",
    "CompanyInfo": "Company Information",
    "destw": "DESTW · Wastewater Operations",
    "duty_house": "Duty House",
    "filtered_water": "Filtered Water",
    "heavy_metal": "Heavy Metal Analysis",
    "instruments": "Laboratory Instruments",
    "inventory": "Inventory",
    "itinerary": "Itinerary",
    "jartest": "Jar Testing",
    "kstw": "Kariobangi South Treatment Works",
    "laboratory": "Laboratory",
    "maintenance": "Maintenance",
    "Maintenance_Incident": "Maintenance Incidents",
    "microbial": "Microbial Analysis",
    "o": "Operations",
    "parameters": "Water Parameters",
    "PHadjustment": "pH Adjustment",
    "qc": "Quality Control",
    "qc_analytics": "QCA Results",
    "sc": "Supply Chain",
    "t": "Treatment Plants",
    "weeklytasks": "Weekly Tasks",
    "wc": "Water Conservation",
    "wsp": "Water Safety Plans",
    "xmicalconcentration": "Chemical Concentration",
}


def _current_app_label(request):
    match = getattr(request, "resolver_match", None)
    app_name = getattr(match, "app_name", "") or ""
    namespace = getattr(match, "namespace", "") or ""
    key = app_name or namespace.split(":", 1)[0]
    if not key:
        return "Workspace"
    return APP_LABELS.get(key, key.replace("_", " ").replace("-", " ").title())


def _display_role_name(name):
    words = str(name).replace("_", " ").replace("-", " ").split()
    return " ".join(
        word if word.isupper() and len(word) <= 4 else word.title()
        for word in words
    )


def navigation_context(request):
    context = {"header_app_label": _current_app_label(request)}
    user = getattr(request, "user", None)

    if not user or not user.is_authenticated:
        return context

    try:
        role_names = sorted(
            {
                str(name).strip()
                for name in user.groups.values_list("name", flat=True)
                if str(name).strip()
            },
            key=str.casefold,
        )
    except Exception:
        role_names = []

    display_roles = [_display_role_name(role) for role in role_names]
    if user.is_superuser:
        role_summary = "System Administrator"
    elif display_roles:
        role_summary = " · ".join(display_roles[:2])
        if len(display_roles) > 2:
            role_summary += f" +{len(display_roles) - 2}"
    elif user.is_staff:
        role_summary = "Staff"
    else:
        role_summary = "General User"

    scope_items = []
    scope_fields = (
        ("section", "Section"),
        ("assigned_treatment_plant", "Plant"),
        ("assigned_lab", "Laboratory"),
    )
    for attribute, scope_label in scope_fields:
        try:
            scope = getattr(user, attribute, None)
            label = getattr(scope, "name", None) if scope else None
            if label:
                scope_items.append({"label": scope_label, "name": str(label)})
        except Exception:
            continue

    context.update(
        {
            "user_home_url": get_user_landing_url(user),
            "user_roles": display_roles,
            "user_role_summary": role_summary,
            "user_scope_items": scope_items,
            # The account-management views are currently superuser-only.
            "user_can_administer_accounts": bool(user.is_superuser),
        }
    )
    return context

# ca/navigation.py
from __future__ import annotations

from django.urls import reverse, NoReverseMatch, URLPattern, URLResolver

from core.engine.super_user import SuperUserEngine


CA_DASHBOARD_DATA_PERMISSIONS = (
    "ca.view_chemicalbatch",
    "ca.view_chemicalanalysisprocess",
    "ca.view_chemicalanalysisrequest",
    "ca.view_batchtestresult",
    "ca.view_chemicalcoa",
)
CA_ANALYTICS_DATA_PERMISSIONS = (
    "ca.view_chemicalbatch",
    "ca.view_batchtestresult",
    "ca.view_chemicalspecification",
)
CA_TASK_ACTION_PERMISSIONS = (
    "ca.receive_chemical_batch",
    "ca.create_analysis_request",
    "ca.complete_receiving_checklist",
    "ca.start_chemical_analysis",
    "ca.record_chemical_result",
    "ca.complete_chemical_analysis",
    "ca.perform_chemical_retest",
    "ca.review_chemical_analysis",
    "ca.approve_chemical_batch",
    "ca.reject_chemical_batch",
    "ca.approve_chemical_coa",
    "ca.verify_chemical_coa",
    "ca.utilize_chemical_batch",
    "ca.return_chemical_batch",
)


# Navigation is derived from ca/urls.py names. Keep labels/sections here;
# URL discovery stays tied to the named routes in urlpatterns.
CA_SIDEBAR_ITEMS = (
    {
        "section": "Workspace",
        "section_key": "workspace",
        "name": "dashboard",
        "label": "Dashboard",
        "description": "Chemical analysis workflow overview",
        "icon": "dashboard",
        "active_names": ("dashboard", "dashboard-view"),
        "permission": None,
    },
    {
        "section": "Workspace",
        "section_key": "workspace",
        "name": "my-tasks",
        "label": "My Tasks",
        "description": "Assigned workflow work",
        "icon": "tasks",
        "active_names": ("my-tasks",),
        "permission": "ca.view_chemicalanalysisprocess",
        "alt_permissions": CA_TASK_ACTION_PERMISSIONS,
    },
    {
        "section": "Workspace",
        "section_key": "workspace",
        "name": "process-list",
        "label": "Analysis Processes",
        "description": "Process control",
        "icon": "process",
        "active_names": (
            "process-list",
            "process-detail",
            "process-json",
            "process-statistics",
            "process-refresh",
            "qa-review",
            "process-action-form",
            "process-action-form-alt",
        ),
        "permission": "ca.view_chemicalanalysisprocess",
    },
    {
        "section": "Analysis Operations",
        "section_key": "operations",
        "name": "batch-list",
        "label": "Chemical Batches",
        "description": "Inventory and lifecycle",
        "icon": "batch",
        "active_names": ("batch-list", "batch-detail", "batch-update"),
        "permission": "ca.view_chemicalbatch",
    },
    {
        "section": "Analysis Operations",
        "section_key": "operations",
        "name": "batch-create",
        "label": "Receive Batch",
        "description": "Register incoming chemical",
        "icon": "add",
        "active_names": ("batch-create",),
        "permission": "ca.receive_chemical_batch",
    },
    {
        "section": "Analysis Operations",
        "section_key": "operations",
        "name": "request-list",
        "label": "Analysis Requests",
        "description": "Requests and assignments",
        "icon": "document",
        "active_names": ("request-list", "request-detail", "request-transition"),
        "permission": "ca.view_chemicalanalysisrequest",
    },
    {
        "section": "Analysis Operations",
        "section_key": "operations",
        "name": "result-list",
        "label": "Test Results",
        "description": "Laboratory results",
        "icon": "results",
        "active_names": ("result-list", "result-detail"),
        "permission": "ca.view_batchtestresult",
    },
    {
        "section": "Analysis Operations",
        "section_key": "operations",
        "name": "interlaboratory-referral-list",
        "label": "Interlaboratory Referrals",
        "description": "Peer laboratory verification requests",
        "icon": "results",
        "active_names": ("interlaboratory-referral-list", "interlaboratory-referral-detail"),
        "permission": "ca.record_chemical_result",
    },
    {
        "section": "Quality & Compliance",
        "section_key": "quality",
        "name": "quality-control-dashboard",
        "label": "Method QC Control Charts",
        "description": "QC trends, acceptance and analytical drift",
        "icon": "analysis",
        "active_names": ("quality-control-dashboard",),
        "permission": "ca.view_batchtestresult",
    },
    {
        "section": "Control Queues",
        "section_key": "queues",
        "name": "pending-analysis",
        "label": "Pending Analysis",
        "description": "Work awaiting analysis",
        "icon": "analysis",
        "active_names": ("pending-analysis",),
        "permission": "ca.record_chemical_result",
    },
    {
        "section": "Control Queues",
        "section_key": "queues",
        "name": "pending-qa",
        "label": "Chemist Review",
        "description": "Independent chemist review queue",
        "icon": "approval",
        "active_names": ("pending-qa",),
        "permission": "ca.view_chemicalanalysisprocess",
    },
    {
        "section": "Control Queues",
        "section_key": "queues",
        "name": "pending-coa-approval",
        "label": "Pending CoA Approval",
        "description": "Certificates awaiting approval",
        "icon": "certificate",
        "active_names": ("pending-coa-approval",),
        "permission": "ca.approve_chemical_coa",
    },
    {
        "section": "Control Queues",
        "section_key": "queues",
        "name": "overdue-analysis",
        "label": "Overdue Analysis",
        "description": "SLA exceptions",
        "icon": "alert",
        "active_names": ("overdue-analysis",),
        "permission": "ca.view_chemicalanalysisprocess",
    },
    {
        "section": "Quality & Compliance",
        "section_key": "quality",
        "name": "coa-list",
        "label": "Certificates of Analysis",
        "description": "CoA verification and control",
        "icon": "certificate",
        "active_names": (
            "coa-list",
            "coa-detail",
            "coa-regenerate",
            "coa-verify",
            "coa-download",
        ),
        "permission": "ca.view_chemicalcoa",
    },
    {
        "section": "Administration",
        "section_key": "administration",
        "name": "specification-list",
        "label": "Chemical Specifications",
        "description": "Analytical specifications",
        "icon": "specification",
        "active_names": ("specification-list",),
        "permission": "ca.view_chemicalspecification",
    },
)


def _named_routes_from_urlpatterns(urlpatterns, prefix=""):
    names = set()

    for pattern in urlpatterns:
        if isinstance(pattern, URLResolver):
            nested = prefix + str(pattern.pattern)
            names.update(_named_routes_from_urlpatterns(pattern.url_patterns, nested))
            continue

        if isinstance(pattern, URLPattern) and pattern.name:
            names.add(pattern.name)

    return names


def get_registered_ca_url_names():
    from . import urls as ca_urls

    return _named_routes_from_urlpatterns(ca_urls.urlpatterns)


def _user_may(user, permission, alt_permissions=(), all_permissions=()):
    if SuperUserEngine.is_superuser(user):
        return True

    if all_permissions and not all(user.has_perm(perm) for perm in all_permissions):
        return False

    if not permission and not alt_permissions:
        return True

    if permission and user.has_perm(permission):
        return True

    return any(user.has_perm(perm) for perm in alt_permissions if perm)


def build_ca_sidebar(request):
    """
    Build sidebar sections from CA_SIDEBAR_ITEMS, constrained to names
    that exist in ca/urls.py.
    """
    registered = get_registered_ca_url_names()
    current = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
    user = request.user

    sections = []
    section_map = {}

    for item in CA_SIDEBAR_ITEMS:
        name = item["name"]
        if name not in registered:
            continue

        if not _user_may(
            user,
            item.get("permission"),
            item.get("alt_permissions", ()),
            item.get("all_permissions", ()),
        ):
            continue

        try:
            href = reverse(f"ca:{name}")
        except NoReverseMatch:
            continue

        active_names = item.get("active_names") or (name,)
        is_active = current in active_names

        section_key = item["section_key"]
        if section_key not in section_map:
            section = {
                "key": section_key,
                "heading": item["section"],
                "items": [],
            }
            section_map[section_key] = section
            sections.append(section)

        section_map[section_key]["items"].append(
            {
                "name": name,
                "label": item["label"],
                "description": item.get("description", ""),
                "icon": item.get("icon", "document"),
                "href": href,
                "is_active": is_active,
                "active_names": active_names,
            }
        )

    return sections

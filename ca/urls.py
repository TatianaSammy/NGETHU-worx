# ca/urls.py
from __future__ import annotations

from django.urls import include, path
from django.views.generic import RedirectView
from viewflow.workflow.flow import FlowViewset
from .dashboard import (
    ChemicalAnalyticsDataView,
    ChemicalNotificationAckView,
    ChemicalNotificationsView,
    ChemicalReportExportView,
    ChemicalReportPreviewView,
)
from .dashboard_home import ChemicalAnalysisDashboardView, ChemicalBatchRegisterExportView
from .interlaboratory import InterlaboratoryReferralDetailView, InterlaboratoryReferralListView
from .quality_control_views import ChemicalQCControlChartView
from .chemical_flow import ChemicalAnalysisFlow
from .views import (
    ChemicalAnalysisInputDetailView,
    ChemicalAnalysisRequestDetailView,
    ChemicalAnalysisRequestListView,
    ChemicalAnalysisRequestAssignAnalystView,
    ChemicalBatchCustodyRecordView,
    ChemicalBatchHoldResolutionView,
    ChemicalBatchCreateView,
    ChemicalBatchDeleteView,
    ChemicalBatchDetailView,
    ChemicalBatchListView,
    ChemicalBatchUpdateView,
    ChemicalBatchWorkflowActionView,
    ChemicalBatchTaskClaimView,
    ChemicalCoADetailView,
    ChemicalCoAListView,
    ChemicalSpecificationFilterView,
    ChemicalSpecificationListView,
    ChecklistDetailView,
    DownloadChemicalCoAView,
    MyChemicalTasksView,
    OverdueAnalysisView,
    PendingAnalysisView,
    PendingCoAApprovalView,
    PendingQAReviewView,
    ProcessDetailView,
    ProcessJSONView,
    ProcessListView,
    ProcessRefreshView,
    ProcessStatisticsView,
    RecalculateAnalysisInputView,
    RegenerateChemicalCoAView,
    ReopenChecklistView,
    ResultDetailView,
    ResultListView,
    VerifyChemicalCoAView,
)

app_name = "ca"

workflow = FlowViewset(ChemicalAnalysisFlow)
workflow_namespace = "chemicalanalysis"
# Viewflow clears the Flow class' ``app_name`` after attaching it to this
# viewset. Preserve the namespace used by the nested URL include explicitly so
# task-level reverse() calls resolve to ca:chemicalanalysis:<node>:<action>.
workflow.namespace = workflow_namespace
workflow.parent_namespace = "ca"

urlpatterns = [
    path("", ChemicalAnalysisDashboardView.as_view(), name="dashboard"),
    path("dashboard/", ChemicalAnalysisDashboardView.as_view(), name="dashboard-view"),
    # Keep old bookmarks working while maintaining one canonical dashboard.
    path("dashboard2/", RedirectView.as_view(pattern_name="ca:dashboard", permanent=False), name="dashboard2"),
    path("dashboard3/", RedirectView.as_view(pattern_name="ca:dashboard", permanent=False), name="dashboard3"),
    path("batches/", ChemicalBatchListView.as_view(), name="batch-list"),
    path("batches/register-export/", ChemicalBatchRegisterExportView.as_view(), name="batch-register-export"),
    path("batches/add/", ChemicalBatchCreateView.as_view(), name="batch-create"),
    path("batches/<int:pk>/delete/", ChemicalBatchDeleteView.as_view(), name="batch-delete"),
    path(
        "batches/<int:pk>/workflow/<str:node_name>/",
        ChemicalBatchWorkflowActionView.as_view(),
        name="batch-workflow-action",
    ),
    path(
        "batches/<int:pk>/tasks/<int:task_pk>/claim/",
        ChemicalBatchTaskClaimView.as_view(),
        name="batch-task-claim",
    ),
    path("batches/<int:pk>/", ChemicalBatchDetailView.as_view(), name="batch-detail"),
    path("batches/<int:pk>/edit/", ChemicalBatchUpdateView.as_view(), name="batch-update"),
    path("batches/<int:pk>/holds/<int:hold_id>/resolve/", ChemicalBatchHoldResolutionView.as_view(), name="batch-hold-resolve"),
    path("batches/<int:pk>/custody/", ChemicalBatchCustodyRecordView.as_view(), name="batch-custody-record"),

    path("requests/", ChemicalAnalysisRequestListView.as_view(), name="request-list"),
    path("requests/<int:pk>/", ChemicalAnalysisRequestDetailView.as_view(), name="request-detail"),
    path("requests/<int:pk>/assign-analyst/", ChemicalAnalysisRequestAssignAnalystView.as_view(), name="request-assign-analyst"),
    path("requests/<int:pk>/transition/", ChemicalAnalysisRequestDetailView.as_view(), name="request-transition"),

    path("processes/", ProcessListView.as_view(), name="process-list"),
    path("processes/<int:pk>/", ProcessDetailView.as_view(), name="process-detail"),
    path("processes/<int:pk>/qa-review/", ProcessDetailView.as_view(), name="qa-review"),
    path("processes/<int:pk>/json/", ProcessJSONView.as_view(), name="process-json"),
    path("processes/<int:pk>/statistics/", ProcessStatisticsView.as_view(), name="process-statistics"),
    path("processes/<int:pk>/refresh/", ProcessRefreshView.as_view(), name="process-refresh"),
    path("processes/<int:pk>/action/", ProcessDetailView.as_view(), name="process-action-form"),
    path("processes/<int:pk>/action/form/", ProcessDetailView.as_view(), name="process-action-form-alt"),

    path(
        "analysis/inputs/<int:pk>/",
        ChemicalAnalysisInputDetailView.as_view(),
        name="analysis-input-detail",
    ),
    path(
        "analysis/inputs/<int:pk>/recalculate/",
        RecalculateAnalysisInputView.as_view(),
        name="analysis-input-recalculate",
    ),

    path(
        "specifications/",
        ChemicalSpecificationListView.as_view(),
        name="specification-list",
    ),
    path(
        "specifications/filter/",
        ChemicalSpecificationFilterView.as_view(),
        name="specification-filter",
    ),

    path("results/", ResultListView.as_view(), name="result-list"),
    path("results/<int:pk>/", ResultDetailView.as_view(), name="result-detail"),

    path("interlaboratory/referrals/", InterlaboratoryReferralListView.as_view(), name="interlaboratory-referral-list"),
    path("interlaboratory/referrals/<int:pk>/", InterlaboratoryReferralDetailView.as_view(), name="interlaboratory-referral-detail"),
    path("quality-control/", ChemicalQCControlChartView.as_view(), name="quality-control-dashboard"),

    path("checklists/<int:pk>/", ChecklistDetailView.as_view(), name="checklist-detail"),
    path(
        "checklists/<int:pk>/reopen/",
        ReopenChecklistView.as_view(),
        name="checklist-reopen",
    ),

    path("coa/", ChemicalCoAListView.as_view(), name="coa-list"),
    path("coa/<int:pk>/", ChemicalCoADetailView.as_view(), name="coa-detail"),
    path(
        "coa/<int:pk>/regenerate/",
        RegenerateChemicalCoAView.as_view(),
        name="coa-regenerate",
    ),
    path(
        "coa/<int:pk>/verify/",
        VerifyChemicalCoAView.as_view(),
        name="coa-verify",
    ),
    path(
        "coa/<int:pk>/download/",
        DownloadChemicalCoAView.as_view(),
        name="coa-download",
    ),

    path("tasks/", MyChemicalTasksView.as_view(), name="my-tasks"),
    path("tasks/overdue/", OverdueAnalysisView.as_view(), name="overdue-analysis"),
    path(
        "tasks/pending-analysis/",
        PendingAnalysisView.as_view(),
        name="pending-analysis",
    ),
    path(
        "tasks/pending-qa/",
        PendingQAReviewView.as_view(),
        name="pending-qa",
    ),
    path(
        "tasks/pending-coa-approval/",
        PendingCoAApprovalView.as_view(),
        name="pending-coa-approval",
    ),

    # Reports & Analytics API
    path("reports/export/", ChemicalReportExportView.as_view(), name="report-export"),
    path("reports/preview/", ChemicalReportPreviewView.as_view(), name="report-preview"),
    path("analytics/data/", ChemicalAnalyticsDataView.as_view(), name="analytics-data"),

    # Alerts & Notifications Integration (channelled through alerts app)
    path("notifications/", ChemicalNotificationsView.as_view(), name="notifications"),
    path("notifications/<str:pk>/ack/", ChemicalNotificationAckView.as_view(), name="notification-ack"),

    # FlowViewset clears the flow app_name while attaching nodes, so supply the
    # app name explicitly for Django's nested namespace include.
    path(
        "workflow/",
        include(
            (workflow.urls[0], workflow_namespace),
            namespace=workflow_namespace,
        ),
    ),
]

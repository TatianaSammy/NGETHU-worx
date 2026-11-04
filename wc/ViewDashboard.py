from collections import Counter
from datetime import timedelta

from django.db.models import Avg, Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard
from core.mixins import WanjalaMixin
from .models import (
    ComplaintAttachment,
    ComplaintAuditTrail,
    ComplaintConfirmationReport,
    ComplaintMicrobialAnalysis,
    ComplaintMicrobialParameterResult,
    ComplaintPost,
    ComplaintResolution,
    ComplaintStatusLog,
    WaterQualityComplaint,
    WaterSample,
    WaterSampleParameterResult,
)


class DashboardView(WanjalaMixin, View):
    template_name = "wc/dashboard.html"
    app_label = "wc"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        days = period_days(request)
        now = timezone.now()
        start = now - timedelta(days=days)
        complaints = WaterQualityComplaint.objects.filter(is_deleted=False).select_related("user", "category", "region", "zone", "sub_zone")
        privileged = request.user.is_superuser or request.user.has_perm("wc.view_waterqualitycomplaint")
        if not privileged:
            complaints = complaints.filter(user=request.user)
        window = complaints.filter(created_at__gte=start, created_at__lte=now)
        complaint_ids = complaints.values_list("pk", flat=True)
        posts = ComplaintPost.objects.filter(complaint_id__in=complaint_ids, is_deleted=False)
        resolutions = ComplaintResolution.objects.filter(complaint_id__in=complaint_ids, is_deleted=False)
        status_logs = ComplaintStatusLog.objects.filter(complaint_id__in=complaint_ids)
        samples = WaterSample.objects.filter(complaint_id__in=complaint_ids, is_deleted=False).select_related("complaint")
        sample_results = WaterSampleParameterResult.objects.filter(sample__in=samples, is_deleted=False).select_related("parameter", "sample")
        microbial = ComplaintMicrobialAnalysis.objects.filter(sample__in=samples, is_deleted=False).select_related("sample")
        microbial_results = ComplaintMicrobialParameterResult.objects.filter(analysis__in=microbial, is_deleted=False).select_related("parameter", "analysis")
        confirmations = ComplaintConfirmationReport.objects.filter(complaint_id__in=complaint_ids, is_deleted=False)
        attachments = ComplaintAttachment.objects.filter(complaint_id__in=complaint_ids, is_deleted=False)
        audit = ComplaintAuditTrail.objects.filter(complaint_id__in=complaint_ids).select_related("complaint", "performed_by")

        status_counts = Counter(complaints.values_list("status", flat=True))
        total = complaints.count()
        pending = status_counts.get("PENDING", 0)
        in_progress = status_counts.get("IN_PROGRESS", 0)
        resolved = status_counts.get("RESOLVED", 0) + status_counts.get("CLOSED", 0)
        resolution_rate = round(resolved * 100 / total, 1) if total else None
        category_counts = list(window.values("category__name").annotate(total=Count("pk")).order_by("-total")[:10])
        trend_counts = Counter(row["day"] for row in window.annotate(day=TruncDate("created_at")).values("day"))
        buckets = [start.date() + timedelta(days=offset) for offset in range(days)]
        if days > 90:
            weekly = Counter()
            for day, count in trend_counts.items():
                weekly[day - timedelta(days=day.weekday())] += count
            buckets, trend_counts = sorted(weekly), weekly
        status_log_count = status_logs.filter(changed_at__gte=start).count()
        confirmation_rate = round(confirmations.filter(is_confirmed=True).count() * 100 / confirmations.count(), 1) if confirmations.exists() else None

        recent_complaints = list(window.order_by("-created_at")[:20])
        recent_records = [{
            "title": f"{complaint.reference_id.hex[:10].upper()} · {complaint.title}",
            "detail": f"{complaint.get_status_display()} · {complaint.category.name if complaint.category else 'Uncategorized'} · {complaint.region.name if complaint.region else 'Region not set'}",
            "timestamp": timezone.localtime(complaint.created_at).strftime("%d %b %H:%M"),
            "icon": "triangle-exclamation" if complaint.status in {"PENDING", "IN_PROGRESS"} else "circle-check",
            "color": "amber" if complaint.status == "PENDING" else "blue" if complaint.status == "IN_PROGRESS" else "emerald",
        } for complaint in recent_complaints]
        table_rows = [{"cells": [
            complaint.reference_id.hex[:12].upper(),
            complaint.title,
            complaint.category.name if complaint.category else "—",
            complaint.get_status_display(),
            str(complaint.region or "—"),
            complaint.user.get_full_name() if complaint.user else "—",
            timezone.localtime(complaint.created_at).strftime("%d %b %Y %H:%M"),
        ]} for complaint in recent_complaints]

        model_specs = [
            ("Complaints", complaints, "triangle-exclamation", "Customer cases"),
            ("Complaint posts", posts, "comments", "Case communication"),
            ("Resolutions", resolutions, "circle-check", "Case resolution"),
            ("Status history", status_logs, "clock-rotate-left", "Workflow audit"),
            ("Water samples", samples, "vial", "Sample collection"),
            ("Sample parameter results", sample_results, "flask-vial", "Water quality results"),
            ("Microbial analyses", microbial, "bacteria", "Microbiological confirmation"),
            ("Microbial results", microbial_results, "virus", "Microbiological results"),
            ("Confirmation reports", confirmations, "file-circle-check", "Confirmation workflow"),
            ("Attachments", attachments, "paperclip", "Case evidence"),
            ("Audit events", audit, "list-check", "Audit trail"),
        ]
        model_rows = [{"label": label, "count": qs.count(), "url": model_list_url(self.app_label, qs.model._meta.model_name), "icon": icon, "group": group} for label, qs, icon, group in model_specs]
        total_results = sample_results.count() + microbial_results.count()
        compliant_results = sample_results.filter(is_compliant=True).count()
        failed_results = sample_results.filter(is_compliant=False).count()
        kpis = [
            {"label": "Complaints received", "value": window.count(), "detail": f"Filed in the last {days} days", "icon": "inbox", "color": "cyan", "tab": "records"},
            {"label": "Awaiting action", "value": pending + in_progress, "detail": f"{pending} pending · {in_progress} in progress", "icon": "triangle-exclamation", "color": "amber", "tab": "analytics"},
            {"label": "Resolved", "value": resolved, "detail": f"{resolution_rate}% of visible complaints" if resolution_rate is not None else "No complaints in scope", "icon": "circle-check", "color": "emerald", "tab": "analytics"},
            {"label": "Water samples", "value": samples.count(), "detail": f"{total_results} sample result records", "icon": "vial", "color": "blue", "tab": "models"},
            {"label": "Microbial follow-ups", "value": microbial.count(), "detail": f"{microbial_results.count()} microbial result records", "icon": "bacteria", "color": "purple", "tab": "models"},
            {"label": "Status changes", "value": status_log_count, "detail": f"Workflow events in {days} days", "icon": "clock-rotate-left", "color": "rose", "tab": "analytics"},
        ]
        ranked_rows = [{"label": row["category__name"] or "Uncategorized", "detail": "Complaints filed in window", "value": row["total"], "percent": round(row["total"] * 100 / max(window.count(), 1)), "color": "rose"} for row in category_counts]
        context = dashboard_context(
            request,
            title="Water Quality Complaints",
            subtitle="Customer case load, resolution workflow, field samples, and quality confirmation signals.",
            kicker="Service quality intelligence",
            kpis=kpis,
            charts=[
                {"id": "wc-complaint-trend", "title": "Complaint intake", "caption": "New visible complaints by filing date.", "type": "line", "labels": [day.strftime("%d %b") for day in buckets], "values": [trend_counts.get(day, 0) for day in buckets], "color": "rose"},
                {"id": "wc-status-mix", "title": "Case lifecycle", "caption": "Current visible complaint states.", "type": "doughnut", "labels": ["Pending", "In progress", "Resolved", "Closed"], "values": [status_counts.get(key, 0) for key in ["PENDING", "IN_PROGRESS", "RESOLVED", "CLOSED"]], "colors": ["amber", "blue", "emerald", "purple"]},
                {"id": "wc-categories", "title": "Complaint category mix", "caption": "New complaints grouped by category.", "type": "horizontal", "labels": [row["category__name"] or "Uncategorized" for row in category_counts], "values": [row["total"] for row in category_counts], "color": "rose"},
            ],
            signals=[
                {"label": "Within-limit sample results", "value": compliant_results, "detail": "Water sample results marked compliant", "icon": "circle-check", "color": "emerald"},
                {"label": "Out-of-limit results", "value": failed_results, "detail": "Water sample results marked non-compliant", "icon": "triangle-exclamation", "color": "rose"},
                {"label": "Confirmation rate", "value": f"{confirmation_rate}%" if confirmation_rate is not None else "—", "detail": "Confirmed reports / visible reports", "icon": "file-circle-check", "color": "blue"},
                {"label": "Case evidence", "value": attachments.count(), "detail": "Attachments across visible complaints", "icon": "paperclip", "color": "purple"},
            ],
            recent_records=recent_records,
            model_rows=model_rows,
            table_headings=["Reference", "Complaint", "Category", "Status", "Region", "Reporter", "Filed"],
            table_rows=table_rows,
            ranked_rows=ranked_rows,
            actions=[{"label": "Complaint register", "url": "/wc/", "icon": "list-check"}],
            overview_caption="Complaint records are scoped to the current user unless they have permission to review the full complaint register.",
            model_count=len(model_specs),
        )
        return context

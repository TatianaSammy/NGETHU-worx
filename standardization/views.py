import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, Q
from django.http import HttpResponse
from django.core.paginator import Paginator
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from inventory.models import LaboratoryReagent

from .forms import StandardizationForm
from .models import Standardization


class StandardizationDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "standardization/dashboard.html"
    login_url = "login"
    PERIODS = {"30": 30, "90": 90, "365": 365}

    def get_queryset(self):
        queryset = Standardization.objects.select_related(
            "titrant", "standard", "prepared_by"
        )
        period = self.request.GET.get("period", "90")
        days = self.PERIODS.get(period)
        if days:
            queryset = queryset.filter(
                preparation_date__gte=timezone.localdate() - timedelta(days=days - 1)
            )

        titrant_id = self.request.GET.get("titrant", "")
        if titrant_id.isdigit():
            queryset = queryset.filter(titrant_id=int(titrant_id))

        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(titrant__rname__icontains=query)
                | Q(standard__rname__icontains=query)
                | Q(prepared_by__username__icontains=query)
                | Q(remarks__icontains=query)
            )
        return queryset

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            return self.export_csv()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        form = StandardizationForm(request.POST)
        if form.is_valid():
            record = form.save(user=request.user)
            messages.success(
                request,
                f"Standardization recorded for {record.titrant.rname} on "
                f"{record.preparation_date:%d %b %Y}. Actual titrant normality: "
                f"{record.actual_normality:.4f} N.",
            )
            return redirect("standardization:dashboard")
        return self.render_to_response(
            self.get_context_data(form=form, show_entry=True),
            status=400,
        )

    def export_csv(self):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="standardization-history.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow((
            "Preparation date", "Titrant", "Aliquot (titrand)",
            "Known titrand normality", "Aliquot volume (mL)", "Titre 1 (mL)",
            "Titre 2 (mL)", "Titre 3 (mL)", "Average titre (mL)",
            "Actual titrant normality", "Prepared by", "Remarks",
        ))
        for record in self.get_queryset().order_by("-preparation_date", "-created_at"):
            writer.writerow((
                record.preparation_date,
                record.titrant.rname,
                record.standard.rname,
                record.standard_normality,
                record.aliquot_volume_ml,
                record.titre1,
                record.titre2,
                record.titre3,
                record.average_titre,
                record.actual_normality,
                record.prepared_by.get_full_name() or record.prepared_by.username if record.prepared_by else "",
                record.remarks or "",
            ))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self.get_queryset()
        ordered_queryset = queryset.order_by("-preparation_date", "-created_at", "-pk")
        page_obj = Paginator(ordered_queryset, 25).get_page(self.request.GET.get("page", 1))
        records = list(page_obj.object_list)
        aggregate = queryset.aggregate(
            average_normality=Avg("actual_normality"),
            reagents=Count("titrant_id", distinct=True),
        )

        period = self.request.GET.get("period", "90")
        context.update({
            "form": kwargs.get("form") or StandardizationForm(),
            "show_entry": kwargs.get(
                "show_entry", self.request.GET.get("entry") == "1"
            ),
            "records": records,
            "page_obj": page_obj,
            "record_count": queryset.count(),
            "average_normality": aggregate["average_normality"],
            "reagent_count": aggregate["reagents"],
            "reagents": LaboratoryReagent.objects.order_by("rname", "pk"),
            "selected_period": period if period in self.PERIODS or period == "all" else "90",
            "selected_titrant": self.request.GET.get("titrant", ""),
            "search_query": self.request.GET.get("q", ""),
        })
        return context

from django.db.models import Count
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, render_domain_dashboard
from core.mixins import WanjalaMixin
from .models import CompanyInfo, RegionalInfo, SubZonalInfo, ZonalInfo


class DashboardView(WanjalaMixin, View):
    template_name = "CompanyInfo/dashboard.html"
    app_label = "CompanyInfo"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        companies = CompanyInfo.objects.all()
        regions = RegionalInfo.objects.select_related("company").annotate(zone_count=Count("zones", distinct=True))
        zones = ZonalInfo.objects.select_related("region", "region__company").annotate(subzone_count=Count("sub_zones", distinct=True))
        subzones = SubZonalInfo.objects.select_related("zone", "zone__region", "zone__region__company")
        recent = []
        table_rows = []
        for model, label, group in [(CompanyInfo, "Company", "Corporate registry"), (RegionalInfo, "Region", "Geographic hierarchy"), (ZonalInfo, "Zone", "Geographic hierarchy"), (SubZonalInfo, "Sub-zone", "Geographic hierarchy")]:
            for item in model.objects.order_by("-updated_at")[:8]:
                recent.append((item.updated_at, {"title": f"{label} · {item.name}", "detail": str(item), "timestamp": item.updated_at.strftime("%d %b %H:%M"), "icon": "building" if label == "Company" else "map-location-dot", "color": "cyan"}))
                if model is CompanyInfo:
                    cells = [label, item.name, item.slug, item.phone_number, item.location, "Logo set" if item.logo else "No logo", item.updated_at.strftime("%d %b %Y")]
                elif model is RegionalInfo:
                    cells = [label, item.name, item.company.name, item.telephone_number, item.office_location, "Boundary set" if item.boundary else "No boundary", item.updated_at.strftime("%d %b %Y")]
                elif model is ZonalInfo:
                    cells = [label, item.name, item.region.name, "—", item.address, "Boundary set" if item.boundary else "No boundary", item.updated_at.strftime("%d %b %Y")]
                else:
                    cells = [label, item.name, item.zone.region.name, "—", item.address, "Boundary set" if item.boundary else "No boundary", item.updated_at.strftime("%d %b %Y")]
                table_rows.append((item.updated_at, {"cells": cells}))
        recent_records = [row for _date, row in sorted(recent, key=lambda row: row[0], reverse=True)[:12]]
        table_rows = [row for _date, row in sorted(table_rows, key=lambda row: row[0], reverse=True)[:50]]
        company_count = companies.count()
        region_count = regions.count()
        zone_count = zones.count()
        subzone_count = subzones.count()
        geocoded_regions = regions.exclude(coordinates__isnull=True).count()
        boundary_regions = regions.exclude(boundary__isnull=True).count()
        hierarchy_total = company_count + region_count + zone_count + subzone_count
        model_specs = [("Companies", companies, "building", "Corporate identity"), ("Regions", regions, "earth-africa", "Geographic hierarchy"), ("Zones", zones, "map", "Geographic hierarchy"), ("Sub-zones", subzones, "location-dot", "Geographic hierarchy")]
        model_rows = [{"label": label, "count": qs.count(), "url": model_list_url(self.app_label, qs.model._meta.model_name), "icon": icon, "group": group} for label, qs, icon, group in model_specs]
        kpis = [
            {"label": "Company profiles", "value": company_count, "detail": "Corporate identity records", "icon": "building", "color": "cyan", "tab": "models"},
            {"label": "Regions", "value": region_count, "detail": f"{geocoded_regions} with map coordinates", "icon": "earth-africa", "color": "blue", "tab": "analytics"},
            {"label": "Zones", "value": zone_count, "detail": "Linked to a parent region", "icon": "map", "color": "purple", "tab": "models"},
            {"label": "Sub-zones", "value": subzone_count, "detail": "Linked to a parent zone", "icon": "location-dot", "color": "emerald", "tab": "models"},
            {"label": "Geographic boundaries", "value": boundary_regions, "detail": "Regions with boundary geometry", "icon": "draw-polygon", "color": "amber", "tab": "analytics"},
            {"label": "Hierarchy records", "value": hierarchy_total, "detail": "Corporate and geographic records", "icon": "diagram-project", "color": "rose", "tab": "records"},
        ]
        context = dashboard_context(
            request,
            title="Corporate & Service Geography",
            subtitle="Corporate identity, service territories, and hierarchical location coverage.",
            kicker="Organization intelligence",
            kpis=kpis,
            charts=[
                {"id": "company-geography", "title": "Service-area hierarchy", "caption": "Actual geographic records at each level.", "type": "bar", "labels": ["Companies", "Regions", "Zones", "Sub-zones"], "values": [company_count, region_count, zone_count, subzone_count], "color": "cyan"},
                {"id": "company-geodata", "title": "Regional geospatial coverage", "caption": "Configured coordinate and boundary availability.", "type": "doughnut", "labels": ["Coordinates available", "Boundary available", "No coordinate", "No boundary"], "values": [geocoded_regions, boundary_regions, max(region_count - geocoded_regions, 0), max(region_count - boundary_regions, 0)], "colors": ["cyan", "emerald", "amber", "slate"]},
            ],
            signals=[
                {"label": "Regions per company", "value": round(region_count / company_count, 1) if company_count else "—", "detail": "Average registered regions", "icon": "earth-africa", "color": "blue"},
                {"label": "Zones per region", "value": round(zone_count / region_count, 1) if region_count else "—", "detail": "Average registered zones", "icon": "map", "color": "purple"},
                {"label": "Sub-zones per zone", "value": round(subzone_count / zone_count, 1) if zone_count else "—", "detail": "Average registered sub-zones", "icon": "location-dot", "color": "emerald"},
                {"label": "Regions without geometry", "value": max(region_count - boundary_regions, 0), "detail": "No boundary JSON stored", "icon": "draw-polygon", "color": "amber"},
            ],
            recent_records=recent_records,
            model_rows=model_rows,
            table_headings=["Level", "Name", "Parent", "Telephone", "Address / location", "Geospatial / identity", "Updated"],
            table_rows=table_rows,
            ranked_rows=[{"label": label, "detail": f"{count} registered records", "value": count, "percent": round(count * 100 / max(hierarchy_total, 1)), "color": color} for label, count, color in [("Regions", region_count, "blue"), ("Zones", zone_count, "purple"), ("Sub-zones", subzone_count, "emerald")]],
            actions=[{"label": "Company registry", "url": "/CompanyInfo/", "icon": "building"}],
            overview_caption="Organization and geography information is read from the company, region, zone, and sub-zone models.",
            model_count=len(model_specs),
        )
        return context

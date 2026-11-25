#qad/urls.py
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from django.core.cache import cache
from django.db import connection
from django.views.generic import RedirectView

import json
import os
from core.views import HomeView
from accounts.LoginView import LoginView
from accounts.LogoutView import LogoutView


def manifest_json(request):
    is_qc_app = request.GET.get("app") == "qc"
    manifest_path = os.path.join(
        settings.BASE_DIR,
        'qc', 'static', 'qc', 'results', 'manifest.json'
    ) if is_qc_app else os.path.join(settings.BASE_DIR, 'static', 'manifest.json')
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception:
        manifest = {}

    # Use the current CompanyInfo logo for the installed-app/taskbar icon as
    # well as for shortcuts. Leave the packaged icons as a safe fallback.
    try:
        from CompanyInfo.models import CompanyInfo

        company = CompanyInfo.objects.order_by("pk").first()
        if company and company.logo:
            logo_url = request.build_absolute_uri(company.logo.url)
            logo_icon = {"src": logo_url, "purpose": "any"}
            # Keep the bundled, correctly sized install icons as fallbacks;
            # company logos can be small or use formats browsers do not accept
            # as install icons.
            icons = manifest.setdefault("icons", [])
            if not any(icon.get("src") == logo_url for icon in icons):
                icons.append(logo_icon)
            for shortcut in manifest.get("shortcuts", []):
                shortcut["icons"] = [logo_icon]
    except Exception:
        pass

    response = HttpResponse(
        json.dumps(manifest),
        content_type='application/manifest+json',
    )
    response["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


def service_worker(request):
    sw_path = os.path.join(settings.BASE_DIR, 'core', 'static', 'core', 'js', 'sw.js')
    try:
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        content = ''
    response = HttpResponse(content, content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


def health_live(request):
    return HttpResponse("OK")


def health_ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        cache_key = "qad:health:ready"
        cache.set(cache_key, "ok", timeout=5)
        if cache.get(cache_key) != "ok":
            raise RuntimeError("Cache read/write failed")
        cache.delete(cache_key)
    except Exception:
        return HttpResponse("Unavailable", status=503)
    return HttpResponse("OK")


urlpatterns = [
    path('', HomeView.as_view(), name='home'),
    path('manifest.json', manifest_json, name='pwa_manifest'),
    path('sw.js', service_worker, name='pwa_service_worker'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('core/', include('core.urls')),
    path('admin/', admin.site.urls),
    path('qca/', include('qc_analytics.urls')),
    path('accounts/', include('accounts.urls')),
    path("ca/", include("ca.urls")),
    path('t/', include('t.urls')),
    path('inventory/', include('inventory.urls')),
    path('standardization/', include('standardization.urls')),
    path("sc/", include("sc.urls")),
    path('wc/', include('wc.urls')),
    path('parameters/', include('parameters.urls')),
    path('microbial/', include('microbial.urls')),
    path("heavy_metal/", include("heavy_metal.urls")),
    path('instruments/', include('instruments.urls')),
    path('o/', include('o.urls')),
    path('PHadjustment/', include('PHadjustment.urls')),
    path("Maintenance/", include("maintenance.urls")),
    # Legacy redirects — unified under Maintenance
    re_path(
        r"^duty_house/(?P<path>.*)$",
        RedirectView.as_view(url="/Maintenance/%(path)s", permanent=False),
        name="duty_house_redirect",
    ),
    re_path(
        r"^maintenance/(?P<path>.*)$",
        RedirectView.as_view(url="/Maintenance/%(path)s", permanent=False),
        name="maintenance_legacy_redirect",
    ),
    re_path(
        r"^Maintenance_Incident/(?P<path>.*)$",
        RedirectView.as_view(url="/Maintenance/%(path)s", permanent=False),
        name="maintenance_incident_redirect",
    ),
    path('wsp/', include('wsp.urls')),
    path('chlorinedemand/', include('chlorinedemand.urls')),
    path("jartest/", include("jartest.urls")),
    path('destw/', include('destw.urls')),
    path('kstw/', include('kstw.urls')),
    path('backwashing/', include('backwashing.urls')),
    path('weeklytasks/', include('weeklytasks.urls')),
    path('xmicalconcentration/', include('xmicalconcentration.urls')),
    path("itinerary/", include("itinerary.urls")),
    path('filtered_water/', include('filtered_water.urls')),
    path('CompanyInfo/', include('CompanyInfo.urls')),
    path("alerts/", include("alerts.urls")),
    path('central_lab/', include('central_lab.urls')),
    path('qc/', include('qc.urls')),
    path("monitoring/", include("monitoring.urls")),
    path("health/", health_live, name="health"),
    path("health/live/", health_live, name="health_live"),
    path("health/ready/", health_ready, name="health_ready"),
]


if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )

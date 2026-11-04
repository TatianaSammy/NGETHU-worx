# accounts/ProfileView.py

import csv
import io
import json
import django
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.models import (
    Group,
    Permission,
)
from django.contrib.sessions.models import Session
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import (
    TemplateView,
    UpdateView,
    ListView,
    CreateView,
    DeleteView,
    DetailView,
    View,
)
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.forms import (
    CustomUserForm,
    ProfilePictureForm,
    SectionForm,
)
from accounts.models import (
    CustomUser,
    audit_user_event,
)

from core.mixins import WanjalaMixin
from core.models import (
    Section,
    SystemConfiguration,
    AuditLogEntry,
    IPRestriction,
    UserSession,
    UserPasswordHistory,
)


ACCOUNT_DASHBOARD_TABS = {
    "profile": "accounts/profile/profile.html",
    "users": "accounts/partials/users_table.html",
    "sections": "accounts/partials/sections_table.html",
    "groups": "accounts/partials/groups_table.html",
    "permissions": "accounts/partials/permissions_table.html",
    "plants": "accounts/partials/plants_table.html",
    "labs": "accounts/partials/labs_table.html",
    "sessions": "accounts/partials/sessions_table.html",
    "security": "accounts/partials/security.html",
    "password": "accounts/partials/reset_password_form.html",
    "configuration": "accounts/partials/configuration.html",
    "system": "accounts/partials/system_information.html",
}


# ==========================================================
# ADMIN MIXIN
# ==========================================================
class SuperUserRequiredMixin(UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_superuser


# ==========================================================
# DASHBOARD
# ==========================================================
class ProfileView(
    WanjalaMixin,
    TemplateView,
):
    template_name = "accounts/dashboard.html"

    def get_template_names(self):
        if not self.request.user.is_superuser:
            return ["accounts/profile/self_profile.html"]
        return [self.template_name]

    def get(self, request, *args, **kwargs):
        export_fmt = request.GET.get("export", "").lower()
        if export_fmt in {"csv", "xlsx", "excel", "json", "docx", "word", "pdf"}:
            if not request.user.is_superuser and not request.user.is_staff:
                from django.core.exceptions import PermissionDenied
                raise PermissionDenied("Only authorized staff or administrators may export user registry data.")
            return self.export_users(request, export_fmt)
        tab = request.GET.get("tab")
        if tab:
            if not request.user.is_superuser:
                from django.core.exceptions import PermissionDenied
                raise PermissionDenied("Account management tabs are available to system administrators.")
            template_name = ACCOUNT_DASHBOARD_TABS.get(tab)
            if template_name is None:
                raise Http404("Unknown Accounts dashboard tab.")
            return render(request, template_name, self.get_dashboard_tab_context(tab))
        return super().get(request, *args, **kwargs)

    def get_users_queryset(self):
        qs = (
            CustomUser.objects
            .select_related("section", "assigned_treatment_plant", "assigned_lab")
            .prefetch_related(
                "groups",
                "user_permissions",
            )
            .order_by("-date_joined", "-pk")
        )

        q = self.request.GET.get("search")

        if q:
            qs = qs.filter(
                Q(username__icontains=q)
                | Q(email__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(mobile_number__icontains=q)
                | Q(section__name__icontains=q)
            )

        status = self.request.GET.get("status")

        filters = {
            "active": {"is_active": True},
            "inactive": {"is_active": False},
            "locked": {"is_locked": True},
            "staff": {"is_staff": True},
            "mfa": {"mfa_enabled": True},
            "verified": {"email_verified": True},
            "superusers": {"is_superuser": True},
        }

        if status in filters:
            qs = qs.filter(**filters[status])

        section_id = self.request.GET.get("section")
        if section_id and section_id.isdigit():
            qs = qs.filter(section_id=int(section_id))

        plant_id = self.request.GET.get("plant")
        if plant_id and plant_id.isdigit():
            qs = qs.filter(assigned_treatment_plant_id=int(plant_id))

        return qs

    def _profile_fields(self, user, now=None, configuration=None):
        now = now or timezone.now()
        if configuration is None:
            configuration = SystemConfiguration.current()
        expiry_days = configuration.password_expire_days if configuration else 90
        password_expiry = (
            user.password_last_set + timezone.timedelta(days=expiry_days)
            if user.password_last_set
            else None
        )
        return {
            "Username": user.username,
            "Email": user.email,
            "First Name": user.first_name,
            "Last Name": user.last_name,
            "Mobile Number": user.mobile_number,
            "Section": user.section,
            "Date Joined": user.date_joined,
            "Last Login": user.last_login,
            "Password Last Set": user.password_last_set,
            "Password Expiry": password_expiry,
            "Failed Login Attempts": user.failed_login_attempts,
            "Email Verified": user.email_verified,
            "MFA Enabled": user.mfa_enabled,
            "Account Locked": user.is_locked,
            "Groups": ", ".join(user.groups.values_list("name", flat=True)),
            "Permissions": ", ".join(user.user_permissions.values_list("codename", flat=True)),
        }

    def _paginated(self, queryset, *, page_size):
        paginator = Paginator(queryset, page_size)
        page_obj = paginator.get_page(self.request.GET.get("page"))
        return {
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": paginator.num_pages > 1,
        }

    def get_dashboard_tab_context(self, tab):
        now = timezone.now()
        configuration = None

        if tab == "profile":
            user = self.request.user
            return {
                "user_obj": user,
                "fields": self._profile_fields(user, now=now),
            }

        if tab == "users":
            from t.models import TreatmentPlant
            users = self.get_users_queryset()
            metrics = users.aggregate(
                active=Count("pk", filter=Q(is_active=True)),
                locked=Count("pk", filter=Q(is_locked=True)),
                mfa=Count("pk", filter=Q(mfa_enabled=True)),
            )
            page = self._paginated(users, page_size=50)
            return {
                **page,
                "users": page["page_obj"],
                "total_users_count": page["paginator"].count,
                "active_users_count": metrics["active"],
                "locked_users_count": metrics["locked"],
                "mfa_users_count": metrics["mfa"],
                "sections": Section.objects.order_by("name"),
                "plants": TreatmentPlant.objects.order_by("name"),
            }

        if tab == "sections":
            sections = Section.objects.annotate(user_count=Count("users", distinct=True)).order_by("name")
            return {
                "sections": sections,
                "active_sections_count": sections.filter(is_active=True).count(),
                "total_section_users": CustomUser.objects.filter(section__isnull=False).count(),
            }

        if tab == "groups":
            return {
                "groups": Group.objects.annotate(
                    user_count=Count("accounts_users", distinct=True)
                ).prefetch_related("permissions").order_by("name"),
            }

        if tab == "permissions":
            permissions = Permission.objects.select_related("content_type").order_by(
                "content_type__app_label", "codename"
            )
            page = self._paginated(permissions, page_size=100)
            return {**page, "permissions": page["page_obj"]}

        if tab == "plants":
            from t.models import TreatmentPlant
            return {"plants": TreatmentPlant.objects.order_by("name")}

        if tab == "labs":
            from laboratory.models import Laboratory
            return {"labs": Laboratory.objects.order_by("name")}

        if tab == "sessions":
            sessions = Session.objects.filter(expire_date__gte=now).order_by("-expire_date")
            page = self._paginated(sessions, page_size=50)
            return {**page, "sessions": page["page_obj"], "now": now}

        if tab == "password" or tab == "configuration":
            configuration = SystemConfiguration.current()
            return {"configuration": configuration}

        if tab == "security":
            return {}

        if tab == "system":
            return {
                "django_version": django.get_version(),
                "environment": "Development" if settings.DEBUG else "Production",
                "now": now,
                "TIME_ZONE": settings.TIME_ZONE,
            }

        return {}

    def export_users(self, request, fmt: str):
        users = self.get_users_queryset()
        headers = [
            "PF Number / Username",
            "Full Name",
            "Email Address",
            "Mobile Number",
            "Section",
            "Assigned Plant",
            "Assigned Laboratory",
            "Account Status",
            "Staff Member",
            "Superuser",
            "MFA Enforced",
            "Email Verified",
            "Failed Login Attempts",
            "Date Joined",
            "Last Login",
        ]

        records = []
        for u in users:
            records.append([
                u.username,
                u.get_full_name() or "—",
                u.email or "—",
                u.mobile_number or "—",
                u.section.name if u.section else "Unassigned",
                u.assigned_treatment_plant.name if u.assigned_treatment_plant else "Global / All Plants",
                u.assigned_lab.name if u.assigned_lab else "—",
                "Locked" if u.is_locked else ("Active" if u.is_active else "Inactive"),
                "Yes" if u.is_staff else "No",
                "Yes" if u.is_superuser else "No",
                "Enabled" if u.mfa_enabled else "Disabled",
                "Verified" if u.email_verified else "Pending",
                u.failed_login_attempts,
                u.date_joined.strftime("%Y-%m-%d %H:%M") if u.date_joined else "—",
                u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else "Never",
            ])

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")

        if fmt == "csv":
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(headers)
            writer.writerows(records)
            response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="qad_user_registry_{timestamp}.csv"'
            return response

        if fmt in {"xlsx", "excel"}:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            wb = Workbook()
            ws = wb.active
            ws.title = "Users Directory"
            ws.append(headers)
            for cell in ws[1]:
                cell.fill = PatternFill("solid", fgColor="0B192C")
                cell.font = Font(color="FFFFFF", bold=True)
                cell.alignment = Alignment(vertical="center")
            for row in records:
                ws.append(row)
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                max_len = max(len(str(c.value or "")) for c in col)
                col_letter = col[0].column_letter
                ws.column_dimensions[col_letter].width = min(36, max(12, max_len + 3))
            output = io.BytesIO()
            wb.save(output)
            response = HttpResponse(
                output.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            response["Content-Disposition"] = f'attachment; filename="qad_user_registry_{timestamp}.xlsx"'
            return response

        if fmt == "json":
            data = [dict(zip(headers, r)) for r in records]
            response = HttpResponse(json.dumps(data, indent=2, default=str), content_type="application/json")
            response["Content-Disposition"] = f'attachment; filename="qad_user_registry_{timestamp}.json"'
            return response

        if fmt in {"doc", "docx", "word"}:
            from docx import Document
            from docx.enum.section import WD_ORIENT
            doc = Document()
            sec = doc.sections[0]
            sec.orientation = WD_ORIENT.LANDSCAPE
            sec.page_width, sec.page_height = sec.page_height, sec.page_width
            doc.add_heading("NCWSCwaterLAB — Enterprise User Registry", 0)
            doc.add_paragraph(f"Report Generated: {timezone.localtime():%Y-%m-%d %H:%M} | Total Users: {len(records)}")
            tbl = doc.add_table(rows=1, cols=len(headers))
            tbl.style = "Light Shading Accent 1"
            for c, h in zip(tbl.rows[0].cells, headers):
                c.text = h
            for r in records:
                row_cells = tbl.add_row().cells
                for c, v in zip(row_cells, r):
                    c.text = str(v or "—")
            output = io.BytesIO()
            doc.save(output)
            response = HttpResponse(
                output.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            response["Content-Disposition"] = f'attachment; filename="qad_user_registry_{timestamp}.docx"'
            return response

        if fmt == "pdf":
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import landscape, letter
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import inch
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
            from xml.sax.saxutils import escape

            output = io.BytesIO()
            doc = SimpleDocTemplate(
                output,
                pagesize=landscape(letter),
                leftMargin=0.3 * inch,
                rightMargin=0.3 * inch,
                topMargin=0.3 * inch,
                bottomMargin=0.3 * inch
            )
            styles = getSampleStyleSheet()
            pdf_cols = ["Username", "Full Name", "Email", "Section", "Plant", "Role", "MFA", "Status", "Last Login"]
            pdf_data = [[Paragraph(f"<b>{escape(c)}</b>", styles["BodyText"]) for c in pdf_cols]]
            for u in users:
                role = "Superuser" if u.is_superuser else ("Staff" if u.is_staff else "User")
                status = "Locked" if u.is_locked else ("Active" if u.is_active else "Inactive")
                pdf_data.append([
                    Paragraph(escape(u.username), styles["BodyText"]),
                    Paragraph(escape(u.get_full_name() or "—"), styles["BodyText"]),
                    Paragraph(escape(u.email or "—"), styles["BodyText"]),
                    Paragraph(escape(u.section.name if u.section else "—"), styles["BodyText"]),
                    Paragraph(escape(u.assigned_treatment_plant.name if u.assigned_treatment_plant else "Global"), styles["BodyText"]),
                    Paragraph(escape(role), styles["BodyText"]),
                    Paragraph(escape("Yes" if u.mfa_enabled else "No"), styles["BodyText"]),
                    Paragraph(escape(status), styles["BodyText"]),
                    Paragraph(escape(u.last_login.strftime("%d %b %Y %H:%M") if u.last_login else "Never"), styles["BodyText"]),
                ])

            avail_w = landscape(letter)[0] - 0.6 * inch
            t = Table(pdf_data, colWidths=[avail_w / len(pdf_cols)] * len(pdf_cols), repeatRows=1, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B192C")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]))

            doc.build([
                Paragraph("NCWSCwaterLAB — User Management Directory", styles["Title"]),
                Paragraph(f"Generated: {timezone.localtime():%Y-%m-%d %H:%M} · Total Users: {len(records)}", styles["Normal"]),
                Spacer(1, 8),
                t
            ])
            response = HttpResponse(output.getvalue(), content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="qad_user_registry_{timestamp}.pdf"'
            return response

        return HttpResponse("Unsupported export format.", status=400, content_type="text/plain")

    def get_context_data(self, **kwargs):
        if not self.request.user.is_superuser:
            user = self.request.user
            return {
                "user_obj": user,
                "fields": {
                    "Username": user.username,
                    "Email": user.email,
                    "Name": user.get_full_name(),
                    "Mobile Number": user.mobile_number,
                    "Section": user.section,
                    "Treatment Plant": user.assigned_treatment_plant,
                    "Laboratory": user.assigned_lab,
                },
            }

        user_metrics = CustomUser.objects.aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(is_active=True)),
            staff=Count("pk", filter=Q(is_staff=True)),
            mfa=Count("pk", filter=Q(mfa_enabled=True)),
            superusers=Count("pk", filter=Q(is_superuser=True)),
            locked=Count("pk", filter=Q(is_locked=True)),
        )
        return {
            "account_dashboard_tabs": [
                {"key": "profile", "id": "profile-tab"},
                {"key": "users", "id": "users-tab"},
                {"key": "sections", "id": "sections-tab"},
                {"key": "groups", "id": "groups-tab"},
                {"key": "permissions", "id": "permissions-tab"},
                {"key": "plants", "id": "plants-tab"},
                {"key": "labs", "id": "labs-tab"},
                {"key": "sessions", "id": "sessions-tab"},
                {"key": "security", "id": "security-tab"},
                {"key": "password", "id": "password-tab"},
                {"key": "configuration", "id": "config-tab"},
                {"key": "system", "id": "system-tab"},
            ],
            "stats": {
                "total_users": user_metrics["total"],
                "active_users": user_metrics["active"],
                "staff_users": user_metrics["staff"],
                "mfa_users": user_metrics["mfa"],
                "superusers": user_metrics["superusers"],
                "locked_users": user_metrics["locked"],
            },
            "recent_users": CustomUser.objects.select_related(
                "section", "assigned_treatment_plant"
            ).order_by("-date_joined", "-pk")[:8],
            "dashboard_counts": {
                "users": user_metrics["total"],
                "sections": Section.objects.count(),
                "groups": Group.objects.count(),
                "permissions": Permission.objects.count(),
            },
        }


# ==========================================================
# PROFILE PICTURE
# ==========================================================
class ProfilePictureUpdateView(
    WanjalaMixin,
    UpdateView,
):
    model = CustomUser
    form_class = ProfilePictureForm
    template_name = "accounts/profile/profile_picture_form.html"

    def get_object(self):
        return self.request.user

    def get_success_url(self):
        return reverse_lazy(
            "accounts:profile"
        )

    def form_valid(self, form):
        response = super().form_valid(form)

        audit_user_event(
            self.request.user,
            "PROFILE_PICTURE_UPDATED",
        )

        messages.success(
            self.request,
            "Profile picture updated successfully.",
        )

        return response


# ==========================================================
# USER VIEWS
# ==========================================================
class UserListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = CustomUser
    template_name = "accounts/dashboard.html"
    context_object_name = "users"
    paginate_by = 50

    def get(self, request, *args, **kwargs):
        return redirect(f"{reverse_lazy('accounts:dashboard')}#users-tab")

    def get_queryset(self):
        return (
            CustomUser.objects
            .select_related("section")
            .prefetch_related(
                "groups",
                "user_permissions",
            )
        )


class UserCreateView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    CreateView,
):
    model = CustomUser
    form_class = CustomUserForm
    template_name = "accounts/profile/user_form.html"
    success_url = reverse_lazy(
        "accounts:profile"
    )

    def form_valid(self, form):
        response = super().form_valid(form)

        audit_user_event(
            self.request.user,
            f"USER_CREATED_{self.object.username}",
        )

        messages.success(
            self.request,
            "User created successfully.",
        )

        return response


class UserUpdateView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    UpdateView,
):
    model = CustomUser
    form_class = CustomUserForm
    template_name = "accounts/profile/user_form.html"
    success_url = reverse_lazy(
        "accounts:profile"
    )

    def form_valid(self, form):
        response = super().form_valid(form)

        audit_user_event(
            self.request.user,
            f"USER_UPDATED_{self.object.username}",
        )

        messages.success(
            self.request,
            "User updated successfully.",
        )

        return response


class UserDeleteView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    DeleteView,
):
    model = CustomUser
    template_name = "accounts/profile/user_confirm_delete.html"
    success_url = reverse_lazy(
        "accounts:profile"
    )

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()

        audit_user_event(
            request.user,
            f"USER_DELETED_{obj.username}",
        )

        messages.success(
            request,
            f"{obj.username} deleted successfully.",
        )

        return super().delete(
            request,
            *args,
            **kwargs,
        )


class UserDetailView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    DetailView,
):
    model = CustomUser
    template_name = "accounts/profile/user_detail.html"
    context_object_name = "user_obj"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        u = self.get_object()
        context["target_user"] = u
        context["user_fields"] = {
            "Username": u.username,
            "Full Name": (u.first_name + " " + u.last_name).strip() or u.username,
            "Email": u.email or "—",
            "Mobile Number": getattr(u, "mobile_number", "—") or "—",
            "Section": getattr(u.section, "name", "—") if getattr(u, "section", None) else "—",
            "Assigned Plant": str(getattr(u, "assigned_treatment_plant", "—") or "—"),
            "Assigned Lab": str(getattr(u, "assigned_lab", "—") or "—"),
            "Status": "Active" if u.is_active else "Inactive",
            "Role": "Superuser" if u.is_superuser else ("Staff" if u.is_staff else "Regular User"),
            "MFA Enabled": "Yes" if getattr(u, "mfa_enabled", False) else "No",
            "Date Joined": u.date_joined.strftime("%d %b %Y, %H:%M") if getattr(u, "date_joined", None) else "—",
            "Last Login": u.last_login.strftime("%d %b %Y, %H:%M") if getattr(u, "last_login", None) else "Never",
        }
        return context


# ==========================================================
# SUPPORTING LIST VIEWS
# ==========================================================
class SectionListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = Section
    context_object_name = "sections"


class SectionCreateView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    CreateView,
):
    model = Section
    form_class = SectionForm
    template_name = "accounts/profile/section_form.html"
    success_url = reverse_lazy("accounts:profile")

    def get_template_names(self):
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            return ["accounts/partials/section_form_modal.html"]
        return [self.template_name]

    def form_valid(self, form):
        self.object = form.save()
        messages.success(
            self.request,
            f"Section '{self.object.name}' created successfully.",
        )
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            return HttpResponse("SUCCESS")
        return super().form_valid(form)

    def form_invalid(self, form):
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            return render(
                self.request,
                "accounts/partials/section_form_modal.html",
                {
                    "form": form,
                    "action_url": self.request.path,
                },
            )
        return super().form_invalid(form)


class SectionUpdateView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    UpdateView,
):
    model = Section
    form_class = SectionForm
    template_name = "accounts/profile/section_form.html"
    success_url = reverse_lazy("accounts:profile")

    def get_template_names(self):
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            return ["accounts/partials/section_form_modal.html"]
        return [self.template_name]

    def form_valid(self, form):
        self.object = form.save()
        messages.success(
            self.request,
            f"Section '{self.object.name}' updated successfully.",
        )
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            return HttpResponse("SUCCESS")
        return super().form_valid(form)

    def form_invalid(self, form):
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            return render(
                self.request,
                "accounts/partials/section_form_modal.html",
                {
                    "form": form,
                    "action_url": self.request.path,
                },
            )
        return super().form_invalid(form)


class SectionDeleteView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    View,
):
    def get(self, request, pk, *args, **kwargs):
        section = get_object_or_404(Section, pk=pk)
        name = section.name
        section.delete()
        messages.success(
            request,
            f"Section '{name}' deleted successfully.",
        )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"status": "success", "message": f"Section '{name}' deleted successfully."})
        return redirect("accounts:profile")

    def post(self, request, pk, *args, **kwargs):
        return self.get(request, pk, *args, **kwargs)


class GroupListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = Group
    context_object_name = "groups"


class PermissionListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = Permission
    context_object_name = "permissions"


class SessionListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = Session
    context_object_name = "sessions"


class AuditLogListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = AuditLogEntry
    context_object_name = "audit_logs"


class IPRestrictionListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = IPRestriction
    context_object_name = "ip_restrictions"


class UserSessionListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = UserSession
    context_object_name = "user_sessions"


class PasswordHistoryListView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    ListView,
):
    model = UserPasswordHistory
    context_object_name = "password_history"


class SystemConfigurationView(
    WanjalaMixin,
    SuperUserRequiredMixin,
    TemplateView,
):
    template_name = "accounts/dashboard.html"

    def get(self, request, *args, **kwargs):
        return redirect(f"{reverse_lazy('accounts:dashboard')}#config-tab")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["configuration"] = (
            SystemConfiguration.current()
        )
        return context

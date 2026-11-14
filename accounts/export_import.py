# accounts/export_import.py
import io
import csv
import json
import logging
from typing import Dict, Any, List, Tuple
from django.http import HttpResponse
from django.utils import timezone
from django.db import transaction
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session

from core.models import Section, AuditLogEntry, IPRestriction
from t.models import TreatmentPlant
from laboratory.models import Laboratory

logger = logging.getLogger(__name__)
User = get_user_model()


# ==============================================================================
# SAMPLE CSV TEMPLATES
# ==============================================================================
SAMPLE_TEMPLATES = {
    "users": {
        "headers": ["username", "first_name", "last_name", "email", "mobile_number", "section", "plant", "is_active", "is_staff"],
        "rows": [
            ["NCWSC99001", "Jane", "Wanjiku", "jwanjiku@ncwsc.co.ke", "0711000111", "LABORATORY", "KSM", "True", "False"],
            ["NCWSC99002", "Peter", "Otieno", "potieno@ncwsc.co.ke", "0722000222", "OPERATIONS", "NRB", "True", "False"],
        ],
    },
    "sections": {
        "headers": ["code", "name", "plant_code", "is_active"],
        "rows": [
            ["SEC_LAB", "Laboratory Testing Section", "KSM", "True"],
            ["SEC_OPS", "Water Operations Section", "NRB", "True"],
        ],
    },
    "groups": {
        "headers": ["name", "permissions"],
        "rows": [
            ["QUALITY_CONTROL_OPERATORS", "view_sample,add_sample,change_sample"],
            ["PLANT_MAINTENANCE_LEADS", "view_equipment,change_equipment"],
        ],
    },
    "permissions": {
        "headers": ["name", "codename", "app_label", "model"],
        "rows": [
            ["Can approve chemical batch", "approve_batch", "ca", "batchrecord"],
            ["Can calibrate sensors", "calibrate_sensor", "qc", "sensorunit"],
        ],
    },
    "plants": {
        "headers": ["code", "name", "location"],
        "rows": [
            ["NGB", "Ngethu Water Treatment Plant", "Ngethu, Kiambu County"],
            ["SST", "Sasumua Treatment Plant", "Sasumua, Nyandarua"],
        ],
    },
    "labs": {
        "headers": ["name", "plant_code"],
        "rows": [
            ["Ngethu Regional Laboratory", "NGB"],
            ["Sasumua Testing Facility", "SST"],
        ],
    },
    "ip_restrictions": {
        "headers": ["name", "ip_range_start", "ip_range_end", "description"],
        "rows": [
            ["Headquarters Subnet", "192.168.1.1", "192.168.1.254", "Main office subnet"],
            ["Plant LAN Gateway", "10.0.0.1", "10.0.0.254", "Treatment plant local gateway"],
        ],
    },
}


def generate_sample_csv(resource: str) -> HttpResponse:
    config = SAMPLE_TEMPLATES.get(resource)
    if not config:
        config = {
            "headers": ["code", "name", "description"],
            "rows": [["SAMPLE_01", "Sample Item 1", "Sample description"]],
        }
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(config["headers"])
    writer.writerows(config["rows"])
    response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="sample_{resource}_import.csv"'
    return response


# ==============================================================================
# PARSING UTILITIES
# ==============================================================================
def parse_uploaded_file(file_obj, filename: str) -> List[Dict[str, str]]:
    name = (filename or "").lower()
    records = []

    if name.endswith(".xlsx") or name.endswith(".xls"):
        from openpyxl import load_workbook
        wb = load_workbook(file_obj, data_only=True)
        sheet = wb.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h).strip().lower().replace(" ", "_") if h else "" for h in rows[0]]
        for row in rows[1:]:
            if not any(row):
                continue
            item = {}
            for col_idx, h in enumerate(headers):
                if h and col_idx < len(row):
                    val = row[col_idx]
                    item[h] = str(val).strip() if val is not None else ""
            if item:
                records.append(item)
        return records

    # CSV / TSV fallback
    content = file_obj.read()
    if isinstance(content, bytes):
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                decoded = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            decoded = content.decode("utf-8", errors="replace")
    else:
        decoded = str(content)

    reader = csv.reader(io.StringIO(decoded))
    headers = []
    for row in reader:
        if not any(row):
            continue
        if not headers:
            headers = [h.strip().lower().replace(" ", "_") for h in row]
            continue
        item = {}
        for idx, h in enumerate(headers):
            if h and idx < len(row):
                item[h] = row[idx].strip()
        if item:
            records.append(item)

    return records


# ==============================================================================
# IMPORT PROCESSORS
# ==============================================================================
def import_users_data(records: List[Dict[str, str]]) -> Tuple[int, int, List[str]]:
    created = 0
    updated = 0
    errors = []

    plants_by_code = {p.code.upper(): p for p in TreatmentPlant.objects.all()}
    plants_by_name = {p.name.upper(): p for p in TreatmentPlant.objects.all()}
    sections_by_name = {s.name.upper(): s for s in Section.objects.all()}
    sections_by_code = {s.code.upper(): s for s in Section.objects.all()}
    labs_by_name = {lab.name.upper(): lab for lab in Laboratory.objects.all()}

    with transaction.atomic():
        for i, row in enumerate(records, start=2):
            username = (row.get("username") or row.get("pf_number") or row.get("pf") or "").strip()
            if not username:
                errors.append(f"Row {i}: Missing username / PF number")
                continue

            sec_val = (row.get("section") or "").strip().upper()
            section_obj = sections_by_code.get(sec_val) or sections_by_name.get(sec_val)

            plant_val = (row.get("plant") or row.get("treatment_plant") or row.get("plant_code") or "").strip().upper()
            plant_obj = plants_by_code.get(plant_val) or plants_by_name.get(plant_val)

            lab_val = (row.get("lab") or row.get("laboratory") or "").strip().upper()
            lab_obj = labs_by_name.get(lab_val)

            is_active = row.get("is_active", "true").lower() in {"1", "true", "yes", "active"}
            is_staff = row.get("is_staff", "false").lower() in {"1", "true", "yes"}

            user = User.objects.filter(username=username).first()
            if user:
                if row.get("first_name"): user.first_name = row["first_name"]
                if row.get("last_name"): user.last_name = row["last_name"]
                if row.get("email"): user.email = row["email"]
                if row.get("mobile_number"): user.mobile_number = row["mobile_number"]
                if section_obj: user.section = section_obj
                if plant_obj: user.assigned_treatment_plant = plant_obj
                if lab_obj: user.assigned_lab = lab_obj
                user.is_active = is_active
                user.is_staff = is_staff
                user.save()
                updated += 1
            else:
                user = User(
                    username=username,
                    first_name=row.get("first_name", ""),
                    last_name=row.get("last_name", ""),
                    email=row.get("email", ""),
                    mobile_number=row.get("mobile_number", ""),
                    section=section_obj,
                    assigned_treatment_plant=plant_obj,
                    assigned_lab=lab_obj,
                    is_active=is_active,
                    is_staff=is_staff,
                )
                user.set_password("NCWSC@2026!")
                user.save()
                created += 1

    return created, updated, errors


def import_sections_data(records: List[Dict[str, str]]) -> Tuple[int, int, List[str]]:
    created = 0
    updated = 0
    errors = []

    plants_by_code = {p.code.upper(): p for p in TreatmentPlant.objects.all()}
    plants_by_name = {p.name.upper(): p for p in TreatmentPlant.objects.all()}

    with transaction.atomic():
        for i, row in enumerate(records, start=2):
            code = (row.get("code") or row.get("section_code") or "").strip()
            name = (row.get("name") or row.get("section_name") or "").strip()
            if not code or not name:
                errors.append(f"Row {i}: Section code and name are required")
                continue

            plant_val = (row.get("plant_code") or row.get("plant") or "").strip().upper()
            plant_obj = plants_by_code.get(plant_val) or plants_by_name.get(plant_val)
            is_active = row.get("is_active", "true").lower() in {"1", "true", "yes", "active"}

            sec, is_new = Section.objects.get_or_create(code=code, defaults={
                "name": name,
                "treatment_plant": plant_obj,
                "is_active": is_active,
            })
            if not is_new:
                sec.name = name
                if plant_obj: sec.treatment_plant = plant_obj
                sec.is_active = is_active
                sec.save()
                updated += 1
            else:
                created += 1

    return created, updated, errors


def import_groups_data(records: List[Dict[str, str]]) -> Tuple[int, int, List[str]]:
    created = 0
    updated = 0
    errors = []

    all_perms = {p.codename.lower(): p for p in Permission.objects.all()}

    with transaction.atomic():
        for i, row in enumerate(records, start=2):
            name = (row.get("name") or row.get("group_name") or "").strip()
            if not name:
                errors.append(f"Row {i}: Group name is required")
                continue

            group, is_new = Group.objects.get_or_create(name=name)
            perm_str = row.get("permissions") or row.get("perms") or ""
            if perm_str:
                perms_to_add = []
                for p_code in perm_str.replace(";", ",").split(","):
                    p_code = p_code.strip().lower()
                    if p_code in all_perms:
                        perms_to_add.append(all_perms[p_code])
                if perms_to_add:
                    group.permissions.add(*perms_to_add)

            if is_new:
                created += 1
            else:
                updated += 1

    return created, updated, errors


def import_permissions_data(records: List[Dict[str, str]]) -> Tuple[int, int, List[str]]:
    created = 0
    updated = 0
    errors = []

    with transaction.atomic():
        for i, row in enumerate(records, start=2):
            name = (row.get("name") or "").strip()
            codename = (row.get("codename") or "").strip()
            app_label = (row.get("app_label") or row.get("app") or "accounts").strip()
            model_name = (row.get("model") or row.get("model_name") or "customuser").strip()

            if not name or not codename:
                errors.append(f"Row {i}: Permission name and codename are required")
                continue

            ct, _ = ContentType.objects.get_or_create(app_label=app_label, model=model_name)
            perm, is_new = Permission.objects.get_or_create(
                content_type=ct,
                codename=codename,
                defaults={"name": name}
            )
            if not is_new and perm.name != name:
                perm.name = name
                perm.save()
                updated += 1
            elif is_new:
                created += 1
            else:
                updated += 1

    return created, updated, errors


def import_plants_data(records: List[Dict[str, str]]) -> Tuple[int, int, List[str]]:
    created = 0
    updated = 0
    errors = []

    with transaction.atomic():
        for i, row in enumerate(records, start=2):
            code = (row.get("code") or "").strip()
            name = (row.get("name") or "").strip()
            location = (row.get("location") or "Nairobi").strip()

            if not code or not name:
                errors.append(f"Row {i}: Plant code and name are required")
                continue

            plant, is_new = TreatmentPlant.objects.get_or_create(code=code, defaults={
                "name": name,
                "location": location,
            })
            if not is_new:
                plant.name = name
                plant.location = location
                plant.save()
                updated += 1
            else:
                created += 1

    return created, updated, errors


def import_labs_data(records: List[Dict[str, str]]) -> Tuple[int, int, List[str]]:
    created = 0
    updated = 0
    errors = []

    plants = {p.code.upper(): p for p in TreatmentPlant.objects.all()}
    plants.update({p.name.upper(): p for p in TreatmentPlant.objects.all()})

    with transaction.atomic():
        for i, row in enumerate(records, start=2):
            name = (row.get("name") or "").strip()
            plant_code = (row.get("plant_code") or row.get("plant") or "").strip().upper()

            if not name:
                errors.append(f"Row {i}: Laboratory name is required")
                continue

            plant_obj = plants.get(plant_code)
            if not plant_obj:
                plant_obj = TreatmentPlant.objects.first()

            if not plant_obj:
                errors.append(f"Row {i}: No treatment plant found to assign laboratory")
                continue

            lab, is_new = Laboratory.objects.get_or_create(
                treatment_plant=plant_obj,
                defaults={"name": name}
            )
            if not is_new:
                lab.name = name
                lab.save()
                updated += 1
            else:
                created += 1

    return created, updated, errors


def import_ip_restrictions_data(records: List[Dict[str, str]]) -> Tuple[int, int, List[str]]:
    created = 0
    updated = 0
    errors = []

    with transaction.atomic():
        for i, row in enumerate(records, start=2):
            name = (row.get("name") or "").strip()
            start = (row.get("ip_range_start") or row.get("start") or "").strip()
            end = (row.get("ip_range_end") or row.get("end") or start).strip()
            desc = (row.get("description") or "").strip()

            if not name or not start:
                errors.append(f"Row {i}: Name and IP range start are required")
                continue

            ip_res, is_new = IPRestriction.objects.get_or_create(name=name, defaults={
                "ip_range_start": start,
                "ip_range_end": end,
                "description": desc,
            })
            if not is_new:
                ip_res.ip_range_start = start
                ip_res.ip_range_end = end
                ip_res.description = desc
                ip_res.save()
                updated += 1
            else:
                created += 1

    return created, updated, errors


RESOURCE_IMPORTERS = {
    "users": import_users_data,
    "sections": import_sections_data,
    "groups": import_groups_data,
    "permissions": import_permissions_data,
    "plants": import_plants_data,
    "labs": import_labs_data,
    "ip_restrictions": import_ip_restrictions_data,
}


def process_import(resource: str, file_obj, filename: str) -> Dict[str, Any]:
    importer = RESOURCE_IMPORTERS.get(resource)
    if not importer:
        return {"status": "error", "message": f"Import not supported for '{resource}'"}

    try:
        records = parse_uploaded_file(file_obj, filename)
        if not records:
            return {"status": "error", "message": "The uploaded file contains no data or could not be parsed."}

        created, updated, errors = importer(records)
        return {
            "status": "success",
            "created": created,
            "updated": updated,
            "errors": errors,
            "total_processed": len(records),
            "message": f"Processed {len(records)} entries: {created} created, {updated} updated.",
        }
    except Exception as exc:
        logger.exception("Error during %s import", resource)
        return {"status": "error", "message": f"Import failed: {str(exc)}"}


# ==============================================================================
# EXPORT DISPATCHER FOR ALL LISTS
# ==============================================================================
def export_dataset(title: str, headers: List[str], records: List[List[Any]], fmt: str, filename_prefix: str) -> HttpResponse:
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    fmt = (fmt or "csv").lower().strip()

    if fmt == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(records)
        response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename_prefix}_{timestamp}.csv"'
        return response

    if fmt in {"xlsx", "excel"}:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = title[:30]
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = PatternFill("solid", fgColor="0B192C")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(vertical="center")
        for row in records:
            ws.append([str(v) if v is not None else "" for v in row])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col in ws.columns:
            max_len = max(len(str(c.value or "")) for c in col)
            col_letter = col[0].column_letter
            ws.column_dimensions[col_letter].width = min(40, max(12, max_len + 3))
        output = io.BytesIO()
        wb.save(output)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename_prefix}_{timestamp}.xlsx"'
        return response

    if fmt == "json":
        data = [dict(zip(headers, r)) for r in records]
        response = HttpResponse(json.dumps(data, indent=2, default=str), content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="{filename_prefix}_{timestamp}.json"'
        return response

    if fmt == "pdf":
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.platypus import SimpleDocTemplate, Table as RLTable, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
        elements = []
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('PDFTitle', parent=styles['Heading1'], fontSize=16, leading=20, textColor=colors.HexColor("#0B192C"))
        elements.append(Paragraph(f"<b>NCWSC QAD</b> — {title}", title_style))
        elements.append(Paragraph(f"<font size='9' color='#64748b'>Generated on {timezone.now().strftime('%d %B %Y at %H:%M')}</font>", styles['Normal']))
        elements.append(Spacer(1, 14))

        safe_data = [[Paragraph(f"<b>{h}</b>", styles['Normal']) for h in headers]]
        for row in records:
            safe_data.append([Paragraph(str(cell or "—"), styles['Normal']) for cell in row])

        table = RLTable(safe_data, repeatRows=1)
        table.setStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ])
        elements.append(table)
        doc.build(elements)
        response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename_prefix}_{timestamp}.pdf"'
        return response

    # Default to CSV
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(records)
    response = HttpResponse("\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename_prefix}_{timestamp}.csv"'
    return response


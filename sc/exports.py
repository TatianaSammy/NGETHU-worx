import csv
import re
from html import escape
from io import BytesIO

from django.http import HttpResponse


HEADERS = (
    "Treatment plant",
    "Week beginning",
    "Week ending",
    "Shift",
    "Time in hours",
    "Public holiday",
    "Week days",
    "Saturday and Sunday",
    "Supervisor",
    "Annual leave / off duty",
)


def export_duty_roster(documents, month_label, file_format):
    rows = list(_table_rows(documents))
    safe_period = re.sub(r"[^A-Za-z0-9]+", "_", month_label).strip("_").lower()
    filename = f"duty_roster_{safe_period or 'schedule'}"
    if file_format == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(HEADERS)
        writer.writerows(rows)
        response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
        return response
    if file_format == "xlsx":
        return _export_xlsx(rows, filename)
    if file_format == "pdf":
        return _export_pdf(rows, filename, month_label)
    if file_format == "docx":
        return _export_docx(rows, filename, month_label)
    return HttpResponse("Unsupported export format.", status=400)


def _table_rows(documents):
    for document in documents:
        plant_name = document["plant"].name
        for row in document["rows"]:
            yield (
                plant_name,
                row["week_start"].strftime("%d %b %Y"),
                row["week_end"].strftime("%d %b %Y"),
                row["shift_name"],
                _time_range(row),
                _join(row.get("holiday")),
                _join(row.get("weekday")),
                _join(row.get("weekend")),
                _join(row.get("supervisors")),
                _join(row.get("annual_leave")),
            )


def _join(values):
    return " / ".join(values) if values else "—"


def _time_range(row):
    start = row.get("start_time")
    end = row.get("end_time")
    if not start or not end:
        return "—"
    return f"{start:%H:%M}-{end:%H:%M}"


def _export_xlsx(rows, filename):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Duty roster"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="15536A")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column_index, width in enumerate((30, 17, 17, 20, 22, 30, 38, 38, 28, 34), start=1):
        sheet.column_dimensions[get_column_letter(column_index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return response


def _export_pdf(rows, filename, month_label):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"Duty roster · {month_label}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("RosterTitle", parent=styles["Title"], alignment=TA_CENTER, textColor=colors.HexColor("#173b4b"), fontSize=15, leading=18, spaceAfter=4)
    cell_style = ParagraphStyle("RosterCell", parent=styles["BodyText"], fontSize=6.5, leading=8)
    header_style = ParagraphStyle("RosterHeader", parent=cell_style, textColor=colors.white, alignment=TA_CENTER, fontName="Helvetica-Bold")
    data = [[Paragraph(escape(value), header_style) for value in HEADERS]]
    data.extend([[Paragraph(escape(str(value)), cell_style) for value in row] for row in rows])
    table = Table(data, repeatRows=1, colWidths=[31 * mm, 18 * mm, 18 * mm, 22 * mm, 22 * mm, 30 * mm, 36 * mm, 34 * mm, 26 * mm, 30 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15536a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#9aa9b1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), (colors.white, colors.HexColor("#f1f6f8"))),
    ]))
    document.build([Paragraph(f"Duty roster - {month_label}", title_style), table])
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return response


def _export_docx(rows, filename, month_label):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = Inches(0.35)
    section.right_margin = Inches(0.35)
    section.top_margin = Inches(0.4)
    section.bottom_margin = Inches(0.4)
    title = document.add_heading(f"Duty roster - {month_label}", level=1)
    title.alignment = 1
    for run in title.runs:
        run.font.color.rgb = RGBColor(21, 83, 106)
        run.font.size = Pt(15)
    table = document.add_table(rows=1, cols=len(HEADERS))
    table.style = "Light Shading Accent 1"
    for cell, header in zip(table.rows[0].cells, HEADERS):
        cell.text = header
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.text = str(value)
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(7)
    output = BytesIO()
    document.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}.docx"'
    return response

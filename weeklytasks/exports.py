import csv
import re
from html import escape
from io import BytesIO

from django.http import HttpResponse


HEADERS = (
    "Week Window",
    "Treatment Plant",
    "Task Plan",
    "Task Code",
    "Staff Member",
    "Assignment Date",
    "Rostered Shift",
)


def export_weekly_tasks(matrix_rows, task_columns, month_label, file_format):
    rows = []
    for row in matrix_rows:
        week_str = f"{row['start_date']:%d %b} – {row['end_date']:%d %b %Y}"
        plant_str = row['plant'].name if row.get('plant') else ""
        for cell in row.get('cells', []):
            task_name = cell['template'].name
            task_code = cell['template'].code
            assignments = []
            for record in cell.get('task_records', []):
                for a in record.get('assignments', []):
                    assignments.append(a)
            if assignments:
                for a in assignments:
                    shift_name = a.get('shift_name') or "—"
                    date_str = f"{a['date']:%Y-%m-%d}" if a.get('date') else "—"
                    rows.append((
                        week_str,
                        plant_str,
                        task_name,
                        task_code,
                        a.get('name') or "—",
                        date_str,
                        shift_name,
                    ))
            else:
                rows.append((
                    week_str,
                    plant_str,
                    task_name,
                    task_code,
                    "Unassigned",
                    "—",
                    "—",
                ))

    safe_period = re.sub(r"[^A-Za-z0-9]+", "_", month_label).strip("_").lower()
    filename = f"weekly_tasks_{safe_period or 'schedule'}"

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


def _export_xlsx(rows, filename):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Weekly Tasks"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="15536A")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = (25, 25, 30, 15, 25, 18, 20)
    for idx, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(idx)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(output.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return response


def _export_pdf(rows, filename, month_label):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"Weekly Tasks · {month_label}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PDFTitle",
        parent=styles["Heading2"],
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#15536A"),
    )
    cell_style = ParagraphStyle(
        "PDFCell",
        parent=styles["Normal"],
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#182230"),
    )
    header_style = ParagraphStyle(
        "PDFHead",
        parent=styles["Normal"],
        fontSize=8,
        leading=11,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )

    story = [
        Paragraph(f"<b>Weekly Tasks Workplan · {escape(month_label)}</b>", title_style),
        Paragraph("<br/>", styles["Normal"]),
    ]

    table_data = [[Paragraph(h, header_style) for h in HEADERS]]
    for r in rows:
        table_data.append([Paragraph(escape(str(v)), cell_style) for v in r])

    t = Table(table_data, colWidths=[38 * mm, 38 * mm, 45 * mm, 20 * mm, 40 * mm, 25 * mm, 30 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15536A")),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#15536A")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)
    doc.build(story)
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return response


def _export_docx(rows, filename, month_label):
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    doc.add_heading(f"Weekly Tasks Workplan · {month_label}", level=1)
    table = doc.add_table(rows=1, cols=len(HEADERS))
    table.style = "Table Grid"
    hdr_cells = table.rows[0].cells
    for i, title in enumerate(HEADERS):
        hdr_cells[i].text = title
        p = hdr_cells[i].paragraphs[0]
        for run in p.runs:
            run.font.bold = True
            run.font.color.rgb = RGBColor(21, 83, 106)

    for row in rows:
        row_cells = table.add_row().cells
        for i, val in enumerate(row):
            row_cells[i].text = str(val)

    output = BytesIO()
    doc.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}.docx"'
    return response


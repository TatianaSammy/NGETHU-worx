from __future__ import annotations

import json
import math
import os
from io import BytesIO
from typing import Any

import qrcode
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph


PAGE_WIDTH, PAGE_HEIGHT = A4

NAVY = colors.HexColor("#0B1736")
CYAN = colors.HexColor("#00B8D9")
CYAN_DARK = colors.HexColor("#087F9C")
BLUE = colors.HexColor("#2563EB")
GREEN = colors.HexColor("#15803D")
GREEN_BG = colors.HexColor("#ECFDF3")
RED = colors.HexColor("#B91C1C")
RED_BG = colors.HexColor("#FEF2F2")
AMBER = colors.HexColor("#B45309")
AMBER_BG = colors.HexColor("#FFF7ED")
SLATE = colors.HexColor("#334155")
MUTED = colors.HexColor("#64748B")
LIGHT = colors.HexColor("#F8FAFC")
LIGHT_BLUE = colors.HexColor("#F0F9FF")
BORDER = colors.HexColor("#CBD5E1")
WHITE = colors.white


def _register_fonts() -> tuple[str, str, str]:
    regular = "Helvetica"
    bold = "Helvetica-Bold"
    italic = "Helvetica-Oblique"

    candidates = (
        (
            "DejaVu",
            "DejaVuSans.ttf",
            "DejaVuBold",
            "DejaVuSans-Bold.ttf",
            "DejaVuOblique",
            "DejaVuSans-Oblique.ttf",
        ),
        (
            "DejaVu",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "DejaVuBold",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuOblique",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        ),
    )

    for (
        regular_name,
        regular_file,
        bold_name,
        bold_file,
        italic_name,
        italic_file,
    ) in candidates:
        try:
            pdfmetrics.registerFont(
                TTFont(regular_name, regular_file)
            )
            pdfmetrics.registerFont(
                TTFont(bold_name, bold_file)
            )

            try:
                pdfmetrics.registerFont(
                    TTFont(italic_name, italic_file)
                )
                italic = italic_name
            except Exception:
                pass

            regular = regular_name
            bold = bold_name
            break
        except Exception:
            continue

    return regular, bold, italic


BODY_FONT, BOLD_FONT, ITALIC_FONT = _register_fonts()


def _safe(value: Any, default: str = "—") -> str:
    if value is None:
        return default

    value = str(value).strip()

    return value if value else default


def _date(value: Any) -> str:
    if not value:
        return "—"

    try:
        return value.strftime("%d %b %Y")
    except Exception:
        return str(value)


def _datetime(value: Any) -> str:
    if not value:
        return "—"

    try:
        return value.strftime("%d %b %Y %H:%M")
    except Exception:
        return str(value)


def _number(value: Any, decimals: int = 4) -> str:
    if value is None:
        return "—"

    try:
        number = float(value)

        if not math.isfinite(number):
            return "—"

        result = f"{number:.{decimals}f}".rstrip("0").rstrip(".")

        return result or "0"
    except (TypeError, ValueError):
        return str(value)


def _choice_display(
    obj: Any,
    field: str,
) -> str:
    display_method = getattr(
        obj,
        f"get_{field}_display",
        None,
    )

    if callable(display_method):
        try:
            return _safe(display_method())
        except Exception:
            pass

    return _safe(
        getattr(obj, field, None)
    )


def _status_text(status: str | None) -> str:
    return {
        "PASS": "PASS",
        "FAIL": "FAIL",
        "NA": "N/A",
    }.get(status, "PENDING")


def _status_colors(status: str | None):
    if status == "PASS":
        return GREEN_BG, GREEN

    if status == "FAIL":
        return RED_BG, RED

    if status == "NA":
        return LIGHT, MUTED

    return AMBER_BG, AMBER


def _method_name(method: Any) -> str:
    if not method:
        return "—"

    for attr in (
        "name",
        "method_name",
        "title",
        "code",
        "reference",
    ):
        value = getattr(method, attr, None)

        if value:
            return str(value)

    return str(method)


def _specification_text(parameter: Any) -> str:
    if parameter is None:
        return "Not specified"

    minimum = getattr(
        parameter,
        "min_limit",
        None,
    )
    maximum = getattr(
        parameter,
        "max_limit",
        None,
    )

    if minimum is not None and maximum is not None:
        return (
            f"{_number(minimum)} – "
            f"{_number(maximum)}"
        )

    if minimum is not None:
        return f"≥ {_number(minimum)}"

    if maximum is not None:
        return f"≤ {_number(maximum)}"

    return "Not specified"


def _serialize_verification_payload(coa: Any) -> str:
    getter = getattr(
        coa,
        "get_verification_payload",
        None,
    )

    if not callable(getter):
        return ""

    payload = getter()

    if isinstance(payload, str):
        return payload

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def generate_qr_code(
    data: Any,
    box_size: int = 8,
    border: int = 2,
) -> BytesIO:
    if not isinstance(data, str):
        data = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=box_size,
        border=border,
    )

    qr.add_data(data)
    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white",
    ).convert("RGBA")

    buffer = BytesIO()

    image.save(
        buffer,
        format="PNG",
        optimize=True,
    )

    buffer.seek(0)

    return buffer


def _load_font(
    size: int,
    bold: bool = False,
):
    candidates = (
        "DejaVuSans-Bold.ttf"
        if bold
        else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    )

    for path in candidates:
        try:
            return ImageFont.truetype(
                path,
                size,
            )
        except Exception:
            continue

    return ImageFont.load_default()


def create_celestial_watermark(
    text: str = "CONTROLLED LABORATORY DOCUMENT",
    font_size: int = 72,
    opacity: int = 24,
) -> Image.Image:
    size = 1100

    image = Image.new(
        "RGBA",
        (size, size),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(image)

    font = _load_font(
        font_size,
        bold=True,
    )

    try:
        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    except Exception:
        text_width, text_height = draw.textsize(
            text,
            font=font,
        )

    x = (size - text_width) / 2
    y = (size - text_height) / 2

    draw.text(
        (x + 4, y + 4),
        text,
        fill=(
            0,
            35,
            80,
            max(5, opacity // 2),
        ),
        font=font,
    )

    draw.text(
        (x, y),
        text,
        fill=(
            0,
            155,
            210,
            opacity,
        ),
        font=font,
    )

    draw.rectangle(
        (
            100,
            100,
            size - 100,
            size - 100,
        ),
        outline=(
            0,
            184,
            217,
            max(5, opacity // 2),
        ),
        width=3,
    )

    return image.rotate(
        35,
        expand=True,
        resample=Image.Resampling.BICUBIC,
    )


def _image_to_reader(
    image: Any,
) -> ImageReader | None:
    if image is None:
        return None

    try:
        if isinstance(image, BytesIO):
            image.seek(0)
            return ImageReader(image)

        if isinstance(
            image,
            (bytes, bytearray),
        ):
            return ImageReader(
                BytesIO(image)
            )

        if isinstance(
            image,
            Image.Image,
        ):
            buffer = BytesIO()

            image.save(
                buffer,
                format="PNG",
            )

            buffer.seek(0)

            return ImageReader(buffer)

        if hasattr(image, "read"):
            try:
                image.seek(0)
            except Exception:
                pass

            return ImageReader(image)

        path = getattr(
            image,
            "path",
            None,
        )

        if path and os.path.exists(path):
            return ImageReader(path)

        if (
            isinstance(image, str)
            and os.path.exists(image)
        ):
            return ImageReader(image)

    except Exception:
        return None

    return None


def _paragraph(
    text: Any,
    width: float,
    font_size: float = 8,
    leading: float | None = None,
    color=SLATE,
    bold: bool = False,
    alignment=TA_LEFT,
):
    if leading is None:
        leading = font_size + 2

    value = _safe(
        text,
        "",
    )

    value = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )

    return Paragraph(
        value,
        ParagraphStyle(
            name="coa_dynamic",
            fontName=(
                BOLD_FONT
                if bold
                else BODY_FONT
            ),
            fontSize=font_size,
            leading=leading,
            textColor=color,
            alignment=alignment,
            spaceAfter=0,
            spaceBefore=0,
        ),
    )


def _draw_paragraph(
    c: canvas.Canvas,
    text: Any,
    x: float,
    y: float,
    width: float,
    font_size: float = 8,
    leading: float | None = None,
    color=SLATE,
    bold: bool = False,
    alignment=TA_LEFT,
):
    paragraph = _paragraph(
        text=text,
        width=width,
        font_size=font_size,
        leading=leading,
        color=color,
        bold=bold,
        alignment=alignment,
    )

    _, height = paragraph.wrap(
        width,
        PAGE_HEIGHT,
    )

    paragraph.drawOn(
        c,
        x,
        y - height,
    )

    return height


def _draw_label_value(
    c,
    label,
    value,
    x,
    y,
    width,
    label_width=70,
):
    c.setFillColor(MUTED)
    c.setFont(
        BOLD_FONT,
        6.5,
    )

    c.drawString(
        x,
        y,
        str(label).upper(),
    )

    _draw_paragraph(
        c,
        value,
        x + label_width,
        y + 1,
        width - label_width,
        font_size=8.2,
        leading=10,
        color=NAVY,
        bold=True,
    )


def _draw_section_header(
    c,
    number,
    title,
    y,
    subtitle=None,
):
    x = 42
    width = PAGE_WIDTH - 84

    c.setFillColor(NAVY)

    c.roundRect(
        x,
        y - 24,
        width,
        24,
        5,
        stroke=0,
        fill=1,
    )

    c.setFillColor(CYAN)

    c.roundRect(
        x,
        y - 24,
        28,
        24,
        5,
        stroke=0,
        fill=1,
    )

    c.setFillColor(WHITE)
    c.setFont(
        BOLD_FONT,
        8,
    )

    c.drawCentredString(
        x + 14,
        y - 16,
        str(number),
    )

    c.setFont(
        BOLD_FONT,
        8.5,
    )

    c.drawString(
        x + 38,
        y - 16,
        str(title).upper(),
    )

    if subtitle:
        c.setFont(
            BODY_FONT,
            6.5,
        )

        c.setFillColor(
            colors.HexColor("#D7E4F5")
        )

        c.drawRightString(
            x + width - 10,
            y - 16,
            str(subtitle),
        )

    return y - 34


def _draw_card(
    c,
    x,
    y,
    width,
    height,
    fill=WHITE,
    stroke=BORDER,
    radius=7,
):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(0.55)

    c.roundRect(
        x,
        y - height,
        width,
        height,
        radius,
        stroke=1,
        fill=1,
    )


def _draw_metric(
    c,
    x,
    y,
    width,
    label,
    value,
    accent=CYAN,
):
    _draw_card(
        c,
        x,
        y,
        width,
        54,
    )

    c.setFillColor(accent)

    c.roundRect(
        x,
        y - 54,
        4,
        54,
        2,
        stroke=0,
        fill=1,
    )

    c.setFillColor(MUTED)
    c.setFont(
        BOLD_FONT,
        6,
    )

    c.drawString(
        x + 12,
        y - 15,
        str(label).upper(),
    )

    c.setFillColor(NAVY)
    c.setFont(
        BOLD_FONT,
        15,
    )

    c.drawString(
        x + 12,
        y - 37,
        str(value),
    )


def _draw_status_badge(
    c,
    status,
    x,
    y,
    width=58,
    height=17,
):
    background, foreground = _status_colors(
        status
    )

    c.setFillColor(background)

    c.roundRect(
        x,
        y - height,
        width,
        height,
        4,
        stroke=0,
        fill=1,
    )

    c.setFillColor(foreground)
    c.setFont(
        BOLD_FONT,
        6.5,
    )

    c.drawCentredString(
        x + width / 2,
        y - 11.5,
        _status_text(status),
    )


def _table_row_height(
    values,
    widths,
    header=False,
):
    height = 24 if header else 25

    for value, width in zip(
        values,
        widths,
    ):
        text = _safe(
            value,
            "",
        )

        paragraph = _paragraph(
            text,
            width - 10,
            font_size=6.7 if header else 6.5,
            leading=8,
            color=WHITE if header else SLATE,
            bold=header,
        )

        _, paragraph_height = paragraph.wrap(
            width - 10,
            200,
        )

        height = max(
            height,
            paragraph_height + 8,
        )

    return min(
        max(height, 24),
        72,
    )


def _draw_grid(
    c,
    x,
    y,
    widths,
    rows,
    header=True,
):
    total_width = sum(widths)
    current_y = y

    for row_index, row in enumerate(rows):
        is_header = (
            header
            and row_index == 0
        )

        row_height = _table_row_height(
            row,
            widths,
            header=is_header,
        )

        fill = (
            NAVY
            if is_header
            else (
                WHITE
                if row_index % 2
                else LIGHT
            )
        )

        text_color = (
            WHITE
            if is_header
            else SLATE
        )

        c.setFillColor(fill)
        c.setStrokeColor(BORDER)

        c.rect(
            x,
            current_y - row_height,
            total_width,
            row_height,
            stroke=1,
            fill=1,
        )

        current_x = x

        for col_index, value in enumerate(row):
            col_width = widths[col_index]

            if col_index:
                c.line(
                    current_x,
                    current_y,
                    current_x,
                    current_y - row_height,
                )

            status_value = (
                value
                if isinstance(value, str)
                else ""
            )

            if (
                not is_header
                and col_index == len(row) - 1
                and status_value
                in {
                    "PASS",
                    "FAIL",
                    "N/A",
                    "PENDING",
                }
            ):
                status = {
                    "PASS": "PASS",
                    "FAIL": "FAIL",
                    "N/A": "NA",
                    "PENDING": None,
                }[status_value]

                _draw_status_badge(
                    c,
                    status,
                    current_x + 4,
                    current_y - 5,
                    width=max(
                        42,
                        col_width - 8,
                    ),
                    height=15,
                )
            else:
                _draw_paragraph(
                    c,
                    value,
                    current_x + 5,
                    current_y - 5,
                    col_width - 10,
                    font_size=(
                        6.7
                        if is_header
                        else 6.5
                    ),
                    leading=8,
                    color=text_color,
                    bold=is_header,
                )

            current_x += col_width

        current_y -= row_height

    return current_y


def _load_qr(
    coa,
    qr_image,
):
    reader = _image_to_reader(
        qr_image
    )

    if reader:
        return reader

    try:
        stored_qr = getattr(
            coa,
            "qr_code",
            None,
        )

        if stored_qr:
            reader = _image_to_reader(
                stored_qr
            )

            if reader:
                return reader
    except Exception:
        pass

    payload = _serialize_verification_payload(
        coa
    )

    if not payload:
        return None

    return _image_to_reader(
        generate_qr_code(payload)
    )


def _load_watermark(
    coa,
    watermark,
):
    reader = _image_to_reader(
        watermark
    )

    if reader:
        return reader

    try:
        stored = getattr(
            coa,
            "watermark",
            None,
        )

        if stored:
            reader = _image_to_reader(
                stored
            )

            if reader:
                return reader
    except Exception:
        pass

    return _image_to_reader(
        create_celestial_watermark()
    )


def _load_footer_image(coa):
    try:
        return _image_to_reader(
            getattr(
                coa,
                "footer_image",
                None,
            )
        )
    except Exception:
        return None


def _draw_watermark(
    c,
    reader,
):
    if reader is None:
        return

    c.saveState()

    try:
        c.setFillAlpha(0.10)
    except Exception:
        pass

    size = 250

    for x in range(
        -100,
        int(PAGE_WIDTH) + 250,
        250,
    ):
        for y in range(
            -100,
            int(PAGE_HEIGHT) + 250,
            250,
        ):
            c.drawImage(
                reader,
                x,
                y,
                width=size,
                height=size,
                preserveAspectRatio=True,
                mask="auto",
            )

    c.restoreState()


def _draw_top_accent(c):
    c.setFillColor(NAVY)

    c.rect(
        0,
        PAGE_HEIGHT - 7,
        PAGE_WIDTH,
        7,
        stroke=0,
        fill=1,
    )

    c.setFillColor(CYAN)

    c.rect(
        0,
        PAGE_HEIGHT - 10,
        PAGE_WIDTH,
        3,
        stroke=0,
        fill=1,
    )


def _draw_header(
    c,
    coa,
):
    margin = 42

    _draw_top_accent(c)

    c.setFillColor(NAVY)

    c.rect(
        0,
        PAGE_HEIGHT - 86,
        PAGE_WIDTH,
        76,
        stroke=0,
        fill=1,
    )

    c.setFillColor(CYAN)

    c.rect(
        margin,
        PAGE_HEIGHT - 78,
        4,
        56,
        stroke=0,
        fill=1,
    )

    plant = getattr(
        getattr(
            coa,
            "treatment_plant",
            None,
        ),
        "name",
        "",
    )

    header = (
        getattr(
            coa,
            "coa_header",
            None,
        )
        or plant
        or "WATER QUALITY & CHEMICAL ANALYSIS LABORATORY"
    )

    c.setFillColor(WHITE)
    c.setFont(
        BOLD_FONT,
        9,
    )

    c.drawString(
        margin + 14,
        PAGE_HEIGHT - 35,
        "NAIROBI CITY WATER & SEWERAGE COMPANY",
    )

    c.setFont(
        BODY_FONT,
        6.5,
    )

    c.setFillColor(
        colors.HexColor("#B8C8E0")
    )

    c.drawString(
        margin + 14,
        PAGE_HEIGHT - 48,
        _safe(header),
    )

    c.setFillColor(WHITE)
    c.setFont(
        BOLD_FONT,
        17,
    )

    c.drawRightString(
        PAGE_WIDTH - margin,
        PAGE_HEIGHT - 36,
        "CERTIFICATE OF ANALYSIS",
    )

    c.setFont(
        BODY_FONT,
        7,
    )

    c.setFillColor(
        colors.HexColor("#C6D6EA")
    )

    c.drawRightString(
        PAGE_WIDTH - margin,
        PAGE_HEIGHT - 50,
        _safe(
            coa.title,
            "Chemical Analysis Certificate",
        ),
    )

    return PAGE_HEIGHT - 104


def _draw_certificate_identity(
    c,
    coa,
    y,
):
    x = 42
    width = PAGE_WIDTH - 84

    _draw_card(
        c,
        x,
        y,
        width,
        72,
    )

    left = x + 14
    right = x + width / 2 + 8

    values = (
        (
            "CERTIFICATE ID",
            (
                f"COA-{coa.pk:06d}"
                if coa.pk
                else "COA-PENDING"
            ),
        ),
        (
            "SAMPLE ID",
            _safe(coa.sample_id),
        ),
        (
            "BATCH ID",
            _safe(coa.batch_id),
        ),
        (
            "REPORT DATE",
            _datetime(coa.date_of_report),
        ),
    )

    for index, (
        label,
        value,
    ) in enumerate(values):
        column = index % 2
        row = index // 2

        draw_x = (
            left
            if column == 0
            else right
        )

        draw_y = (
            y - 19 - row * 27
        )

        _draw_label_value(
            c,
            label,
            value,
            draw_x,
            draw_y,
            width / 2 - 30,
        )

    integrity = getattr(
        coa,
        "integrity_status",
        None,
    )

    badge = {
        "VALID": (
            GREEN,
            "● VERIFIED",
        ),
        "INVALID": (
            RED,
            "● INVALID",
        ),
        "UNVERIFIED": (
            AMBER,
            "● UNVERIFIED",
        ),
    }.get(
        integrity,
        (
            MUTED,
            "● UNAPPROVED",
        ),
    )

    badge_color, badge_text = badge

    c.setFillColor(badge_color)

    c.roundRect(
        x + width - 92,
        y - 20,
        78,
        18,
        5,
        stroke=0,
        fill=1,
    )

    c.setFillColor(WHITE)
    c.setFont(
        BOLD_FONT,
        6.5,
    )

    c.drawCentredString(
        x + width - 53,
        y - 13,
        badge_text,
    )

    return y - 82


def _draw_traceability(
    c,
    coa,
    y,
):
    y = _draw_section_header(
        c,
        "01",
        "Chemical & Delivery Traceability",
        y,
        "CHAIN OF CUSTODY",
    )

    x = 42
    width = PAGE_WIDTH - 84
    gap = 8
    card_width = (
        width - gap
    ) / 2

    quantity = _safe(
        getattr(
            coa,
            "quantity",
            None,
        )
    )

    left_data = (
        (
            "Chemical",
            coa.chemical_name,
        ),
        (
            "Supplier",
            coa.supplier_name,
        ),
        (
            "Quantity",
            quantity,
        ),
        (
            "Delivery Note",
            coa.delivery_note_number,
        ),
        (
            "Delivery Date",
            _date(coa.delivery_date),
        ),
    )

    right_data = (
        (
            "Delivered To",
            coa.delivered_to,
        ),
        (
            "Treatment Plant",
            getattr(
                coa.treatment_plant,
                "name",
                None,
            ),
        ),
        (
            "Packaging",
            _choice_display(
                coa,
                "packaging_status",
            ),
        ),
        (
            "Sampling Date",
            _date(coa.sampling_date),
        ),
        (
            "Analysis Date",
            _date(coa.analysis_date),
        ),
    )

    card_height = 103

    for card_x, data in (
        (x, left_data),
        (
            x + card_width + gap,
            right_data,
        ),
    ):
        _draw_card(
            c,
            card_x,
            y,
            card_width,
            card_height,
            fill=LIGHT,
        )

        current_y = y - 18

        for label, value in data:
            _draw_label_value(
                c,
                label,
                value,
                card_x + 12,
                current_y,
                card_width - 24,
            )

            current_y -= 18

    return y - card_height - 12


def _draw_conformity_summary(
    c,
    coa,
    y,
):
    stats = coa.get_result_statistics()

    y = _draw_section_header(
        c,
        "02",
        "Analytical Conformity",
        y,
        "QUALITY DISPOSITION",
    )

    metrics = (
        (
            "TOTAL",
            stats["total"],
            NAVY,
        ),
        (
            "APPLICABLE",
            stats["applicable"],
            BLUE,
        ),
        (
            "PASSED",
            stats["pass"],
            GREEN,
        ),
        (
            "FAILED",
            stats["fail"],
            RED,
        ),
        (
            "N/A",
            stats["na"],
            MUTED,
        ),
        (
            "PASS RATE",
            f'{stats["pass_rate"]:.1f}%',
            CYAN_DARK,
        ),
    )

    x = 42
    gap = 7
    width = (
        PAGE_WIDTH
        - 84
        - gap * 5
    ) / 6

    for index, (
        label,
        value,
        accent,
    ) in enumerate(metrics):
        _draw_metric(
            c,
            x + index * (
                width + gap
            ),
            y,
            width,
            label,
            value,
            accent,
        )

    y -= 66

    if stats["pending"] > 0:
        disposition = "ANALYSIS INCOMPLETE"
        fill = AMBER_BG
        foreground = AMBER
        symbol = "!"
    elif stats["fail"] > 0:
        disposition = "DOES NOT CONFORM"
        fill = RED_BG
        foreground = RED
        symbol = "×"
    else:
        disposition = "CONFORMS TO SPECIFICATION"
        fill = GREEN_BG
        foreground = GREEN
        symbol = "✓"

    _draw_card(
        c,
        42,
        y,
        PAGE_WIDTH - 84,
        52,
        fill=fill,
        stroke=foreground,
    )

    c.setFillColor(foreground)
    c.setFont(
        BOLD_FONT,
        19,
    )

    c.drawString(
        57,
        y - 34,
        symbol,
    )

    c.setFont(
        BOLD_FONT,
        10,
    )

    c.drawString(
        84,
        y - 23,
        "OVERALL LABORATORY DISPOSITION",
    )

    c.setFont(
        BOLD_FONT,
        12,
    )

    c.drawString(
        84,
        y - 39,
        disposition,
    )

    return y - 64


def _result_rows(coa):
    results = coa.get_results()

    rows = []

    for result in results:
        parameter = getattr(
            result,
            "parameter",
            None,
        )

        parameter_name = (
            getattr(
                parameter,
                "parameter_name",
                None,
            )
            or "Unspecified"
        )

        unit = (
            getattr(
                parameter,
                "unit",
                None,
            )
            or "—"
        )

        specification = _specification_text(
            parameter
        )

        method = _method_name(
            getattr(
                parameter,
                "method",
                None,
            )
        )

        version_value = getattr(
            parameter,
            "version",
            None,
        )

        version = (
            f"v{version_value}"
            if version_value
            else "—"
        )

        if result.value is not None:
            result_value = _number(
                result.value
            )
        elif result.textual_result:
            result_value = str(
                result.textual_result
            )
        else:
            result_value = "—"

        analyst = getattr(
            result,
            "analyst",
            None,
        )

        analyst_name = "—"

        if analyst:
            analyst_name = (
                analyst.get_full_name()
                or getattr(
                    analyst,
                    "username",
                    None,
                )
                or "—"
            )

        traceability_notes = []
        if getattr(result, "reference_standard_name", ""):
            traceability_notes.append(f"Std: {result.reference_standard_name}")
        if getattr(result, "reference_standard_lot", ""):
            traceability_notes.append(f"Std lot: {result.reference_standard_lot}")
        if getattr(result, "reagent_lot_numbers", ""):
            traceability_notes.append(f"Reagent lots: {result.reagent_lot_numbers}")
        if getattr(result, "expanded_uncertainty", None) is not None:
            traceability_notes.append(
                f"U={_number(result.expanded_uncertainty)} (k={_number(result.uncertainty_coverage_factor or 2)})"
            )
        if getattr(result, "instrument_calibration_id", None):
            traceability_notes.append(f"Calibration #{result.instrument_calibration_id}")
        instrument_snapshot = getattr(result, "instrument_snapshot", None) or {}
        if instrument_snapshot.get("name"):
            equipment_note = instrument_snapshot["name"]
            if instrument_snapshot.get("serial_number"):
                equipment_note += f" (SN {instrument_snapshot['serial_number']})"
            traceability_notes.append(f"Equipment: {equipment_note}")
        if instrument_snapshot.get("calibration_status"):
            due = instrument_snapshot.get("calibration_due_date") or "date not recorded"
            traceability_notes.append(f"Calibration {instrument_snapshot['calibration_status']} (due {due})")
        if instrument_snapshot.get("maintenance_status") in {"DUE", "OVERDUE"}:
            traceability_notes.append(
                f"Equipment maintenance {instrument_snapshot['maintenance_status']} "
                f"(due {instrument_snapshot.get('maintenance_due_date') or 'date not recorded'})"
            )
        remark = result.auto_remark or "—"
        if traceability_notes:
            remark = (remark + " · " + "; ".join(traceability_notes))[:500]

        rows.append(
            {
                "parameter": parameter_name,
                "method": method,
                "specification": specification,
                "result": result_value,
                "unit": unit,
                "status": result.status,
                "version": version,
                "analyst": analyst_name,
                "tested_at": _datetime(
                    result.tested_at
                ),
                "remark": remark,
            }
        )

    return rows


def _draw_results_table(
    c,
    coa,
    y,
):
    y = _draw_section_header(
        c,
        "03",
        "Analytical Results",
        y,
        "SPECIFICATION-AWARE RESULTS",
    )

    rows = _result_rows(coa)

    if not rows:
        _draw_card(
            c,
            42,
            y,
            PAGE_WIDTH - 84,
            44,
            fill=AMBER_BG,
            stroke=AMBER,
        )

        c.setFillColor(AMBER)
        c.setFont(
            BOLD_FONT,
            8,
        )

        c.drawString(
            55,
            y - 26,
            "No analytical results available.",
        )

        return y - 56

    table_width = PAGE_WIDTH - 84

    widths = [
        92,
        73,
        79,
        52,
        34,
        table_width - 92 - 73 - 79 - 52 - 34,
    ]

    table_rows = [
        [
            "PARAMETER",
            "METHOD",
            "SPECIFICATION",
            "RESULT",
            "UNIT",
            "STATUS",
        ]
    ]

    table_rows.extend(
        [
            [
                row["parameter"],
                row["method"],
                row["specification"],
                row["result"],
                row["unit"],
                _status_text(
                    row["status"]
                ),
            ]
            for row in rows
        ]
    )

    return _draw_grid(
        c,
        42,
        y,
        widths,
        table_rows,
        header=True,
    ) - 12


def _draw_observations(
    c,
    coa,
    y,
):
    y = _draw_section_header(
        c,
        "04",
        "Laboratory Observations",
        y,
        "CONTROLLED REMARKS",
    )

    observation = (
        coa.remarks
        or coa.generate_remarks()
    )

    summary = (
        coa.results_summary
        or coa.generate_results_summary()
    )

    _draw_card(
        c,
        42,
        y,
        PAGE_WIDTH - 84,
        82,
        fill=LIGHT,
    )

    c.setFillColor(NAVY)
    c.setFont(
        BOLD_FONT,
        7,
    )

    c.drawString(
        55,
        y - 17,
        "CERTIFICATE REMARK",
    )

    _draw_paragraph(
        c,
        observation,
        55,
        y - 24,
        PAGE_WIDTH - 110,
        font_size=8,
        leading=11,
        color=SLATE,
    )

    summary_y = y - 52

    c.setFillColor(MUTED)
    c.setFont(
        BOLD_FONT,
        6.5,
    )

    c.drawString(
        55,
        summary_y,
        "RESULT SUMMARY",
    )

    _draw_paragraph(
        c,
        summary,
        55,
        summary_y - 7,
        PAGE_WIDTH - 110,
        font_size=6.8,
        leading=9,
        color=MUTED,
    )

    return y - 94


def _draw_approval(
    c,
    coa,
    y,
    qr_reader,
    footer_reader=None,
):
    y = _draw_section_header(
        c,
        "05",
        "Authentication & Approval",
        y,
        "DOCUMENT AUTHORITY",
    )

    x = 42
    width = PAGE_WIDTH - 84
    card_height = 155

    _draw_card(
        c,
        x,
        y,
        width,
        card_height,
    )

    approved_by = getattr(
        coa,
        "approved_by",
        None,
    )

    approved_name = "Not approved"

    if approved_by:
        approved_name = (
            approved_by.get_full_name()
            or getattr(
                approved_by,
                "username",
                None,
            )
            or "Approved user"
        )

    approved_username = (
        getattr(
            approved_by,
            "username",
            None,
        )
        if approved_by
        else None
    )

    _draw_label_value(
        c,
        "Approved By",
        approved_name,
        x + 14,
        y - 20,
        230,
    )

    _draw_label_value(
        c,
        "Approval Date",
        _datetime(coa.approved_on),
        x + 14,
        y - 43,
        230,
    )

    _draw_label_value(
        c,
        "Account",
        approved_username,
        x + 14,
        y - 66,
        230,
    )

    _draw_label_value(
        c,
        "Integrity",
        getattr(
            coa,
            "integrity_status",
            None,
        ),
        x + 14,
        y - 89,
        230,
    )

    c.setFillColor(MUTED)
    c.setFont(
        BOLD_FONT,
        6,
    )

    c.drawString(
        x + 14,
        y - 112,
        "SHA-256 DOCUMENT FINGERPRINT",
    )

    _draw_paragraph(
        c,
        coa.verified_hash
        or "Not available",
        x + 14,
        y - 117,
        245,
        font_size=6.5,
        leading=8,
        color=NAVY,
        bold=True,
    )

    qr_x = x + width - 100
    qr_y = y - 12

    c.setFillColor(LIGHT_BLUE)

    c.roundRect(
        qr_x,
        qr_y - 105,
        82,
        105,
        8,
        stroke=0,
        fill=1,
    )

    if qr_reader:
        c.drawImage(
            qr_reader,
            qr_x + 7,
            qr_y - 83,
            width=68,
            height=68,
            preserveAspectRatio=True,
            mask="auto",
        )

    c.setFillColor(NAVY)
    c.setFont(
        BOLD_FONT,
        6,
    )

    c.drawCentredString(
        qr_x + 41,
        qr_y - 95,
        "SCAN TO VERIFY",
    )

    c.setFillColor(MUTED)
    c.setFont(
        BODY_FONT,
        5.5,
    )

    c.drawCentredString(
        qr_x + 41,
        qr_y - 103,
        "Certificate integrity",
    )

    if footer_reader:
        try:
            c.drawImage(
                footer_reader,
                x + 14,
                y - card_height + 8,
                width=180,
                height=25,
                preserveAspectRatio=True,
                anchor="sw",
                mask="auto",
            )
        except Exception:
            pass

    return y - card_height - 12


def _draw_signature(
    c,
    coa,
    y,
):
    approver = getattr(
        coa,
        "approved_by",
        None,
    )

    if not approver:
        return y

    cert = getattr(
        approver,
        "chemicalcertapprover",
        None,
    )

    if not cert:
        return y

    signature = getattr(
        cert,
        "signature",
        None,
    )

    if not signature:
        return y

    reader = _image_to_reader(
        signature
    )

    if not reader:
        return y

    c.setFillColor(MUTED)
    c.setFont(
        BODY_FONT,
        6,
    )

    c.drawString(
        300,
        y,
        "DIGITAL SIGNATURE",
    )

    c.drawImage(
        reader,
        300,
        y - 45,
        width=130,
        height=40,
        preserveAspectRatio=True,
        mask="auto",
    )

    return y - 55


def _draw_technical_traceability(
    c,
    coa,
    y,
):
    y = _draw_section_header(
        c,
        "06",
        "Technical Traceability",
        y,
        "ANALYTICAL AUDIT TRAIL",
    )

    rows = _result_rows(coa)

    if not rows:
        return y - 10

    widths = [
        78,
        75,
        72,
        65,
        100,
        80,
    ]

    table_rows = [
        [
            "PARAMETER",
            "SPEC. VERSION",
            "ANALYST",
            "TESTED",
            "OBSERVATION",
            "METHOD",
        ]
    ]

    table_rows.extend(
        [
            [
                row["parameter"],
                row["version"],
                row["analyst"],
                row["tested_at"],
                row["remark"],
                row["method"],
            ]
            for row in rows
        ]
    )

    return _draw_grid(
        c,
        42,
        y,
        widths,
        table_rows,
        header=True,
    ) - 10


def _draw_control_statement(
    c,
    coa,
    y,
):
    _draw_card(
        c,
        42,
        y,
        PAGE_WIDTH - 84,
        64,
        fill=NAVY,
        stroke=NAVY,
    )

    c.setFillColor(CYAN)
    c.setFont(
        BOLD_FONT,
        7,
    )

    c.drawString(
        55,
        y - 17,
        "DOCUMENT CONTROL",
    )

    text = (
        "This certificate is generated from the controlled "
        "chemical analysis record associated with the stated "
        "batch and sample. Approved certificate content is "
        "protected by the recorded verification hash."
    )

    _draw_paragraph(
        c,
        text,
        55,
        y - 25,
        PAGE_WIDTH - 110,
        font_size=6.5,
        leading=9,
        color=colors.HexColor(
            "#D8E5F5"
        ),
    )


def _draw_footer(
    c,
    coa,
    page_number,
):
    c.saveState()

    c.setFillColor(NAVY)

    c.rect(
        0,
        0,
        PAGE_WIDTH,
        27,
        stroke=0,
        fill=1,
    )

    c.setFillColor(CYAN)

    c.rect(
        0,
        27,
        PAGE_WIDTH,
        2,
        stroke=0,
        fill=1,
    )

    c.setFillColor(
        colors.HexColor("#B8C8E0")
    )

    c.setFont(
        BODY_FONT,
        5.8,
    )

    certificate_id = (
        f"COA-{coa.pk:06d}"
        if coa.pk
        else "COA-PENDING"
    )

    sample_id = _safe(
        coa.sample_id,
        "",
    )

    c.drawString(
        42,
        10,
        f"{certificate_id}  •  {sample_id}",
    )

    c.drawCentredString(
        PAGE_WIDTH / 2,
        10,
        "CONTROLLED LABORATORY DOCUMENT",
    )

    c.drawRightString(
        PAGE_WIDTH - 42,
        10,
        f"PAGE {page_number}",
    )

    c.restoreState()


def _begin_page(
    c,
    coa,
    watermark_reader,
    page_number,
):
    _draw_watermark(
        c,
        watermark_reader,
    )

    y = _draw_header(
        c,
        coa,
    )

    return y


def _finish_page(
    c,
    coa,
    page_number,
):
    _draw_footer(
        c,
        coa,
        page_number,
    )

    c.showPage()


def _draw_compact_coa_results(c, coa, top, bottom):
    """Render every analytical result in the certificate's single-page table."""
    rows = _result_rows(coa)
    x = 42
    widths = [112, 95, 58, 48, PAGE_WIDTH - 84 - 112 - 95 - 58 - 48]
    table_width = sum(widths)
    headers = ["PARAMETER / METHOD", "SPECIFICATION", "RESULT", "STATUS", "ANALYST · TESTED · NOTE"]
    data = []
    for row in rows:
        parameter_method = f'{row["parameter"]}\n{row["method"]} · {row["version"]}'
        specification = row["specification"]
        traceability = f'{row["analyst"]} · {row["tested_at"]}'
        if row["remark"] and row["remark"] != "—":
            traceability += f'\n{row["remark"]}'
        data.append([
            parameter_method,
            specification,
            f'{row["result"]} {row["unit"]}'.strip(),
            _status_text(row["status"]),
            traceability,
        ])

    header_height = 17
    available_height = max(1, top - bottom)
    selected = None
    for font_size in (6.0, 5.6, 5.2, 4.8, 4.4, 4.0, 3.6, 3.2):
        leading = font_size + 1.1
        prepared = []
        total_height = header_height
        for row in data:
            paragraphs = [
                _paragraph(value, width - 8, font_size=font_size, leading=leading, color=SLATE)
                for value, width in zip(row, widths)
            ]
            heights = [paragraph.wrap(width - 8, available_height)[1] for paragraph, width in zip(paragraphs, widths)]
            row_height = max(font_size + 4, max(heights, default=font_size) + 4)
            prepared.append((paragraphs, row_height))
            total_height += row_height
        selected = (font_size, leading, prepared, total_height)
        if total_height <= available_height:
            break

    font_size, leading, prepared, total_height = selected
    vertical_scale = min(1.0, available_height / total_height) if total_height else 1.0
    c.saveState()
    c.translate(0, top * (1 - vertical_scale))
    c.scale(1, vertical_scale)

    current_y = top
    c.setFillColor(NAVY)
    c.rect(x, current_y - header_height, table_width, header_height, stroke=0, fill=1)
    current_x = x
    for title, width in zip(headers, widths):
        _draw_paragraph(c, title, current_x + 4, current_y - 4, width - 8, font_size=5.5, leading=6.2, color=WHITE, bold=True)
        current_x += width
    current_y -= header_height

    if not prepared:
        c.setFillColor(LIGHT)
        c.rect(x, current_y - 23, table_width, 23, stroke=0, fill=1)
        _draw_paragraph(c, "No analytical results are recorded for this certificate.", x + 6, current_y - 7, table_width - 12, font_size=6, leading=7, color=SLATE)
        current_y -= 23
    else:
        for index, (paragraphs, row_height) in enumerate(prepared):
            c.setFillColor(WHITE if index % 2 == 0 else LIGHT)
            c.setStrokeColor(BORDER)
            c.rect(x, current_y - row_height, table_width, row_height, stroke=1, fill=1)
            current_x = x
            for column_index, (paragraph, width) in enumerate(zip(paragraphs, widths)):
                if column_index:
                    c.setStrokeColor(BORDER)
                    c.line(current_x, current_y, current_x, current_y - row_height)
                if column_index == 3:
                    _, status_color = _status_colors(rows[index]["status"])
                    c.setFillColor(status_color)
                    c.setFont(BOLD_FONT, max(3.2, font_size))
                    c.drawString(current_x + 4, current_y - min(row_height - 2, leading + 2), _status_text(rows[index]["status"]))
                else:
                    _, paragraph_height = paragraph.wrap(width - 8, row_height)
                    paragraph.drawOn(c, current_x + 4, current_y - 2 - paragraph_height)
                current_x += width
            current_y -= row_height

    c.restoreState()
    return top - vertical_scale * total_height


def _draw_compact_fitted_paragraph(c, text, x, top, width, max_height, *, font_size=5.3, color=SLATE):
    """Fit free-form certificate text into its reserved one-page area."""
    paragraph = None
    paragraph_height = 0
    for size in (font_size, 5.0, 4.6, 4.2, 3.8, 3.4, 3.0):
        paragraph = _paragraph(text, width, font_size=size, leading=size + 0.8, color=color)
        _, paragraph_height = paragraph.wrap(width, max_height)
        if paragraph_height <= max_height:
            break
    vertical_scale = min(1.0, max_height / paragraph_height) if paragraph_height else 1.0
    c.saveState()
    c.translate(0, top * (1 - vertical_scale))
    c.scale(1, vertical_scale)
    paragraph.drawOn(c, x, top - paragraph_height)
    c.restoreState()


def _draw_compact_coa_body(c, coa, qr_reader, footer_reader, watermark_reader):
    """Draw a complete, printable CoA on one A4 page."""
    _draw_watermark(c, watermark_reader)
    body_top = _draw_header(c, coa)
    margin = 42
    content_width = PAGE_WIDTH - 2 * margin

    # Certificate identifiers.
    identity_top = body_top - 8
    identity_height = 43
    _draw_card(c, margin, identity_top, content_width, identity_height, fill=LIGHT_BLUE)
    identity = [
        ("CERTIFICATE", f"COA-{coa.pk:06d}" if coa.pk else "COA-PENDING"),
        ("SAMPLE", _safe(coa.sample_id)),
        ("BATCH", _safe(coa.batch_id)),
        ("REPORT DATE", _datetime(coa.date_of_report)),
    ]
    cell_width = content_width / len(identity)
    for index, (label, value) in enumerate(identity):
        cell_x = margin + index * cell_width + 9
        c.setFillColor(MUTED)
        c.setFont(BOLD_FONT, 5.3)
        c.drawString(cell_x, identity_top - 14, label)
        _draw_paragraph(c, value, cell_x, identity_top - 19, cell_width - 15, font_size=7, leading=8, color=NAVY, bold=True)
    identity_bottom = identity_top - identity_height

    # Compact, complete custody and delivery information.
    trace_top = identity_bottom - 7
    trace_height = 74
    _draw_card(c, margin, trace_top, content_width, trace_height, fill=LIGHT)
    left_fields = [
        ("Chemical", coa.chemical_name),
        ("Supplier", coa.supplier_name),
        ("Quantity", _safe(getattr(coa, "quantity", None))),
        ("Delivery note", _safe(coa.delivery_note_number)),
        ("Delivery date", _date(coa.delivery_date)),
    ]
    right_fields = [
        ("Delivered to", coa.delivered_to),
        ("Treatment plant", getattr(coa.treatment_plant, "name", None)),
        ("Packaging", _choice_display(coa, "packaging_status")),
        ("Sampling date", _date(coa.sampling_date)),
        ("Analysis date", _date(coa.analysis_date)),
    ]
    gap = 12
    column_width = (content_width - gap - 20) / 2
    for column, fields in enumerate((left_fields, right_fields)):
        column_x = margin + 10 + column * (column_width + gap)
        for index, (label, value) in enumerate(fields):
            line_y = trace_top - 12 - index * 12.5
            c.setFillColor(MUTED)
            c.setFont(BOLD_FONT, 5.1)
            c.drawString(column_x, line_y, f"{label.upper()}:")
            _draw_paragraph(c, value, column_x + 70, line_y + 1, column_width - 72, font_size=6.1, leading=7, color=NAVY, bold=True)
    trace_bottom = trace_top - trace_height

    # Conformity and disposition strip.
    stats = coa.get_result_statistics()
    conformity_top = trace_bottom - 7
    conformity_height = 34
    _draw_card(c, margin, conformity_top, content_width, conformity_height, fill=WHITE)
    metrics = [
        ("RESULTS", stats["total"], NAVY),
        ("PASS", stats["pass"], GREEN),
        ("FAIL", stats["fail"], RED),
        ("PENDING", stats["pending"], AMBER),
        ("PASS RATE", f'{stats["pass_rate"]:.1f}%', CYAN_DARK),
    ]
    metric_width = 63
    for index, (label, value, color) in enumerate(metrics):
        metric_x = margin + 10 + index * metric_width
        c.setFillColor(MUTED)
        c.setFont(BOLD_FONT, 4.8)
        c.drawString(metric_x, conformity_top - 12, label)
        c.setFillColor(color)
        c.setFont(BOLD_FONT, 9)
        c.drawString(metric_x, conformity_top - 26, str(value))
    if stats["pending"]:
        disposition, status_color = "ANALYSIS INCOMPLETE", AMBER
    elif stats["fail"]:
        disposition, status_color = "NON-CONFORMING · REPORT ONLY", RED
    else:
        disposition, status_color = "CONFORMS TO SPECIFICATION", GREEN
    c.setFillColor(status_color)
    c.setFont(BOLD_FONT, 7)
    c.drawRightString(PAGE_WIDTH - margin - 10, conformity_top - 21, disposition)
    conformity_bottom = conformity_top - conformity_height

    # Result table adapts its type size to keep every result on this page.
    results_title_y = conformity_bottom - 13
    c.setFillColor(NAVY)
    c.setFont(BOLD_FONT, 7)
    c.drawString(margin, results_title_y, "SPECIFICATION-AWARE ANALYTICAL RESULTS")
    c.setFillColor(MUTED)
    c.setFont(BODY_FONT, 5.3)
    c.drawRightString(PAGE_WIDTH - margin, results_title_y, "Method, specification, analyst, test time and remark are shown per result")
    table_top = results_title_y - 5
    table_bottom = 188
    _draw_compact_coa_results(c, coa, table_top, table_bottom)

    # Notes, approval evidence and integrity verification share the lower band.
    auth_top = 178
    auth_height = 111
    _draw_card(c, margin, auth_top, content_width, auth_height, fill=LIGHT)
    c.setFillColor(NAVY)
    c.setFont(BOLD_FONT, 6.2)
    c.drawString(margin + 11, auth_top - 13, "CERTIFICATE NOTES & AUTHORIZATION")

    qr_size = 55
    qr_x = PAGE_WIDTH - margin - qr_size - 10
    qr_y = auth_top - 21
    if qr_reader:
        c.drawImage(qr_reader, qr_x, qr_y - qr_size, width=qr_size, height=qr_size, preserveAspectRatio=True, mask="auto")
    c.setFillColor(MUTED)
    c.setFont(BOLD_FONT, 4.7)
    c.drawCentredString(qr_x + qr_size / 2, qr_y - qr_size - 7, "SCAN TO VERIFY")

    text_x = margin + 11
    text_width = qr_x - text_x - 12
    c.setFillColor(MUTED)
    c.setFont(BOLD_FONT, 4.9)
    c.drawString(text_x, auth_top - 27, "REMARKS")
    observation = coa.remarks or coa.generate_remarks() or "—"
    _draw_compact_fitted_paragraph(c, observation, text_x, auth_top - 31, text_width, 21, font_size=5.3, color=SLATE)
    c.setFillColor(MUTED)
    c.setFont(BOLD_FONT, 4.9)
    c.drawString(text_x, auth_top - 53, "RESULT SUMMARY")
    summary = (
        f'{stats["total"]} parameters · {stats["pass"]} pass · '
        f'{stats["fail"]} fail · {stats["pending"]} pending · '
        f'{stats["na"]} not applicable'
    )
    _draw_paragraph(c, summary, text_x, auth_top - 57, text_width, font_size=5.1, leading=5.8, color=SLATE)

    approved_by = getattr(coa, "approved_by", None)
    approved_name = (
        approved_by.get_full_name() or getattr(approved_by, "username", "Approved user")
        if approved_by else "Not approved"
    )
    c.setFillColor(NAVY)
    c.setFont(BOLD_FONT, 5.2)
    c.drawString(text_x, auth_top - 77, f"APPROVED BY: {_safe(approved_name)}   ·   DATE: {_datetime(coa.approved_on)}")
    c.setFillColor(MUTED)
    c.setFont(BODY_FONT, 4.5)
    c.drawString(text_x, auth_top - 88, f"SHA-256: {_safe(coa.verified_hash, 'Not available')}")

    if footer_reader:
        try:
            c.drawImage(footer_reader, text_x, auth_top - auth_height + 5, width=90, height=12, preserveAspectRatio=True, anchor="sw", mask="auto")
        except Exception:
            pass
    signature = getattr(getattr(approved_by, "chemicalcertapprover", None), "signature", None) if approved_by else None
    signature_reader = _image_to_reader(signature) if signature else None
    if signature_reader:
        c.drawImage(signature_reader, text_x + 305, auth_top - 101, width=55, height=12, preserveAspectRatio=True, mask="auto")
        c.setFillColor(MUTED)
        c.setFont(BODY_FONT, 4.2)
        c.drawString(text_x + 305, auth_top - 108, "Digital signature")

    c.setFillColor(MUTED)
    c.setFont(BODY_FONT, 4.5)
    c.drawString(margin, 35, "Controlled laboratory document · Approved content is protected by the recorded verification hash.")


def generate_coa_pdf(
    coa,
    qr_image: BytesIO | None = None,
    watermark: Image.Image | BytesIO | None = None,
) -> BytesIO:
    if coa is None:
        raise ValueError(
            "A ChemicalCoA instance is required."
        )

    buffer = BytesIO()

    pdf = canvas.Canvas(
        buffer,
        pagesize=A4,
        pageCompression=1,
    )

    pdf.setTitle(
        _safe(
            coa.title,
            "Certificate of Analysis",
        )
    )

    pdf.setAuthor(
        "Chemical Analysis Laboratory"
    )

    pdf.setSubject(
        f"Chemical CoA - {_safe(coa.sample_id)}"
    )

    pdf.setCreator(
        "NCWSC Chemical Analysis System"
    )

    watermark_reader = _load_watermark(coa, watermark)
    qr_reader = _load_qr(coa, qr_image)
    footer_reader = _load_footer_image(coa)
    _draw_compact_coa_body(pdf, coa, qr_reader, footer_reader, watermark_reader)
    _draw_footer(pdf, coa, 1)

    pdf.save()

    buffer.seek(0)

    return buffer

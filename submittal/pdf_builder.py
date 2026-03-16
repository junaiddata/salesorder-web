"""
Submittal PDF Builder
Generates title page, index page, section divider pages, materials table
with ReportLab, then merges all sections using PyPDF2.
Divider pages are auto-generated (not uploaded).
"""
import os
from io import BytesIO

from django.conf import settings
from PyPDF2 import PdfMerger
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, BaseDocTemplate, Frame, PageTemplate,
    NextPageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.pdfgen import canvas

from .models import Submittal, SubmittalSectionUpload
from . import services


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BLUE_DARK = HexColor('#003399')
RED_ACCENT = HexColor('#CC0000')
WHITE = colors.white

PAGE_W, PAGE_H = A4

# Standard sections that have content auto-provided (no upload needed)
AUTO_CONTENT_LABELS = {
    "title page", "index",
    "company profile", "trade license",
    "list of proposed material",
    "product catalogue", "technical details",
    "test certificates", "country of origin certificate",
    "previous approvals",
}

# Sections that need per-submittal upload (unless user removed them from index)
UPLOAD_LABELS = {
    "highlighted vendor list", "comply statement with project specification",
    "comply statement", "area of application", "warranty draft letter",
}


DEFAULT_INDEX_ITEMS = [
    "Title Page",
    "Index",
    "Company Profile",
    "Trade License",
    "Highlighted Vendor List",
    "Comply Statement with Project Specification",
    "List of Proposed Material",
    "Area of Application",
    "Product Catalogue",
    "Technical Details",
    "Test Certificates",
    "Country of Origin Certificate",
    "Warranty Draft Letter",
    "Previous Approvals",
]

INDEX_LABEL_TO_SECTION = {
    "title page": 1,
    "index": 2,
    "company profile": 3,
    "trade license": 4,
    "highlighted vendor list": 5,
    "comply statement with project specification": 6,
    "comply statement": 6,
    "list of proposed material": 7,
    "area of application": 8,
    "product catalogue": 9,
    "technical details": 10,
    "test certificates": 11,
    "country of origin certificate": 12,
    "warranty draft letter": 13,
    "previous approvals": 14,
}


def _norm(label: str) -> str:
    return (label or "").strip().lower()


def _label_to_section(label: str):
    return INDEX_LABEL_TO_SECTION.get(_norm(label))


def needs_upload(label: str) -> bool:
    """Return True if this index label requires a per-submittal file upload."""
    key = _norm(label)
    if key in AUTO_CONTENT_LABELS:
        return False
    if key in UPLOAD_LABELS:
        return True
    # Custom (unknown) labels always need an upload
    if key not in INDEX_LABEL_TO_SECTION:
        return True
    return False


# ---------------------------------------------------------------------------
# Shared company header drawing (used by title, index, divider pages)
# ---------------------------------------------------------------------------

def _draw_left_strips(c):
    c.setFillColor(RED_ACCENT)
    c.rect(0, 0, 12, PAGE_H, fill=1, stroke=0)
    c.setFillColor(BLUE_DARK)
    c.rect(12, 0, 8, PAGE_H, fill=1, stroke=0)


def _draw_company_header(c, box_y=None, box_h=120, use_black=False):
    """Draw the company header box. Returns bottom y of the box.
    use_black: if True, use black instead of blue (for compliance statement)."""
    cx = PAGE_W / 2
    if box_y is None:
        box_y = PAGE_H - 170
    box_x = 60
    box_w = PAGE_W - 120

    header_color = colors.black if use_black else BLUE_DARK
    c.setStrokeColor(header_color)
    c.setLineWidth(2)
    c.rect(box_x, box_y, box_w, box_h, fill=0, stroke=1)

    c.setFont('Helvetica-Bold', 15)
    c.setFillColor(header_color)
    c.drawCentredString(cx, box_y + box_h - 26, 'JUNAID SANT. & ELECT. MAT. TR. LLC')

    c.setFont('Helvetica', 8.5)
    c.setFillColor(colors.black)
    info = [
        'P.O. Box 34862, Dubai, U.A.E.',
        'Tel: +971 4 236 7723, Fax: +971 4 236 7750',
        'E-mail: project@junaid.ae',
    ]
    for i, line in enumerate(info):
        c.drawCentredString(cx, box_y + box_h - 46 - i * 13, line)

    return box_y


# ---------------------------------------------------------------------------
# Title Page (Section 1)
# ---------------------------------------------------------------------------

def _build_title_page(submittal: Submittal) -> BytesIO:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    cx = PAGE_W / 2

    _draw_left_strips(c)

    box_y = PAGE_H - 180
    box_x, box_w, box_h = 60, PAGE_W - 120, 130

    c.setStrokeColor(BLUE_DARK)
    c.setLineWidth(2)
    c.rect(box_x, box_y, box_w, box_h, fill=0, stroke=1)

    c.setFont('Helvetica-Bold', 16)
    c.setFillColor(BLUE_DARK)
    c.drawCentredString(cx, box_y + box_h - 30, 'JUNAID SAN & ELE MAT TRDG LLC')

    c.setFont('Helvetica', 9)
    c.setFillColor(colors.black)
    for i, line in enumerate([
        'Dealers in Plumbing & Sanitary ware Products',
        'P.O. Box 34862, Dubai, U.A.E.',
        'Tel: 04-2367723  Fax: 04-2367250',
        'E-mail: project@junaid.ae',
        'Web: www.junaidworld.com',
    ]):
        c.drawCentredString(cx, box_y + box_h - 50 - i * 14, line)

    title_y = box_y - 60
    c.setFont('Helvetica-Bold', 22)
    c.setFillColor(BLUE_DARK)
    c.drawCentredString(cx, title_y, 'MATERIAL SUBMITTAL')

    fields = [
        ('Project', submittal.project),
        ('Client', submittal.client),
        ('Consultant', submittal.consultant),
        ('Main Contractor', submittal.main_contractor),
        ('MEP Contractor', submittal.mep_contractor),
        ('Product', submittal.product),
    ]

    field_y = title_y - 60
    label_x, colon_x, value_x = 90, 210, 220
    max_w = PAGE_W - value_x - 60

    for label, value in fields:
        if not value:
            continue
        c.setFillColor(BLUE_DARK)
        p = c.beginPath()
        p.moveTo(label_x - 18, field_y + 4)
        p.lineTo(label_x - 8, field_y + 8)
        p.lineTo(label_x - 18, field_y + 12)
        p.close()
        c.drawPath(p, fill=1, stroke=0)

        c.setFont('Helvetica-Bold', 11)
        c.setFillColor(colors.black)
        c.drawString(label_x, field_y, label)
        c.drawString(colon_x, field_y, ':')

        c.setFont('Helvetica', 10)
        _draw_wrapped(c, value, value_x, field_y, max_w, 10, 14)

        line_count = max(1, len(value) * 6 / max_w + 1)
        field_y -= max(40, int(line_count) * 16 + 10)

    c.save()
    buf.seek(0)
    return buf


def _draw_wrapped(c, text, x, y, max_w, fs, leading):
    words = text.split()
    line = ''
    cy = y
    for w in words:
        test = f'{line} {w}'.strip()
        if c.stringWidth(test, 'Helvetica', fs) > max_w:
            c.drawString(x, cy, line)
            cy -= leading
            line = w
        else:
            line = test
    if line:
        c.drawString(x, cy, line)


# ---------------------------------------------------------------------------
# Index Page (Section 2)
# ---------------------------------------------------------------------------

def _build_index_page(items: list) -> BytesIO:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    cx = PAGE_W / 2

    _draw_left_strips(c)
    _draw_company_header(c)

    title_y = PAGE_H - 170 - 50
    c.setFont('Helvetica-Bold', 20)
    c.setFillColor(BLUE_DARK)
    c.drawCentredString(cx, title_y, 'INDEX')
    tw = c.stringWidth('INDEX', 'Helvetica-Bold', 20)
    c.setStrokeColor(BLUE_DARK)
    c.setLineWidth(1.5)
    c.line(cx - tw / 2, title_y - 4, cx + tw / 2, title_y - 4)

    row_xl, row_xr = 80, PAGE_W - 80
    row_y = title_y - 36
    row_h = 26

    for idx, label in enumerate(items, 1):
        bg = HexColor('#EEF2F8') if idx % 2 == 0 else WHITE
        c.setFillColor(bg)
        c.rect(row_xl - 6, row_y - 6, row_xr - row_xl + 12, row_h, fill=1, stroke=0)

        c.setFillColor(BLUE_DARK)
        c.roundRect(row_xl - 6, row_y - 4, 26, 20, 4, fill=1, stroke=0)
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(WHITE)
        c.drawCentredString(row_xl + 6, row_y + 3, str(idx))

        c.setFont('Helvetica', 10)
        c.setFillColor(colors.black)
        c.drawString(row_xl + 28, row_y + 3, label)

        c.setStrokeColor(HexColor('#CBD5E1'))
        c.setLineWidth(0.5)
        c.setDash([2, 3])
        lw = c.stringWidth(label, 'Helvetica', 10)
        x1, x2 = row_xl + 30 + lw + 6, row_xr - 28
        if x2 > x1:
            c.line(x1, row_y + 7, x2, row_y + 7)
        c.setDash()
        row_y -= row_h

    c.save()
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Watermark (logo from media)
# ---------------------------------------------------------------------------

WATERMARK_FILENAMES = ('footer-logo1.png', 'footer-logo.png')


def _draw_watermark(c, cx):
    """Draw watermark: logo image from media if available, else text."""
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if not media_root:
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'media')
    media_root = os.path.abspath(str(media_root))

    for filename in WATERMARK_FILENAMES:
        path = os.path.join(media_root, filename)
        if os.path.isfile(path):
            try:
                from PIL import Image
                from reportlab.lib.utils import ImageReader

                im = Image.open(path).convert('RGBA')
                iw, ih = im.size
                # Reduce opacity to ~15% (very light, like old text watermark #E8ECF0)
                data = im.getdata()
                faded = []
                for item in data:
                    r, g, b, a = item
                    faded.append((r, g, b, int(a * 0.15)))
                im.putdata(faded)
                buf = BytesIO()
                im.save(buf, format='PNG')
                buf.seek(0)
                im.close()

                img = ImageReader(buf)
                max_w = 250
                scale = min(1.0, max_w / iw) if iw else 1.0
                w, h = iw * scale, ih * scale
                x = cx - w / 2
                y = PAGE_H / 2 - h / 2
                c.saveState()
                c.drawImage(img, x, y, width=w, height=h, mask='auto')
                c.restoreState()
                return
            except Exception:
                continue
    # Fallback: text watermark
    c.saveState()
    c.setFillColor(HexColor('#E8ECF0'))
    c.setFont('Helvetica-Bold', 42)
    c.drawCentredString(cx, PAGE_H / 2 + 30, 'JUNAID')
    c.setFont('Helvetica', 16)
    c.drawCentredString(cx, PAGE_H / 2 - 5, 'GROUP OF COMPANIES')
    c.restoreState()


# ---------------------------------------------------------------------------
# Section Divider Page (auto-generated, matches uploaded design EXACTLY)
#
# Design: Red+Blue left strips, company header box at top,
# Logo watermark in center (or "JUNAID GROUP OF COMPANIES" text fallback),
# "N. Section Name" centered large text over watermark area.
# Blue+Red bottom bar.
# ---------------------------------------------------------------------------

def _build_divider_page(section_number: int, section_name: str) -> BytesIO:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    cx = PAGE_W / 2

    # Left decorative strips
    _draw_left_strips(c)

    # Company header box at top
    _draw_company_header(c, box_y=PAGE_H - 150, box_h=100)

    # Watermark: logo image from media, or fallback to text
    _draw_watermark(c, cx)

    # Section name (over watermark)
    section_text = section_name
    c.setFont('Helvetica-Bold', 24)
    c.setFillColor(colors.black)
    c.drawCentredString(cx, PAGE_H / 2 + 10, section_text)

    # Bottom decorative bar
    bar_h = 14
    c.setFillColor(BLUE_DARK)
    c.rect(0, 0, PAGE_W * 0.7, bar_h, fill=1, stroke=0)
    c.setFillColor(RED_ACCENT)
    c.rect(PAGE_W * 0.7, 0, PAGE_W * 0.15, bar_h, fill=1, stroke=0)
    c.setFillColor(BLUE_DARK)
    c.rect(PAGE_W * 0.85, 0, PAGE_W * 0.15, bar_h, fill=1, stroke=0)

    c.save()
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Proposed Materials Table (Section 7)
# ---------------------------------------------------------------------------

def _get_effective_columns(submittal, column_override=None):
    """Return list of (key, label) for materials table. Uses materials_columns if set.
    column_override: optional list of keys to filter; None = use submittal.materials_columns."""
    materials = submittal.materials.select_related('brand').all()
    if not materials:
        return [('model_no', 'Model No.'), ('item_description', 'Item Description')]

    seen_keys = {}
    for mat in materials:
        if not mat.brand or not mat.brand.column_definitions:
            continue
        for col in mat.brand.column_definitions:
            key = col.get('key') or col.get('label', '')
            if key and key not in seen_keys:
                seen_keys[key] = col.get('label', key)

    if not seen_keys:
        return [('model_no', 'Model No.'), ('item_description', 'Item Description')]

    first_brand = next((m.brand for m in materials if m.brand and m.brand.column_definitions), None)
    if first_brand:
        ordered = []
        for col in first_brand.column_definitions:
            key = col.get('key')
            if key and key in seen_keys:
                ordered.append((key, seen_keys[key]))
        for k in seen_keys:
            if k not in [x[0] for x in ordered]:
                ordered.append((k, seen_keys[k]))
        cols = ordered
    else:
        cols = list(seen_keys.items())

    sel = column_override if column_override is not None else (submittal.materials_columns or [])
    if sel:
        cols = [(k, lbl) for k, lbl in cols if k in sel]
    return cols if cols else [('model_no', 'Model No.')]


def _get_warranty_columns(submittal):
    """Return columns for warranty materials table. Uses warranty_materials_columns if set, else materials_columns."""
    sel = submittal.warranty_materials_columns if submittal.warranty_materials_columns else None
    return _get_effective_columns(submittal, column_override=sel)


def _build_materials_table(submittal: Submittal) -> BytesIO:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=36, rightMargin=36,
                            topMargin=50, bottomMargin=50)

    style_title = ParagraphStyle(
        'MatTitle', fontSize=14, fontName='Helvetica-Bold',
        textColor=BLUE_DARK, alignment=TA_CENTER, spaceAfter=20,
    )
    style_cell = ParagraphStyle('MatCell', fontSize=8, fontName='Helvetica', leading=10)
    style_header = ParagraphStyle('MatHeader', fontSize=8, fontName='Helvetica-Bold', textColor=WHITE, leading=10)

    elements = [Paragraph('LIST OF PROPOSED MATERIAL', style_title)]
    materials = submittal.materials.select_related('brand').all().order_by('display_order', 'model_no')

    cols = _get_effective_columns(submittal)
    header = [Paragraph('S.No', style_header)] + [
        Paragraph(lbl, style_header) for _, lbl in cols
    ]

    data = [header]
    for idx, mat in enumerate(materials, 1):
        row = [Paragraph(str(idx), style_cell)]
        for key, _ in cols:
            if key == 'model_no':
                val = mat.model_no
            else:
                val = mat.get(key, '')
            row.append(Paragraph(str(val or ''), style_cell))
        data.append(row)

    ncols = len(cols) + 1
    col_widths = [25] + [max(40, 500 // ncols)] * (ncols - 1)
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BLUE_DARK),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, HexColor('#F5F7FA')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    elements.append(table)
    doc.build(elements)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_path(file_field):
    if file_field and file_field.name:
        try:
            return file_field.path
        except (ValueError, FileNotFoundError):
            return None
    return None


def _get_upload_path(submittal, label):
    """Get the per-submittal uploaded file path for a given index label."""
    try:
        upload = SubmittalSectionUpload.objects.get(
            submittal=submittal, index_label=label
        )
        if upload.file and upload.file.name:
            return upload.file.path
    except SubmittalSectionUpload.DoesNotExist:
        pass
    return None


def _get_ordered_index_labels(submittal: Submittal) -> list:
    raw_items = submittal.index_items or []
    labels = []
    for entry in raw_items:
        if isinstance(entry, dict):
            if entry.get('included', True):
                labels.append(entry.get('label', ''))
        elif isinstance(entry, str):
            labels.append(entry)
    return labels if labels else list(DEFAULT_INDEX_ITEMS)


def _append(merger, pdf, add_divider=False, div_num=0, div_name=''):
    """Append optional divider then content to merger."""
    if add_divider and div_name:
        divider = _build_divider_page(div_num, div_name)
        merger.append(divider)
    if pdf is None:
        return
    if isinstance(pdf, BytesIO):
        merger.append(pdf)
    elif isinstance(pdf, str) and os.path.exists(pdf):
        merger.append(pdf)


# ---------------------------------------------------------------------------
# Compliance Statement PDF (Section 6 - generated from form rows)
# ---------------------------------------------------------------------------

def _build_compliance_statement_pdf(submittal: Submittal) -> BytesIO:
    """
    Build a multi-page compliance statement PDF from submittal.compliance_rows.
    First page: company header + horizontal line + title + project details + table.
    Continuation pages: title only + table (no header / project details).
    """
    rows = submittal.compliance_rows or []

    buf = BytesIO()

    # Page frame leaves room for the first-page header content
    FIRST_TOP_MARGIN = 320   # space for header, title, and project details
    LATER_TOP_MARGIN = 170   # space for company header + title on continuation pages
    BOTTOM_MARGIN = 50       # space for "Page X of Y"

    style_cell = ParagraphStyle('CSCell', fontSize=8, fontName='Helvetica', leading=11)
    style_header_cell = ParagraphStyle('CSHdr', fontSize=8, fontName='Helvetica-Bold', textColor=WHITE, leading=10)
    style_bold = ParagraphStyle('CSBold', fontSize=8, fontName='Helvetica-Bold', leading=11)

    def _draw_first_page(canvas_obj, doc):
        """Draw company header, title, and project details on the first page."""
        c = canvas_obj
        cx = PAGE_W / 2

        _draw_left_strips(c)
        _draw_company_header(c, box_y=PAGE_H - 150, box_h=100, use_black=True)

        # Horizontal rule
        rule_y = PAGE_H - 160
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.line(36, rule_y, PAGE_W - 36, rule_y)

        # Title
        title_y = rule_y - 28
        c.setFont('Helvetica-Bold', 16)
        c.setFillColor(colors.black)
        c.drawCentredString(cx, title_y, 'COMPLIANCE STATEMENT')
        tw = c.stringWidth('COMPLIANCE STATEMENT', 'Helvetica-Bold', 16)
        c.setLineWidth(1.2)
        c.line(cx - tw / 2, title_y - 4, cx + tw / 2, title_y - 4)

        # Project details
        fields = [
            ('Project', submittal.project),
            ('Client', submittal.client),
            ('Consultant', submittal.consultant),
            ('Main Contractor', submittal.main_contractor),
            ('MEP Contractor', submittal.mep_contractor),
            ('Product', submittal.product),
        ]
        field_y = title_y - 30
        label_x, colon_x, value_x = 55, 170, 180
        max_w = PAGE_W - value_x - 36

        c.setFont('Helvetica', 9)
        c.setFillColor(colors.black)
        for lbl, val in fields:
            if not val:
                continue
            c.setFont('Helvetica-Bold', 9)
            c.drawString(label_x, field_y, lbl)
            c.drawString(colon_x, field_y, ':')
            c.setFont('Helvetica', 9)
            _draw_wrapped(c, val, value_x, field_y, max_w, 9, 12)
            field_y -= 16

        # Page X of Y at bottom (small)
        page_num = getattr(doc, 'page', 1)
        total_pages = getattr(doc, '_compliance_total_pages', page_num)
        c.setFont('Helvetica', 8)
        c.setFillColor(colors.grey)
        c.drawCentredString(cx, 22, f'Page {page_num} of {total_pages}')

    def _draw_later_page(canvas_obj, doc):
        """Draw company header + title on continuation pages. Page X of Y at bottom."""
        c = canvas_obj
        cx = PAGE_W / 2

        _draw_left_strips(c)
        _draw_company_header(c, box_y=PAGE_H - 150, box_h=100, use_black=True)

        # Horizontal rule
        rule_y = PAGE_H - 160
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.line(36, rule_y, PAGE_W - 36, rule_y)

        # Title (no "continued")
        title_y = rule_y - 28
        c.setFont('Helvetica-Bold', 16)
        c.setFillColor(colors.black)
        c.drawCentredString(cx, title_y, 'COMPLIANCE STATEMENT')
        tw = c.stringWidth('COMPLIANCE STATEMENT', 'Helvetica-Bold', 16)
        c.setLineWidth(1.2)
        c.line(cx - tw / 2, title_y - 4, cx + tw / 2, title_y - 4)

        # Page X of Y at bottom (small)
        page_num = getattr(doc, 'page', 1)
        total_pages = getattr(doc, '_compliance_total_pages', page_num)
        c.setFont('Helvetica', 8)
        c.setFillColor(colors.grey)
        c.drawCentredString(cx, 22, f'Page {page_num} of {total_pages}')

    # Build table data
    col_widths = [28, 235, 100, 155]
    header_row = [
        Paragraph('SR.<br/>NO.', style_header_cell),
        Paragraph('SPECIFICATION', style_header_cell),
        Paragraph('COMPLIANCE', style_header_cell),
        Paragraph('REMARKS', style_header_cell),
    ]
    table_data = [header_row]

    for i, row in enumerate(rows, 1):
        spec = row.get('specification', '') or ''
        compliance = row.get('compliance', '') or ''
        remarks = row.get('remarks', '') or ''
        table_data.append([
            Paragraph(str(i), style_cell),
            Paragraph(str(spec).replace('\n', '<br/>'), style_cell),
            Paragraph(str(compliance), style_bold if compliance else style_cell),
            Paragraph(str(remarks).replace('\n', '<br/>'), style_cell),
        ])

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.black),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, HexColor('#F5F7FA')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))

    first_frame = Frame(
        36, BOTTOM_MARGIN,
        PAGE_W - 72, PAGE_H - FIRST_TOP_MARGIN - BOTTOM_MARGIN,
        id='first',
    )
    later_frame = Frame(
        36, BOTTOM_MARGIN,
        PAGE_W - 72, PAGE_H - LATER_TOP_MARGIN - BOTTOM_MARGIN,
        id='later',
    )

    # Two-pass: first build to get page count, then build with "Page X of Y"
    total_pages = [1]

    def _first_page_cb(c, doc):
        doc._compliance_total_pages = total_pages[0]
        _draw_first_page(c, doc)

    def _later_page_cb(c, doc):
        doc._compliance_total_pages = total_pages[0]
        _draw_later_page(c, doc)

    # First pass: build to temp buffer to get page count
    temp_buf = BytesIO()
    doc_temp = BaseDocTemplate(
        temp_buf,
        pagesize=A4,
        leftMargin=36, rightMargin=36,
        topMargin=FIRST_TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
    )
    doc_temp.addPageTemplates([
        PageTemplate(id='First', frames=[first_frame], onPage=lambda c, d: None),
        PageTemplate(id='Later', frames=[later_frame], onPage=lambda c, d: None),
    ])
    doc_temp.build([NextPageTemplate('Later'), tbl])
    temp_buf.seek(0)
    from PyPDF2 import PdfReader
    total_pages[0] = len(PdfReader(temp_buf).pages)

    # Second pass: build with correct page callbacks
    doc3 = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=36, rightMargin=36,
        topMargin=FIRST_TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
    )
    doc3.addPageTemplates([
        PageTemplate(id='First', frames=[first_frame], onPage=_first_page_cb),
        PageTemplate(id='Later', frames=[later_frame], onPage=_later_page_cb),
    ])
    doc3.build([NextPageTemplate('Later'), tbl])
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Warranty Letter PDF (Section 13 - Pegler-style generated)
# ---------------------------------------------------------------------------

def _build_warranty_letter_pdf(submittal: Submittal) -> BytesIO:
    """
    Build a black-and-white warranty certificate letter with full proposed materials table.
    Replaces single material line with the whole materials table.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=36, rightMargin=36,
                            topMargin=50, bottomMargin=50)

    # Black-and-white styles only
    style_cell = ParagraphStyle('WarrCell', fontSize=8, fontName='Helvetica', leading=10)
    style_header = ParagraphStyle('WarrHeader', fontSize=8, fontName='Helvetica-Bold', textColor=colors.black, leading=10)
    style_body = ParagraphStyle('WarrBody', fontSize=9, fontName='Helvetica', leading=12)

    materials = submittal.materials.select_related('brand').all().order_by('display_order', 'model_no')
    cols = _get_warranty_columns(submittal)

    # Build materials table - black text on white, no background colors
    header_row = [Paragraph('S.No', style_header)] + [
        Paragraph(lbl, style_header) for _, lbl in cols
    ]
    table_data = [header_row]
    for idx, mat in enumerate(materials, 1):
        row = [Paragraph(str(idx), style_cell)]
        for key, _ in cols:
            val = mat.model_no if key == 'model_no' else mat.get(key, '')
            row.append(Paragraph(str(val or ''), style_cell))
        table_data.append(row)

    ncols = len(cols) + 1
    col_widths = [25] + [max(40, 500 // ncols)] * (ncols - 1)
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    date_type = getattr(submittal, 'warranty_date_type', 'toc') or 'toc'
    date_word = 'Invoice' if date_type == 'invoice' else 'TOC'

    elements = [
        Paragraph('TO WHOM IT MAY CONCERN', ParagraphStyle(
            'WarrTitle', fontSize=14, fontName='Helvetica-Bold',
            textColor=colors.black, alignment=TA_CENTER, spaceAfter=16,
        )),
        Paragraph(f'<b>Project:</b> {submittal.project or ""}', style_body),
        Paragraph(f'<b>Employer:</b> {submittal.client or ""}', style_body),
        Paragraph('<b>Subject:</b> Warranty Certificate for Plumbing Valves', style_body),
        Spacer(1, 12),
        Paragraph(
            'This is to confirm that the below following items are manufactured in accordance with ISO 9001:2015 Quality Management Systems.',
            style_body,
        ),
        Spacer(1, 12),
        tbl,
        Spacer(1, 12),
        Paragraph(
            f'It carries warranty for a period of 5 years from date of {date_word}. '
            'This warranty covers defects rising due to faulty manufacture.',
            style_body,
        ),
        Paragraph(
            'It does not extend to defects arising due to incorrect installation/application, misuse or normal wear and tear.',
            style_body,
        ),
        Paragraph(
            'As per the manufacturer guidelines all warranty given as per the supply/invoice date.',
            style_body,
        ),
        Paragraph(
            "And Manufacturer's instructions manual must be strictly complied for warranty claim.",
            style_body,
        ),
        Spacer(1, 24),
        Paragraph('For M/s. Junaid Sanitary Electrical Material Trading LLC', style_body),
        Paragraph('Mr. Junaid Nasheer', style_body),
        Paragraph('Sales Manager', style_body),
    ]

    doc.build(elements)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Main Build Pipeline
# ---------------------------------------------------------------------------

def build_submittal_pdf(submittal_id: int) -> BytesIO:
    """
    Build the complete merged submittal PDF.
    Order follows submittal.index_items. Each included section gets an
    auto-generated divider page (except Title Page) then its content.
    Custom sections use SubmittalSectionUpload for content.
    """
    submittal = Submittal.objects.select_related('warranty_brand').prefetch_related('materials').get(pk=submittal_id)
    company_docs = services.get_company_documents()
    merger = PdfMerger()

    labels = _get_ordered_index_labels(submittal)
    materials = submittal.materials.all().order_by('display_order')
    seen = set()
    visible_num = 0

    for label in labels:
        section = _label_to_section(label)
        visible_num += 1

        # ── Title Page: no divider ──
        if section == 1:
            merger.append(_build_title_page(submittal))
            seen.add(1)
            continue

        # ── Index: no divider ──
        if section == 2:
            merger.append(_build_index_page(labels))
            seen.add(2)
            continue

        # Skip duplicate sections
        if section is not None and section in seen:
            continue
        if section is not None:
            seen.add(section)

        # ── Standard single-content sections ──
        if section == 3:
            _append(merger, _safe_path(company_docs.company_profile_pdf),
                    True, visible_num, label)
            continue

        if section == 4:
            _append(merger, _safe_path(company_docs.trade_license_pdf),
                    True, visible_num, label)
            continue

        if section == 7:
            _append(merger, _build_materials_table(submittal),
                    True, visible_num, label)
            continue

        # ── Upload-based sections (standard or custom) ──
        if section in (5, 6, 8, 13) or section is None:
            # Always add divider; content optional
            merger.append(_build_divider_page(visible_num, label))

            # Section 13: generated warranty letter (when brand has format) OR uploaded PDF
            if section == 13:
                warranty_brand = getattr(submittal, 'warranty_brand', None)
                use_generated = warranty_brand and getattr(warranty_brand, 'use_generated_warranty', False)
                if use_generated:
                    merger.append(_build_warranty_letter_pdf(submittal))
                else:
                    upload_path = _get_upload_path(submittal, label)
                    if not upload_path:
                        upload_path = _safe_path(submittal.warranty_draft_pdf)
                    if upload_path:
                        merger.append(upload_path)
                continue

            upload_path = _get_upload_path(submittal, label)
            if not upload_path:
                legacy_map = {
                    5: submittal.vendor_list_pdf,
                    6: submittal.comply_statement_file,
                    8: submittal.area_of_application_pdf,
                }
                ff = legacy_map.get(section)
                upload_path = _safe_path(ff) if ff else None
            if upload_path:
                merger.append(upload_path)

            # Section 6: also append generated compliance table if rows exist
            if section == 6 and (submittal.compliance_rows or []):
                compliance_pdf = _build_compliance_statement_pdf(submittal)
                merger.append(compliance_pdf)
            continue

        # ── Multi-material sections (9-12, 14) ──
        if section in (9, 10, 11, 12, 14):
            # Always add divider; content optional
            merger.append(_build_divider_page(visible_num, label))
            for mat in materials:
                paths = []
                if section == 9:
                    p = services.get_catalogue_pdf(mat)
                    if p:
                        paths = [p]
                elif section == 10:
                    p = services.get_technical_pdf(mat)
                    if p:
                        paths = [p]
                elif section == 11:
                    paths = services.get_certifications(mat, 'test_certificate')
                elif section == 12:
                    paths = services.get_certifications(mat, 'country_of_origin')
                elif section == 14:
                    paths = services.get_certifications(mat, 'previous_approval')
                for path in paths:
                    if path and os.path.exists(path):
                        merger.append(path)

    output = BytesIO()
    merger.write(output)
    merger.close()
    output.seek(0)
    return output

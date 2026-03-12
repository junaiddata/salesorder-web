"""
Submittal PDF Builder
Generates the title page with ReportLab and merges all 14 sections using PyPDF2.
"""
import os
from io import BytesIO

from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch, mm, cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
)
from reportlab.pdfgen import canvas

from .models import Submittal, CompanyDocuments, SectionDivider
from . import services


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BLUE_DARK = HexColor('#003399')
BLUE_BORDER = HexColor('#003399')
GREY_BG = HexColor('#E8EDF2')
WHITE = colors.white

PAGE_W, PAGE_H = A4  # 595.28 x 841.89 points


# ---------------------------------------------------------------------------
# Title Page Generator (Section 1)
# ---------------------------------------------------------------------------

def _build_title_page(submittal: Submittal) -> BytesIO:
    """Generate the title/cover page matching the Junaid template."""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    margin = 36  # 0.5 inch

    # Background decorative strips (left side)
    c.setFillColor(HexColor('#CC0000'))
    c.rect(0, 0, 12, PAGE_H, fill=1, stroke=0)
    c.setFillColor(HexColor('#003399'))
    c.rect(12, 0, 8, PAGE_H, fill=1, stroke=0)

    # Company header box
    box_x = 60
    box_y = PAGE_H - 180
    box_w = PAGE_W - 120
    box_h = 130

    c.setStrokeColor(BLUE_BORDER)
    c.setLineWidth(2)
    c.rect(box_x, box_y, box_w, box_h, fill=0, stroke=1)

    # Company name
    c.setFont('Helvetica-Bold', 16)
    c.setFillColor(BLUE_DARK)
    cx = PAGE_W / 2
    c.drawCentredString(cx, box_y + box_h - 30, 'JUNAID SAN & ELE MAT TRDG LLC')

    # Subtext
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.black)
    lines = [
        'Dealers in Plumbing & Sanitary ware Products',
        'P.O. Box 34862, Dubai, U.A.E.',
        'Tel: 04-2367723  Fax: 04-2367250',
        'E-mail: project@junaid.ae',
        'Web: www.junaidworld.com',
    ]
    y = box_y + box_h - 50
    for line in lines:
        c.drawCentredString(cx, y, line)
        y -= 14

    # "MATERIAL SUBMITTAL" title
    title_y = box_y - 60
    c.setFont('Helvetica-Bold', 22)
    c.setFillColor(BLUE_DARK)
    c.drawCentredString(cx, title_y, 'MATERIAL SUBMITTAL')

    # Dynamic fields
    fields = [
        ('Project', submittal.project),
        ('Client', submittal.client),
        ('Consultant', submittal.consultant),
        ('Main Contractor', submittal.main_contractor),
        ('MEP Contractor', submittal.mep_contractor),
        ('Product', submittal.product),
    ]

    field_y = title_y - 60
    label_x = 90
    colon_x = 210
    value_x = 220
    max_value_w = PAGE_W - value_x - 60

    for label, value in fields:
        if not value:
            continue

        # Triangle bullet
        c.setFillColor(BLUE_DARK)
        p = c.beginPath()
        p.moveTo(label_x - 18, field_y + 4)
        p.lineTo(label_x - 8, field_y + 8)
        p.lineTo(label_x - 18, field_y + 12)
        p.close()
        c.drawPath(p, fill=1, stroke=0)

        # Label
        c.setFont('Helvetica-Bold', 11)
        c.setFillColor(colors.black)
        c.drawString(label_x, field_y, label)

        # Colon
        c.drawString(colon_x, field_y, ':')

        # Value (wrap long text)
        c.setFont('Helvetica', 10)
        _draw_wrapped_text(c, value, value_x, field_y, max_value_w, 10, 14)

        line_count = max(1, len(value) * 6 / max_value_w + 1)
        field_y -= max(40, int(line_count) * 16 + 10)

    c.save()
    buf.seek(0)
    return buf


def _draw_wrapped_text(c, text, x, y, max_width, font_size, leading):
    """Simple word-wrap drawing on canvas."""
    words = text.split()
    line = ''
    current_y = y
    for word in words:
        test = f'{line} {word}'.strip()
        if c.stringWidth(test, 'Helvetica', font_size) > max_width:
            c.drawString(x, current_y, line)
            current_y -= leading
            line = word
        else:
            line = test
    if line:
        c.drawString(x, current_y, line)


# ---------------------------------------------------------------------------
# Proposed Materials Table (Section 7)
# ---------------------------------------------------------------------------

def _build_materials_table(submittal: Submittal) -> BytesIO:
    """Generate the proposed materials table as a PDF page."""
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=36, rightMargin=36,
                            topMargin=50, bottomMargin=50)

    style_title = ParagraphStyle(
        'MatTitle', fontSize=14, fontName='Helvetica-Bold',
        textColor=BLUE_DARK, alignment=TA_CENTER, spaceAfter=20,
    )
    style_cell = ParagraphStyle(
        'MatCell', fontSize=8, fontName='Helvetica', leading=10,
    )
    style_header = ParagraphStyle(
        'MatHeader', fontSize=8, fontName='Helvetica-Bold',
        textColor=WHITE, leading=10,
    )

    elements = [Paragraph('LIST OF PROPOSED MATERIAL', style_title)]

    materials = submittal.materials.all().order_by('display_order', 'description')

    header = [
        Paragraph('S.No', style_header),
        Paragraph('Item Code', style_header),
        Paragraph('Description', style_header),
        Paragraph('Brand', style_header),
        Paragraph('Size', style_header),
        Paragraph('WRAS No.', style_header),
        Paragraph('Other Certs', style_header),
    ]

    data = [header]
    for idx, mat in enumerate(materials, 1):
        data.append([
            Paragraph(str(idx), style_cell),
            Paragraph(mat.item_code, style_cell),
            Paragraph(mat.description, style_cell),
            Paragraph(mat.brand, style_cell),
            Paragraph(mat.size, style_cell),
            Paragraph(mat.wras_number, style_cell),
            Paragraph(mat.other_certifications, style_cell),
        ])

    col_widths = [30, 65, 160, 70, 50, 70, 80]
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
# PDF Merge Pipeline
# ---------------------------------------------------------------------------

def _append_pdf(merger: PdfMerger, pdf_path_or_buf, section_num=None):
    """Append a divider (if exists) then the section PDF to the merger."""
    if section_num:
        divider_path = services.get_divider_pdf(section_num)
        if divider_path and os.path.exists(divider_path):
            merger.append(divider_path)

    if pdf_path_or_buf is None:
        return

    if isinstance(pdf_path_or_buf, BytesIO):
        merger.append(pdf_path_or_buf)
    elif isinstance(pdf_path_or_buf, str) and os.path.exists(pdf_path_or_buf):
        merger.append(pdf_path_or_buf)


def _safe_file_path(file_field):
    """Return the path of a FileField or None."""
    if file_field and file_field.name:
        try:
            return file_field.path
        except (ValueError, FileNotFoundError):
            return None
    return None


def build_submittal_pdf(submittal_id: int) -> BytesIO:
    """
    Build the complete merged submittal PDF.
    Returns a BytesIO ready for HTTP response.
    """
    submittal = Submittal.objects.prefetch_related('materials').get(pk=submittal_id)
    company_docs = services.get_company_documents()
    merger = PdfMerger()

    # ── Section 1: Title Page (ReportLab) ──
    title_buf = _build_title_page(submittal)
    _append_pdf(merger, title_buf)

    # ── Section 2: Index ──
    if submittal.index_format == 'standard':
        index_path = _safe_file_path(company_docs.index_standard_pdf)
    else:
        index_path = _safe_file_path(submittal.index_client_pdf)
    _append_pdf(merger, index_path, section_num=2)

    # ── Section 3: Company Profile ──
    _append_pdf(merger, _safe_file_path(company_docs.company_profile_pdf), section_num=3)

    # ── Section 4: Trade License ──
    _append_pdf(merger, _safe_file_path(company_docs.trade_license_pdf), section_num=4)

    # ── Section 5: Vendor List ──
    _append_pdf(merger, _safe_file_path(submittal.vendor_list_pdf), section_num=5)

    # ── Section 6: Comply Statement ──
    _append_pdf(merger, _safe_file_path(submittal.comply_statement_file), section_num=6)

    # ── Section 7: List of Proposed Material ──
    materials_buf = _build_materials_table(submittal)
    _append_pdf(merger, materials_buf, section_num=7)

    # ── Section 8: Area of Application ──
    _append_pdf(merger, _safe_file_path(submittal.area_of_application_pdf), section_num=8)

    # ── Section 9: Product Catalogue (per material) ──
    materials = submittal.materials.all().order_by('display_order')
    catalogue_added = False
    for mat in materials:
        cat_path = services.get_catalogue_pdf(mat)
        if cat_path:
            if not catalogue_added:
                divider = services.get_divider_pdf(9)
                if divider and os.path.exists(divider):
                    merger.append(divider)
                catalogue_added = True
            merger.append(cat_path)

    # ── Section 10: Technical Details (per material) ──
    tech_added = False
    for mat in materials:
        tech_path = services.get_technical_pdf(mat)
        if tech_path:
            if not tech_added:
                divider = services.get_divider_pdf(10)
                if divider and os.path.exists(divider):
                    merger.append(divider)
                tech_added = True
            merger.append(tech_path)

    # ── Section 11: Test Certificates ──
    certs_added = False
    for mat in materials:
        cert_paths = services.get_certifications(mat, 'test_certificate')
        for cp in cert_paths:
            if not certs_added:
                divider = services.get_divider_pdf(11)
                if divider and os.path.exists(divider):
                    merger.append(divider)
                certs_added = True
            merger.append(cp)

    # ── Section 12: Country of Origin ──
    origin_added = False
    for mat in materials:
        origin_paths = services.get_certifications(mat, 'country_of_origin')
        for op in origin_paths:
            if not origin_added:
                divider = services.get_divider_pdf(12)
                if divider and os.path.exists(divider):
                    merger.append(divider)
                origin_added = True
            merger.append(op)

    # ── Section 13: Warranty Draft ──
    _append_pdf(merger, _safe_file_path(submittal.warranty_draft_pdf), section_num=13)

    # ── Section 14: Previous Approvals ──
    approvals_added = False
    for mat in materials:
        approval_paths = services.get_certifications(mat, 'previous_approval')
        for ap in approval_paths:
            if not approvals_added:
                divider = services.get_divider_pdf(14)
                if divider and os.path.exists(divider):
                    merger.append(divider)
                approvals_added = True
            merger.append(ap)

    # ── Write final merged PDF ──
    output = BytesIO()
    merger.write(output)
    merger.close()
    output.seek(0)
    return output

"""
Warranty Letter PDF Builder.

Builds a single-page (typically) formal warranty letter with ReportLab:
Ref No/Date, a centered title, a Project/Client/Consultant/Main Contractor
label block, an intro sentence, an items table ending in "END OF LIST",
warranty terms as a bullet list, and a sign-off block with the signature
and stamp images placed inline once uploaded. Letterhead (header logo,
footer banner, DRAFT watermark while not yet Approved) is drawn on every
page via a PageTemplate onPage callback, mirroring the pattern used for
the Submittal app's internal warranty letter.
"""
import os
from io import BytesIO

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem, Image,
)

from .models import WarrantyLetterSettings

PAGE_W, PAGE_H = A4

HEADER_LOGO_BOX = (160, 55)   # (max width, max height)
FOOTER_IMAGE_BOX = (PAGE_W - 60, 75)


def _fit_image_flowable(path, max_w, max_h):
    """Return a reportlab Image flowable scaled to fit within max_w/max_h,
    preserving aspect ratio (platypus Image doesn't do this on its own)."""
    im = PILImage.open(path)
    iw, ih = im.size
    im.close()
    scale = min(max_w / iw, max_h / ih) if iw and ih else 1.0
    return Image(path, width=iw * scale, height=ih * scale)


class _WarrantyLetterDocTemplate(BaseDocTemplate):
    """Doc template that draws the header logo, footer banner, and (while
    the letter isn't yet Approved) a DRAFT watermark on every page."""

    def __init__(self, buf, company='junaid', status='Draft', **kwargs):
        self._settings = WarrantyLetterSettings.get_instance(company)
        self._status = status
        super().__init__(buf, **kwargs)
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id='normal')
        template = PageTemplate(id='warranty', frames=[frame], onPage=self._draw_letterhead)
        self.addPageTemplates([template])

    def _draw_letterhead(self, c, doc):
        c.saveState()

        if self._settings.header_logo and os.path.exists(self._settings.header_logo.path):
            logo_w, logo_h = HEADER_LOGO_BOX
            c.drawImage(
                self._settings.header_logo.path,
                PAGE_W - 40 - logo_w, PAGE_H - 40 - logo_h,
                width=logo_w, height=logo_h,
                preserveAspectRatio=True, anchor='n', mask='auto',
            )

        if self._settings.footer_image and os.path.exists(self._settings.footer_image.path):
            footer_w, footer_h = FOOTER_IMAGE_BOX
            c.drawImage(
                self._settings.footer_image.path,
                (PAGE_W - footer_w) / 2, 15,
                width=footer_w, height=footer_h,
                preserveAspectRatio=True, anchor='s', mask='auto',
            )

        if self._status != 'Approved':
            c.setFont('Helvetica-Bold', 72)
            c.setFillColor(colors.Color(0.75, 0.75, 0.75, alpha=0.35))
            c.translate(PAGE_W / 2, PAGE_H / 2)
            c.rotate(45)
            c.drawCentredString(0, 0, 'DRAFT')

        c.restoreState()


def build_warranty_letter_pdf(letter) -> BytesIO:
    buf = BytesIO()
    settings_row = WarrantyLetterSettings.get_instance(letter.company)
    doc = _WarrantyLetterDocTemplate(
        buf,
        company=letter.company,
        status=letter.status,
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=90,     # room for header logo
        bottomMargin=105,  # room for footer banner
    )

    style_body = ParagraphStyle('WLBody', fontSize=10, fontName='Helvetica', leading=14, spaceAfter=4)
    style_body_bold = ParagraphStyle('WLBodyBold', parent=style_body, fontName='Helvetica-Bold')
    style_title = ParagraphStyle(
        'WLTitle', fontSize=13, fontName='Helvetica-Bold',
        alignment=TA_CENTER, spaceBefore=6, spaceAfter=14, textColor=colors.black,
    )
    style_label = ParagraphStyle('WLLabel', fontSize=10, fontName='Helvetica-Bold', leading=14)
    style_label_val = ParagraphStyle('WLLabelVal', fontSize=10, fontName='Helvetica', leading=14)
    style_cell = ParagraphStyle('WLCell', fontSize=9, fontName='Helvetica', leading=11)
    style_header = ParagraphStyle('WLHeader', fontSize=9, fontName='Helvetica-Bold', leading=11, textColor=colors.black)

    def _lbl_row(label, value):
        return [Paragraph(label, style_label), Paragraph(':', style_label), Paragraph(value, style_label_val)]

    lbl_w, col_w = 120, 10
    val_w = doc.width - lbl_w - col_w
    label_rows = [
        row for row in (
            _lbl_row('Project Name', letter.project),
            _lbl_row('Client', letter.client),
            _lbl_row('Consultant', letter.consultant),
            _lbl_row('Main Contractor', letter.main_contractor),
        ) if row[2].text
    ]
    label_tbl = Table(label_rows, colWidths=[lbl_w, col_w, val_w]) if label_rows else None
    if label_tbl:
        label_tbl.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))

    # ── Items table ────────────────────────────────────────────────────
    headers = ['Date', 'Invoice No.', 'LPO No.', 'Brand/Model', 'Total Qty.']
    col_widths = [55, 85, 85, 190, 50]
    table_data = [[Paragraph(h, style_header) for h in headers]]
    for item in letter.items.all():
        table_data.append([
            Paragraph(item.date, style_cell),
            Paragraph(item.invoice_no, style_cell),
            Paragraph(item.lpo_no, style_cell),
            Paragraph(item.brand_model, style_cell),
            Paragraph(item.total_qty, style_cell),
        ])
    end_row_idx = len(table_data)
    table_data.append([Paragraph('END OF LIST', style_header), '', '', '', ''])

    items_tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ('SPAN', (0, end_row_idx), (-1, end_row_idx)),
        ('ALIGN', (0, end_row_idx), (-1, end_row_idx), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    # ── Warranty terms bullets ───────────────────────────────────────────
    terms_lines = [ln.strip() for ln in (letter.terms_text or '').splitlines() if ln.strip()]
    terms_list = ListFlowable(
        [ListItem(Paragraph(line, style_body), leftIndent=6) for line in terms_lines],
        bulletType='bullet', start='circle', leftIndent=14, bulletFontSize=6,
    ) if terms_lines else None

    # ── Sign-off block ────────────────────────────────────────────────
    sig_flowable = None
    if letter.signature_image and os.path.exists(letter.signature_image.path):
        sig_flowable = _fit_image_flowable(letter.signature_image.path, 130, 50)
    stamp_flowable = None
    if letter.stamp_image and os.path.exists(letter.stamp_image.path):
        stamp_flowable = _fit_image_flowable(letter.stamp_image.path, 90, 90)

    sign_table = Table(
        [[sig_flowable or Spacer(1, 50), stamp_flowable or Spacer(1, 50)]],
        colWidths=[200, 100],
    )
    sign_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'LEFT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))

    # ── Assemble ─────────────────────────────────────────────────────
    elements = [
        Paragraph(f'Ref: {letter.ref_no}', style_body) if letter.ref_no else Spacer(1, 0),
        Spacer(1, 4),
        Paragraph(f"Date: {letter.letter_date.strftime('%B %d, %Y')}", style_body),
        Spacer(1, 6),
        Paragraph('<u>WARRANTY LETTER</u>', style_title),
    ]
    if label_tbl:
        elements += [label_tbl, Spacer(1, 12)]

    # Intro text: one line per paragraph (mirrors how Warranty Terms splits
    # by line), but rendered as plain paragraphs -- no bullet markers.
    intro_lines = [ln.strip() for ln in (letter.intro_text or '').splitlines() if ln.strip()]
    if intro_lines:
        elements += [Paragraph(line, style_body) for line in intro_lines]
        elements.append(Spacer(1, 10))

    elements += [items_tbl, Spacer(1, 12)]

    if terms_list:
        elements += [terms_list, Spacer(1, 16)]

    elements += [
        Paragraph('For,', style_body),
        Paragraph(f'M/s. {settings_row.legal_company_name}', style_body_bold),
        Spacer(1, 8),
        sign_table,
        Spacer(1, 4),
    ]
    if letter.signatory_name:
        elements.append(Paragraph(f'<b>{letter.signatory_name}</b>', style_body))
    if letter.signatory_title:
        elements.append(Paragraph(letter.signatory_title, style_body))

    doc.build(elements)
    buf.seek(0)
    return buf

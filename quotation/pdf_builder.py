"""
Quotation PDF builder.

The body (title, To / QTN block, items table with photos and datasheet download links,
total, remarks, terms, bank details, sales contact) is laid out with ReportLab
inside a frame that leaves room for the letterhead, then each page is stamped on
top of media/JUNAID_GALLERIA1.pdf with PyPDF2. Nothing here is shared with any
other app's PDF code.
"""
import os
import re
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

from django.conf import settings
from PIL import Image as PILImage, ImageOps
from PyPDF2 import PageObject, PdfReader, PdfWriter, Transformation
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus.doctemplate import LayoutError

from .models import VAT_RATE
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer,
    Table, TableStyle,
)

# The PDF is A4 (what UAE printers use), portrait or landscape. The letterhead (media/JUNAID_GALLERIA1.pdf) is an A4
# portrait page with the brand logos box top left, the company logo top right and the contact details bottom right.
MARGIN_X = 30                    # printers cannot print the outer ~12pt, so keep well inside it
LETTERHEAD_PATH = os.path.join(settings.MEDIA_ROOT, 'JUNAID_GALLERIA1.pdf')
LH_W, LH_H = 595.5, 842.25       # letterhead page size
LH_HEADER_BOTTOM = 84            # portrait: the header artwork ends 84pt from the top of the letterhead page
LH_FOOTER_TOP = 766              # portrait: the contact footer starts 766pt from the top of the letterhead page
# Landscape: the header artwork and the contact footer are cut out of the letterhead (as sharp clips of the original
# page) and placed on a landscape page - brand box at the left margin, company logo and contact details at the right
# margin - instead of turning the whole letterhead on its side.
# Each clip is (x0, y0, x1, y1) on the letterhead page; the artwork ink inside it is given for alignment.
LH_HEAD_LEFT_CLIP = (30, 26, 215, 90)      # brand box:      ink x 38.5-205,  y 31.5-84
LH_HEAD_LEFT_INK_X0 = 38.5
LH_HEAD_RIGHT_CLIP = (325, 26, 575, 90)    # company logo:   ink x 335-567.5, y 31.5-84
LH_HEAD_RIGHT_INK_X1 = 567.5
LH_HEAD_INK_Y = (31.5, 84)
LH_FOOT_CLIP = (250, 760, 580, 812)        # contact footer: ink x 258-572.5, y 766-806.5
LH_FOOT_INK_X1 = 572.5
LH_FOOT_INK_Y = (766, 806.5)


class Layout:
    """Page geometry for one orientation (kept in an object so concurrent requests can't affect each other)."""

    def __init__(self, orientation='portrait'):
        self.landscape = orientation == 'landscape'
        self.page_w, self.page_h = landscape(A4) if self.landscape else A4
        self.content_w = self.page_w - 2 * MARGIN_X
        if self.landscape:
            self.head_y = 14                                          # top of the header clips on the page
            self.foot_ink_from_bottom = 26                            # printers need room below the footer text
            self.top = self.head_y + (LH_HEAD_INK_Y[1] - LH_HEAD_LEFT_CLIP[1]) + 10
            self.bottom = self.foot_ink_from_bottom + (LH_FOOT_INK_Y[1] - LH_FOOT_INK_Y[0]) + 8
        else:
            self.lh_scale = self.page_w / LH_W
            self.lh_dy = (self.page_h - LH_H * self.lh_scale) / 2     # centres the scaled letterhead vertically
            self.top = LH_HEADER_BOTTOM * self.lh_scale + self.lh_dy + 10
            self.bottom = self.page_h - (LH_FOOTER_TOP * self.lh_scale + self.lh_dy) + 7
        self.col_w = [self.content_w * w / sum(_WEIGHTS) for w in _WEIGHTS]
        self.frame_h = self.page_h - self.top - self.bottom
        self.max_row_h = int(self.frame_h * 0.7)   # tallest a description piece may be (page 1 also holds the To block)


CURRENCY = 'AED'
VAT_PERCENT = int(VAT_RATE * 100)
BANK_LINES = [
    'NATIONAL BANK OF FUJAIRAH, P.O BOX: 2979, DUBAI - U.A.E.',
    'SWIFT: NBFUAEAFDXB',
    'TEL: 009714 3971700 / FAX: 009714 3973922',
    'A/C NO: 012001111025',
    'A/C NAME: JUNAID GALLERIA TRADING LLC',
    'IBAN: AE88 0380 0000 1200 1111 025',
]

_base = ParagraphStyle('qbase', fontName='Helvetica', fontSize=7.5, leading=9.5)
S_BASE = _base
S_BOLD = ParagraphStyle('qbold', parent=_base, fontName='Helvetica-Bold')
S_TITLE = ParagraphStyle('qtitle', parent=_base, fontName='Helvetica-Bold',
                         fontSize=13, leading=16, alignment=TA_CENTER)
S_HEAD_C = ParagraphStyle('qheadc', parent=S_BOLD, fontSize=6.5, leading=8, alignment=TA_CENTER,
                          textColor=colors.white)
S_CELL = ParagraphStyle('qcell', parent=_base, fontSize=7, leading=8.6)
S_CELL_C = ParagraphStyle('qcellc', parent=S_CELL, alignment=TA_CENTER)
S_RIGHT = ParagraphStyle('qright', parent=S_CELL, alignment=TA_RIGHT)
S_RIGHT_B = ParagraphStyle('qrightb', parent=S_RIGHT, fontName='Helvetica-Bold', fontSize=8)
S_LINK = ParagraphStyle('qlink', parent=_base, fontSize=7, leading=9, alignment=TA_CENTER)
S_ADDR = ParagraphStyle('qaddr', parent=_base, fontSize=8.5, leading=11.5)
S_ADDR_R = ParagraphStyle('qaddrr', parent=S_ADDR, alignment=TA_RIGHT)
S_POINT = ParagraphStyle('qpoint', parent=_base, leftIndent=16, firstLineIndent=-16)

HEADER_BG = colors.HexColor('#1f3a5f')
TOTAL_BG = colors.HexColor('#eef1f6')

# Relative widths of: Item no, Model no, Photo, Brand, Description, Size, Color, Qty, Unit price, Total, Link, Warranty
_WEIGHTS = [24, 42, 50, 40, 98, 56, 40, 24, 42, 50, 52, 52]


def _p(text, style=S_BASE):
    return Paragraph(escape(str(text or '')).replace('\n', '<br/>'), style)


def _money(value):
    return f'{value:,.2f}'


def _qty(value):
    return f'{value:,.0f}' if value == value.to_integral_value() else f'{value:,.2f}'


def _photo(field_file, max_w, max_h):
    """Read an uploaded photo into a ReportLab Image scaled to fit the cell."""
    if not field_file:
        return ''
    try:
        with field_file.open('rb') as fh:
            pil = PILImage.open(BytesIO(fh.read()))
        pil = ImageOps.exif_transpose(pil)
        if pil.mode in ('RGBA', 'LA', 'P'):
            pil = pil.convert('RGBA')
            bg = PILImage.new('RGB', pil.size, (255, 255, 255))
            bg.paste(pil, mask=pil.getchannel('A'))
            pil = bg
        else:
            pil = pil.convert('RGB')
        pil.thumbnail((500, 500))   # plenty for print, keeps the PDF small
        buf = BytesIO()
        pil.save(buf, 'JPEG', quality=82)
        buf.seek(0)
        w, h = pil.size
        scale = min(max_w / w, max_h / h)
        return Image(buf, width=w * scale, height=h * scale)
    except Exception:
        return ''   # a missing/corrupt file must never break the PDF


def _link_cell(item, base_url):
    """Clickable link that downloads the item's datasheet (blank when it has none)."""
    if not (item.datasheet and item.share_token):
        return ''
    url = f'{base_url}/quote-builder/ds/{item.share_token}/'
    return Paragraph(f'<link href={quoteattr(url)} color="#1d4ed8"><u>Download<br/>Datasheet</u></link>', S_LINK)


def _header_block(q, lay):
    left = [Paragraph('<b>TO:</b>', S_ADDR)]
    left += [Paragraph(escape(ln) or '&nbsp;', S_ADDR) for ln in (q.to_text or '').splitlines()]
    right = [
        Paragraph(f'<b>QTN NO:</b> {escape(q.qtn_no)}', S_ADDR_R),
        Paragraph(f'<b>DATE:</b> {q.date.strftime("%d.%m.%Y")}', S_ADDR_R),
    ]
    if (q.project_reference or '').strip():
        right.append(Paragraph(f'<b>PROJECT REF:</b> {escape(q.project_reference.strip())}', S_ADDR_R))
    t = Table([[left, right]], colWidths=[lay.content_w * 0.66, lay.content_w * 0.34])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    return t


def _split_description(text, lay):
    """The description as one Paragraph, or several when it is taller than lay.max_row_h.

    Pieces break at line ends, so no line of text is ever cut in half.
    """
    width = lay.col_w[4] - 6
    rest = _p(text, S_CELL)
    pieces = []
    while True:
        _, h = rest.wrap(width, 100000)
        if h <= lay.max_row_h:
            pieces.append(rest)
            return pieces
        parts = rest.split(width, lay.max_row_h)
        if len(parts) < 2:   # cannot be split any further
            pieces.append(rest)
            return pieces
        pieces.append(parts[0])
        rest = parts[1]


def _items_table(q, base_url, lay, split_rows=False):
    heads = ['ITEM<br/>NO.', 'MODEL<br/>NO.', 'PHOTO', 'BRAND', 'DESCRIPTION', 'SIZE', 'COLOR', 'QTY',
             f'UNIT PRICE<br/>{CURRENCY}', f'TOTAL<br/>AMOUNT {CURRENCY}', 'LINK', 'WARRANTY']
    rows = [[Paragraph(h, S_HEAD_C) for h in heads]]
    # Vertical lines run through every row; horizontal lines are drawn only under the header and under the LAST
    # row of each item, so the continuation rows of a very long description look like one tall cell.
    style = [
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('LINEBEFORE', (1, 0), (-1, -1), 0.6, colors.black),
        ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('BACKGROUND', (0, 0), (-1, 0), HEADER_BG),
    ]
    items = list(q.items.all())
    for n, it in enumerate(items, 1):
        # A description taller than lay.max_row_h is cut (at line ends) into pieces; the first piece goes in the item's
        # row and the rest in continuation rows directly below, so no single row can ever outgrow a page.
        pieces = _split_description(it.description, lay)
        rows.append([
            Paragraph(escape(it.item_no) if it.item_no else str(n), S_CELL_C),
            _p(it.model_no, S_CELL),
            _photo(it.photo, lay.col_w[2] - 6, 52),
            _p(it.brand, S_CELL), pieces[0], _p(it.size, S_CELL), _p(it.color, S_CELL),
            Paragraph(_qty(it.qty), S_CELL_C),
            Paragraph(_money(it.unit_price), S_RIGHT),
            Paragraph(_money(it.amount), S_RIGHT),
            _link_cell(it, base_url),
            _p(it.warranty, S_CELL),
        ])
        for piece in pieces[1:]:
            rows.append(['', '', '', '', piece] + [''] * 7)
        style.append(('LINEBELOW', (0, len(rows) - 1), (-1, len(rows) - 1), 0.6, colors.black))
    # Totals: item total, VAT 5% on it, then the grand total (VAT included). NOSPLIT keeps the last item row and the
    # three totals rows on one page, so the totals are never left alone at the top of a new page.
    style.append(('NOSPLIT', (0, len(rows) - 1), (-1, len(rows) + 2)))
    for label, value, bold in ((f'TOTAL ({CURRENCY})', q.total, False),
                               (f'VAT {VAT_PERCENT}% ({CURRENCY})', q.vat_amount, False),
                               (f'GRAND TOTAL ({CURRENCY})', q.grand_total, True)):
        r = len(rows)
        s_label, s_value = (S_RIGHT_B, S_RIGHT_B) if bold else (S_RIGHT, S_RIGHT)
        rows.append([Paragraph(label, s_label)] + [''] * 8 + [Paragraph(_money(value), s_value)] + ['', ''])
        style += [
            ('SPAN', (0, r), (8, r)),
            ('SPAN', (10, r), (11, r)),
            ('BACKGROUND', (0, r), (-1, r), TOTAL_BG),
        ]
        if not bold:
            style.append(('LINEBELOW', (0, r), (-1, r), 0.4, colors.grey))
    # A row never splits across pages: if it doesn't fit it moves whole to the next page. (split_rows is only the
    # last-resort fallback used when some other cell, not the description, is taller than a page.)
    t = Table(rows, colWidths=lay.col_w, repeatRows=1, splitInRow=1 if split_rows else 0)
    t.setStyle(TableStyle(style))
    return t


def _boxed(title, body_rows, lay, label_w=None):
    """A bordered block: optional bold title row, then label/value or plain text rows."""
    rows = list(body_rows)
    first = 0
    if title:
        rows.insert(0, [Paragraph(f'<b>{title}</b>', S_BASE)] + [''] * (1 if label_w else 0))
        first = 1
    widths = [label_w, lay.content_w - label_w] if label_w else [lay.content_w]
    t = Table(rows, colWidths=widths, splitInRow=1)   # text-only rows: a very long point may continue on the next page
    style = [
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]
    if title:
        style += [('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.black), ('BACKGROUND', (0, 0), (-1, 0), TOTAL_BG)]
    if label_w:
        if title:
            style.append(('SPAN', (0, 0), (1, 0)))
        style.append(('INNERGRID', (0, first), (-1, -1), 0.4, colors.grey))
    t.setStyle(TableStyle(style))
    return t


def _numbered_points(text):
    """One row per non-empty line, numbered 1., 2., 3. ...

    Any number the user already typed ("1.", "2)", "3 -") is dropped first so a point is never numbered twice.
    """
    rows = []
    for line in (text or '').splitlines():
        line = re.sub(r'^\s*\d+\s*[.)\-:]\s*', '', line).strip()
        if line:
            rows.append([Paragraph(f'{len(rows) + 1}.&nbsp;&nbsp;{escape(line)}', S_POINT)])
    return rows


def _info_rows(pairs):
    return [[Paragraph(f'<b>{k}</b>', S_BASE), _p(v)] for k, v in pairs]


def _body(q, base_url, lay, split_rows=False):
    story = [
        Paragraph('<u>QUOTATION</u>', S_TITLE),
        Spacer(1, 8),
        _header_block(q, lay),
        Spacer(1, 8),
        _items_table(q, base_url, lay, split_rows),
        Spacer(1, 10),
    ]
    points = _numbered_points(q.remarks)   # the editable "remarks" text: one numbered point per line
    if points:
        story += [_boxed('TERMS AND CONDITIONS', points, lay), Spacer(1, 8)]
    tail = [
        _boxed(None, _info_rows([
            ('VALIDITY', q.validity), ('STOCK AVAILABILITY', q.stock_availability.strftime('%d.%m.%Y') if q.stock_availability else ''),
            ('COUNTRY OF ORIGIN', q.country_of_origin),
        ]), lay, label_w=130),
        Spacer(1, 8),
        _boxed('BANK ACCOUNT DETAILS', [[_p(ln)] for ln in BANK_LINES], lay),
        Spacer(1, 8),
        _boxed('CONTACT', _info_rows([
            ('SALES PERSON', q.sales_person), ('EMAIL', q.email), ('CONTACT NUMBER', q.contact_number),
        ]), lay, label_w=130),
    ]
    story.append(KeepTogether(tail))
    return story


def _letterhead_page(lay):
    """A fresh letterhead page (PyPDF2 page) for the layout's orientation."""
    if not lay.landscape:
        letterhead = PdfReader(LETTERHEAD_PATH).pages[0]
        letterhead.add_transformation(Transformation().scale(lay.lh_scale, lay.lh_scale).translate(0, lay.lh_dy))
        base = PageObject.create_blank_page(width=lay.page_w, height=lay.page_h)
        base.merge_page(letterhead)
        return base

    import fitz  # PyMuPDF: places clipped regions of the letterhead page, keeping them sharp (original images)

    src = fitz.open(LETTERHEAD_PATH)
    doc = fitz.open()
    page = doc.new_page(width=lay.page_w, height=lay.page_h)
    left, right = MARGIN_X, lay.page_w - MARGIN_X

    def place(clip, x_left, y_top):
        x0, y0, x1, y1 = clip
        page.show_pdf_page(fitz.Rect(x_left, y_top, x_left + (x1 - x0), y_top + (y1 - y0)), src, 0,
                           clip=fitz.Rect(x0, y0, x1, y1))

    cx0 = LH_HEAD_LEFT_CLIP[0]
    place(LH_HEAD_LEFT_CLIP, left - (LH_HEAD_LEFT_INK_X0 - cx0), lay.head_y)           # brand box on the left margin
    cx0, cx1 = LH_HEAD_RIGHT_CLIP[0], LH_HEAD_RIGHT_CLIP[2]
    place(LH_HEAD_RIGHT_CLIP, right + (cx1 - LH_HEAD_RIGHT_INK_X1) - (cx1 - cx0), lay.head_y)   # logo on the right margin
    fx0, fy0, fx1, fy1 = LH_FOOT_CLIP
    ink_bottom_on_page = lay.page_h - lay.foot_ink_from_bottom
    place(LH_FOOT_CLIP, right + (fx1 - LH_FOOT_INK_X1) - (fx1 - fx0),
          ink_bottom_on_page - (LH_FOOT_INK_Y[1] - fy0))                              # contact details bottom right
    return PdfReader(BytesIO(doc.tobytes())).pages[0]


def build_quotation_pdf(quotation, base_url, orientation='portrait'):
    """Return the finished PDF (letterhead + content) as bytes.

    base_url: scheme + host (no trailing slash) used for the datasheet download links.
    orientation: 'portrait' or 'landscape' (A4 either way).
    """
    lay = Layout('landscape' if orientation == 'landscape' else 'portrait')
    for split_rows in (False, True):
        body_buf = BytesIO()
        doc = BaseDocTemplate(
            body_buf, pagesize=(lay.page_w, lay.page_h), leftMargin=MARGIN_X, rightMargin=MARGIN_X,
            topMargin=lay.top, bottomMargin=lay.bottom,
            title=f'Quotation {quotation.qtn_no}',
        )
        frame = Frame(MARGIN_X, lay.bottom, lay.content_w, lay.frame_h,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])
        try:
            doc.build(_body(quotation, base_url.rstrip('/'), lay, split_rows))
            break
        except LayoutError:
            # A single row (an extremely long description) is taller than a page: retry letting rows split.
            if split_rows:
                raise
    body_buf.seek(0)

    body_reader = PdfReader(body_buf)
    writer = PdfWriter()
    for body_page in body_reader.pages:
        base = _letterhead_page(lay)       # fresh letterhead page per output page, with the content on top
        base.merge_page(body_page)
        writer.add_page(base)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()

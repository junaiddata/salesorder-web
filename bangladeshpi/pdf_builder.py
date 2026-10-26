"""
Bangladesh PI PDF builder.

The body (reference block, shipment details, items table, totals, notes,
terms, signature blocks, bank details) is laid out with ReportLab inside a
frame that leaves room for the letterhead, then each page is stamped on top
of media/BDLetterhead.pdf with pypdf. Nothing here is shared with any other
app's PDF code.
"""
import os
import re
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from PIL import Image as PILImage
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer,
    Table, TableStyle,
)

PAGE_W, PAGE_H = A4
MARGIN_X = 36
TOP_RESERVED = 72       # letterhead logo band (BD_letter.pdf: logo ends ~46pt from the top)
BOTTOM_RESERVED = 69    # letterhead contact footer (BD_letter.pdf: starts ~49pt from the bottom edge)
CONTENT_W = PAGE_W - 2 * MARGIN_X

from .models import BangladeshPISeal  # noqa: E402

LETTERHEAD_PATH = os.path.join(settings.MEDIA_ROOT, 'BD_letter.pdf')   # old BDLetterhead.pdf kept as a backup

EXPORTER_LINES = [
    'JUNAID SANITARY AND ELECTRICAL REQUISITES TRADING LLC',
    'PO BOX NO :34862',
    'DIP - 2, DUBAI - U.A.E',
    'PH: 00971-04-2367723, FAX: 00971-04-2367250',
]
IMPORTER_LINES = [
    'IMAA CORPORATION',
    '316, Chowdhury Machinery Market, Jubilee Road,',
    'Enayet Bazar, Chattogram, Bangladesh',
    'PH: 0088-02-41360491, FAX: 0088-031-610095',
]
CURRENCY = 'USD'
INCOTERM = 'CFR Cost and Freight CHATTOGRAM'
IMPORTANT_NOTE = (
    '<b>IMPORTANT NOTE :</b> ONCE THE ORDER IS CONFIRMED, NO REVISION IS ALLOWED. '
    'IF REVISION IS INEVITABLE, THEN THE NEW DELIVERY DATE WILL BE 45 DAYS UPON '
    'FACTORY CONFIRMATION.'
)
BANK_LINES = [
    'NATIONAL BANK OF FUJAIRAH, P.O BOX: 2979, DUBAI - U.A.E.',
    'SWIFT: NBFUAEAFDXB',
    'TEL: 009714 3971700 / FAX: 009714 3973922',
    'A/C NO: 012000027446',
    'A/C NAME:JUNAID SANITARY AND ELECTRICAL REQUISITES TRADING LLC',
    'IBAN: AE66 0380 0000 1200 0027 446',
]

_base = ParagraphStyle('base', fontName='Helvetica', fontSize=8, leading=10)
S_BASE = _base
S_BOLD = ParagraphStyle('bold', parent=_base, fontName='Helvetica-Bold')
S_TITLE = ParagraphStyle('title', parent=_base, fontName='Helvetica-Bold',
                         fontSize=10, leading=13, alignment=TA_CENTER)
S_HEAD_C = ParagraphStyle('headc', parent=S_BOLD, alignment=TA_CENTER)
S_RIGHT = ParagraphStyle('right', parent=_base, alignment=TA_RIGHT)
S_RIGHT_B = ParagraphStyle('rightb', parent=S_RIGHT, fontName='Helvetica-Bold')
S_CENTER = ParagraphStyle('center', parent=_base, alignment=TA_CENTER)
S_NOTE = ParagraphStyle('note', parent=_base, fontSize=7.5, leading=14)
S_TERM = ParagraphStyle('term', parent=_base, leftIndent=16, firstLineIndent=-16, leading=11)
S_ADDR = ParagraphStyle('addr', parent=_base, leading=12.5)
S_ADDR_B = ParagraphStyle('addrb', parent=S_ADDR, fontName='Helvetica-Bold')


def _p(text, style=S_BASE):
    return Paragraph(escape(str(text or '')).replace('\n', '<br/>'), style)


def _money(value):
    return f'{value:,.2f}'


def _qty(value):
    return f'{value:,.0f}' if value == value.to_integral_value() else f'{value:,.2f}'


def _lines(lines, bold_first=False):
    out = []
    for i, ln in enumerate(lines):
        out.append(_p(ln, S_ADDR_B if (bold_first and i == 0) else S_ADDR))
    return out


def _header_block(pi):
    left = [
        Paragraph(f'<b>NO.</b>&nbsp;&nbsp;&nbsp;&nbsp;{escape(pi.ref_no)}', S_ADDR),
        Paragraph(f'<b>DATE</b>&nbsp;&nbsp;&nbsp;&nbsp;{pi.date.strftime("%d.%m.%Y")}', S_ADDR),
        Spacer(1, 6),
    ] + _lines(EXPORTER_LINES)
    right = [Paragraph('<b>IMPORTER/CLIENT :</b>', S_ADDR), Spacer(1, 6)] + _lines(IMPORTER_LINES)
    t = Table([[left, right]], colWidths=[CONTENT_W * 0.5, CONTENT_W * 0.5])
    t.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('LINEAFTER', (0, 0), (0, 0), 0.8, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    return t


def _details_table(pi, variant='customer', tight=False):
    def lab(t):
        return Paragraph(t, S_BOLD)

    w = [CONTENT_W * 0.17, CONTENT_W * 0.33, CONTENT_W * 0.17, CONTENT_W * 0.33]
    insurance, payment = pi.insurance, pi.payment
    if variant == 'bank':
        insurance = pi.bank_insurance or pi.insurance
        payment = pi.bank_payment or pi.payment
    rows = [
        [lab('PORT OF ORIGIN'), _p(pi.port_of_origin), lab('COUNTRY OF ORIGIN'), _p(pi.country_of_origin)],
        [lab('TRANSPORT'), _p(pi.transport), lab('INSURANCE'), _p(insurance)],
        [lab('DESTINATION'), _p(pi.destination), lab('FINAL DESTINATION'), _p(pi.final_destination)],
        [lab('SHIPMENT'), _p(pi.shipment), lab('VALIDITY'), _p(pi.validity)],
        [lab('CURRENCY'), _p(CURRENCY), lab('INCOTERMS'), _p(INCOTERM)],
        [lab('PAYMENT'), _p(payment), '', ''],
    ]
    t = Table(rows, colWidths=w)
    t.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.6, colors.black),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('SPAN', (1, 5), (3, 5)),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 1.5 if tight else 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5 if tight else 3),
    ]))
    return t


def _items_table(pi, variant='customer', tight=False):
    bank = variant == 'bank'
    w = [CONTENT_W * 0.14, CONTENT_W * 0.38, CONTENT_W * 0.14, CONTENT_W * 0.17, CONTENT_W * 0.17]
    rows = [
        [Paragraph('CODES', S_HEAD_C), Paragraph('DESCRIPTION OF GOODS', S_HEAD_C),
         Paragraph('QUANTITY<br/>NOS.', S_HEAD_C), Paragraph('UNIT PRICE<br/>USD', S_HEAD_C),
         Paragraph('TOTAL AMT.<br/>USD', S_HEAD_C)],
    ]
    style = [
        ('GRID', (0, 0), (-1, -1), 0.6, colors.black),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 1.5 if tight else 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5 if tight else 3),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f2f2f2')),
    ]
    for it in pi.items.all():
        if bank and it.is_foc:
            continue            # FOC items are not shown on the bank copy
        r = len(rows)
        if it.kind == 'heading':
            text = it.description or it.code
            rows.append([Paragraph(escape(text), S_BOLD), '', '', '', ''])
            style.append(('SPAN', (0, r), (-1, r)))
            continue
        if it.is_foc:
            unit_cell = amt_cell = Paragraph('FOC', S_RIGHT_B)
        else:
            unit_cell = Paragraph(_money(it.bank_unit_price if bank else it.unit_price), S_RIGHT)
            amt_cell = Paragraph(_money(it.bank_amount if bank else it.amount), S_RIGHT)
        rows.append([
            _p(it.code), _p(it.description),
            Paragraph(_qty(it.qty), S_CENTER),
            unit_cell, amt_cell,
        ])

    country = (pi.country_of_origin or '').upper()
    if bank:
        if pi.bank_freight_charges:
            # Bank freight entered: same three lines as the customer copy.
            totals = [
                (f'TOTAL EX-WORKS, {country}' if country else 'TOTAL EX-WORKS', pi.bank_items_total),
                ('INLAND AND SEA FREIGHT CHARGES', pi.bank_freight_charges),
                ('TOTAL CFR CHATTOGRAM , BANGLADESH.', pi.bank_grand_total),
            ]
        else:
            # No bank freight: a single total, as before.
            totals = [('TOTAL CFR CHATTOGRAM , BANGLADESH.', pi.bank_items_total)]
    else:
        totals = [
            (f'TOTAL EX-WORKS, {country}' if country else 'TOTAL EX-WORKS', pi.items_total),
            ('INLAND AND SEA FREIGHT CHARGES', pi.freight_charges),
            ('TOTAL CFR CHATTOGRAM , BANGLADESH.', pi.grand_total),
        ]
    for label, val in totals:
        r = len(rows)
        rows.append([Paragraph(label, S_RIGHT_B), '', '', '', Paragraph(_money(val), S_RIGHT_B)])
        style.append(('SPAN', (0, r), (3, r)))
    style.append(('BACKGROUND', (0, len(rows) - 1), (-1, len(rows) - 1), colors.HexColor('#f2f2f2')))

    t = Table(rows, colWidths=w, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def _terms_flowables(pi):
    text = (pi.bank_terms_text or '').strip()
    if not text:
        return []
    out = [Spacer(1, 10), Paragraph('<u><b>TERMS &amp; CONDITIONS FOR ISSUING L/C:</b></u>', S_BASE), Spacer(1, 4)]
    n = 0
    for line in text.splitlines():
        # Drop any number the user already typed ("1.", "2)", "3 -") so we never double-number.
        line = re.sub(r'^\s*\d+\s*[.)\-:]\s*', '', line).strip()
        if line:
            n += 1
            out.append(Paragraph(f'{n}.&nbsp;&nbsp;{escape(line)}', S_TERM))
    return out


def _strip_white_background(img):
    """Remove the paper background of a photographed/scanned signature or stamp.

    The paper may be grey or unevenly lit, so the background level is estimated
    locally (a blurred "max brightness" map), every pixel is divided by it, and
    only pixels clearly darker than the paper keep any opacity. The result is
    the ink on a transparent background, with the grey cast removed.
    """
    import numpy as np
    from PIL import ImageFilter

    rgba = img.convert('RGBA')
    w, h = rgba.size
    rgb = np.asarray(rgba.convert('RGB'), dtype=np.float32)
    old_alpha = np.asarray(rgba.getchannel('A'), dtype=np.float32)

    gray = rgba.convert('L')
    sw, sh = max(1, w // 8), max(1, h // 8)
    bg = gray.resize((sw, sh), PILImage.BILINEAR).filter(ImageFilter.MaxFilter(9))
    bg = bg.filter(ImageFilter.GaussianBlur(2)).resize((w, h), PILImage.BICUBIC)
    bg = np.maximum(np.asarray(bg, dtype=np.float32), 60.0)      # avoid dividing by ~0

    norm = np.clip(rgb * 255.0 / bg[..., None], 0, 255)           # paper -> white
    lum = norm @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    # transparent at/above 232, opaque at/below 185, smooth in between
    alpha = np.clip((232.0 - lum) / (232.0 - 185.0), 0, 1) * 255.0
    alpha = np.minimum(alpha, old_alpha)

    # Photos often have a dark shadow line along the edges of the paper: ignore the outer border.
    by, bx = max(1, int(h * 0.03)), max(1, int(w * 0.03))
    if h > 40 and w > 40:
        alpha[:by, :] = 0
        alpha[-by:, :] = 0
        alpha[:, :bx] = 0
        alpha[:, -bx:] = 0

    # Slightly stronger, darker ink so thin pen strokes still print clearly when shrunk.
    alpha = 255.0 * (alpha / 255.0) ** 0.6
    norm = norm * 0.85

    out = PILImage.fromarray(np.dstack([norm, alpha]).astype(np.uint8), 'RGBA')
    # Trim the empty margins so the ink (not the photo of the paper) fills the box.
    box = PILImage.fromarray((alpha > 25).astype(np.uint8) * 255).getbbox()
    if box:
        pad = 6
        out = out.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                        min(w, box[2] + pad), min(h, box[3] + pad)))
    return out


def _seal_image(field_file, max_w, max_h, remove_bg=False):
    """Read an uploaded seal image into a ReportLab Image scaled to fit the box."""
    if not field_file:
        return None
    try:
        with field_file.open('rb') as fh:
            data = fh.read()
        pil = PILImage.open(BytesIO(data))
        if remove_bg:
            pil.thumbnail((900, 900))   # plenty for print, keeps the pixel pass fast
            pil = _strip_white_background(pil)
            buf = BytesIO()
            pil.save(buf, 'PNG')
            data = buf.getvalue()
        w, h = pil.size
        scale = min(max_w / w, max_h / h)
        return Image(BytesIO(data), width=w * scale, height=h * scale, mask='auto')
    except Exception:
        return None  # a missing/corrupt file must never break the PDF


class _Mark(Flowable):
    """Zero-size marker that records which page it lands on (layout probing)."""

    def __init__(self, marks, name):
        super().__init__()
        self.marks, self.name = marks, name
        self.width = self.height = 0

    def wrap(self, availWidth, availHeight):
        return 0, 0

    def draw(self):
        self.marks[self.name] = self.canv.getPageNumber()


def _seal_row_height(pi, compact):
    """Height of the signature/stamp strip under the 'Confirmed ... EXPORTER' line."""
    if pi.add_stamp:
        return 52 if compact else 84
    return 44 if compact else 58


def _exporter_cell(pi, compact=False):
    """Heading, then the signature with the stamp right after it (same strip)."""
    cell = [Paragraph('<b>CONFIRMED AND ACCEPTED BY EXPORTER:</b>', S_BASE)]
    seal = BangladeshPISeal.get_instance()
    row_h = _seal_row_height(pi, compact)
    sig = _seal_image(seal.signature, 150, row_h - 6, remove_bg=True) if pi.add_signature else None
    stamp = _seal_image(seal.stamp, 125, row_h - 4, remove_bg=True) if pi.add_stamp else None
    pics = [p for p in (sig, stamp) if p is not None]
    if pics:
        t = Table([pics], colWidths=[p.drawWidth + 12 for p in pics], rowHeights=[row_h])
        t.hAlign = 'LEFT'
        t.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        cell.append(t)
    return cell


def _signature_and_bank(pi, compact=False, marks=None):
    sig = Table(
        [[_exporter_cell(pi, compact),
          Paragraph('<b>CONFIRMED AND ACCEPTED BY BUYER:</b>', S_BASE)]],
        colWidths=[CONTENT_W * 0.5, CONTENT_W * 0.5], rowHeights=[16 + _seal_row_height(pi, compact) if (pi.add_signature or pi.add_stamp) else (52 if compact else 68)],
    )
    sig.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))

    bank_rows = [[Paragraph('<b>BANK ACCOUNT DETAILS [PAYMENT IN USD]</b>', S_BASE)]]
    bank_rows += [[_p(ln)] for ln in BANK_LINES]
    bank = Table(bank_rows, colWidths=[CONTENT_W])
    bank.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 1 if compact else 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1 if compact else 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    parts = [sig, Spacer(1, 6 if compact else 12), bank]
    if marks is not None:
        parts.insert(0, _Mark(marks, 'block'))
    return KeepTogether(parts)


def _body(pi, variant='customer', mode='normal', marks=None):
    """mode: 'normal' | 'compact' (tighter signature/bank block) |
    'together' (notes, terms, signature and bank kept together on one page)."""
    compact = mode == 'compact'
    story = [
        Paragraph('<u>PROFORMA INVOICE</u>', S_TITLE),
        Spacer(1, 6),
        _header_block(pi),
        Spacer(1, 4),
        _details_table(pi, variant, compact),
        Spacer(1, 4),
        _items_table(pi, variant, compact),
        Spacer(1, 10),
    ]
    tail = [Paragraph(IMPORTANT_NOTE, S_NOTE)]
    if marks is not None:
        tail.append(_Mark(marks, 'note'))
    if variant == 'customer' and pi.freight_valid_till:
        tail.append(Spacer(1, 6))
        tail.append(Paragraph(f'<b>Freight Rate Valid Till {escape(pi.freight_valid_till)} only</b>', S_NOTE))
    if variant == 'bank':
        tail += _terms_flowables(pi)
    tail += [Spacer(1, 5 if compact else 10), _signature_and_bank(pi, compact, marks)]
    if mode == 'together':
        story.append(KeepTogether(tail))
    else:
        story += tail
    return story


def build_pi_pdf(pi, variant='customer', with_seals=True):
    """Return the finished PDF (letterhead + content) as bytes.

    variant: 'customer' (Ex-Works/freight/CFR totals) or 'bank' (bank unit
    prices, single total, L/C terms).
    """
    # Layout tiers: if the signature/bank block would land alone on a new page,
    # first retry with a tighter block, then keep the notes/terms with it so the
    # last page is never just the signature and bank box.
    if not with_seals:
        # Viewer isn't authorised to use the signature/stamp: print an unsigned copy.
        pi.add_signature = pi.add_stamp = False   # in-memory only, never saved
    for mode in ('normal', 'compact', 'together'):
        marks = {}
        body_buf = BytesIO()
        doc = BaseDocTemplate(
            body_buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
            topMargin=TOP_RESERVED, bottomMargin=BOTTOM_RESERVED,
            title=f'Proforma Invoice {pi.ref_no}',
        )
        frame = Frame(MARGIN_X, BOTTOM_RESERVED, CONTENT_W, PAGE_H - TOP_RESERVED - BOTTOM_RESERVED,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])
        doc.build(_body(pi, variant, mode, marks))
        orphaned = marks.get('block', 0) > marks.get('note', 0)
        if mode == 'together' or not orphaned:
            break
    body_buf.seek(0)

    body_reader = PdfReader(body_buf)
    writer = PdfWriter()
    for body_page in body_reader.pages:
        # Fresh letterhead page per output page, with the content on top.
        base = PdfReader(LETTERHEAD_PATH).pages[0]
        base.merge_page(body_page)
        writer.add_page(base)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()

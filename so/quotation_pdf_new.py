"""
New-design PDF for so.Quotation (quotations/<id>/details/ -> Export PDF, and the Send Quotation email attachment).

Same content as the old generate_alabama_quotation / generate_junaid_quotation in views_quotation.py, laid out in the
plain design of the quote-builder PDF (title, boxed To / reference / date block, ruled items table, totals with the
amount in words, terms). The body is drawn with ReportLab inside a frame that leaves room for the division's
letterhead, then each page is merged on top of that letterhead with PyPDF2.

Everything that differs per division (letterhead, margins around it, company line, stamp) lives in DIVISION_CONFIG.
Nothing here is shared with the quotation (quote-builder) app's PDF code, and the old generators are untouched.
"""
import logging
import os
from functools import lru_cache
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from PIL import Image as PILImage, ImageOps
from PyPDF2 import PageObject, PdfReader, PdfWriter, Transformation
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from quotation.words import amount_in_words

from .quotation_customer_details import customer_details

log = logging.getLogger(__name__)

MARGIN_X = 30
PAGE_W, PAGE_H = A4
CONTENT_W = PAGE_W - 2 * MARGIN_X
CURRENCY = 'AED'
VAT_RATE = 0.05

# The letterheads (media/quotation_letterheads/*.pdf) are A4 PDFs of 595.5 x 842.25pt, scaled to the A4 page and centred
# vertically. They have a header only (no footer); the header artwork ends 116pt (Alabama) / 123pt (Junaid) from the top.
_LH_W, _LH_H = 595.5, 842.25
_LH_SCALE = PAGE_W / _LH_W
_LH_DY = (PAGE_H - _LH_H * _LH_SCALE) / 2
LETTERHEAD_DIR = os.path.join(settings.MEDIA_ROOT, 'quotation_letterheads')

# Per division: letterhead file (a one-page PDF or an A4 image), how far down the page the content may start (`top`) and
# how far up from the bottom it must stop (`bottom`), and an optional stamp image (path without extension: .png/.jpg/
# .jpeg/.webp are all picked up; skipped when no file exists).
DIVISION_CONFIG = {
    'JUNAID': {
        'letterhead': os.path.join(LETTERHEAD_DIR, 'Junaid_world_qt.pdf'),
        # Same letterhead without the head-office address: used for a branch salesman's quotations, with the branch's
        # address lines drawn by the code (see BRANCHES). Every other salesman gets the letterhead above unchanged.
        'branch_letterhead': os.path.join(LETTERHEAD_DIR, 'Junaid_world_address.pdf'),
        'top': 137,
        'bottom': 40,
        'stamp': os.path.join(settings.MEDIA_ROOT, 'quotation_stamps', 'junaid'),
        'show_license': True,
        'phone': '04-2367723',      # the landline printed on the letterhead (used in the solar terms)
    },
    'ALABAMA': {
        'letterhead': os.path.join(LETTERHEAD_DIR, 'Alabama_qt.pdf'),
        'top': 130,
        'bottom': 40,
        'stamp': os.path.join(settings.MEDIA_ROOT, 'quotation_stamps', 'alabama'),
        'show_license': False,
        'phone': '04-3526973',      # the landline printed on the letterhead (used in the solar terms)
    },
}

# Branch address lines for the Junaid letterhead, keyed by Salesman.branch: (address, landline, mobile, email).
BRANCHES = {
    'AJMAN': ('Shop 7, Rashideya 2, Ajman', '06-7447415', '052-9796910', 'ajman@junaid.ae'),
    'DEIRA': ('Shop 1, Diera, Dubai', '04-2216046', '050-3663916', 'salesdeira@junaid.ae'),
    'RAK1': ('Ras Al Khor Industrial Second, Dubai', '04-3201694', '055-1236439', 'storemanager@junaid.ae'),
    'RAK2': ('Ras Al Khor Industrial Second, Dubai', '04-3201694', '054-9946951', 'qusaisshop@junaid.ae'),
    'ABUDHABI': ('Building No 2, Mussaffah 9, Abudhabi', '02-2461525', '050-3860314', 'adshop@junaidgroup.ae'),
}
# Where the three address lines go: right-aligned to the letterhead's right edge, baselines measured from the page top
# (matching the head-office address on Junaid_world_qt.pdf).
ADDRESS_RIGHT_X = 562 * _LH_SCALE
ADDRESS_BASELINES = (91.5, 106, 120.5)
ADDRESS_FONT = ('Helvetica', 10)

LICENSE_NAMES = {
    'JUNAID_SME': 'JUNAID SANITARY AND ELECTRICAL REQUISITES TRADING L.L.C - DUBAI BRANCH',
    'JUNAID_MAT': 'JUNAID SANITARY & ELECTRICAL MATERIALS TRADING (LLC.) (DUBAI BR)',
}

TERMS = [
    'This quotation is valid as Agreed.',
    'Prices are subject to change without prior notice after the validity period.',
    'Delivery timelines will be confirmed upon order confirmation.',
    'This is a system-generated document and does not require a signature.',
]

# Solar water heater quotations (every item Vera Solar / Ariston Solar) print this block instead of TERMS. `{brand}` is
# VERA for Vera Solar and ARISTON for Ariston Solar. Plain strings = numbered points; ('H', text) = a bold sub-heading;
# ('P', text) = an unnumbered paragraph.
SOLAR_TERMS = [
    ('P', 'Ignore the below if it is unrelated to the above items in this quotation.'),
    ('P', 'Conditions apply for the Warranty of all tanks and calorifiers models including the solar water systems as follows:'),
    'Consumables (magnesium anode bars) must be checked periodically (recommended twice per year) & replaced at least '
    'once a year (depending on water quality) to prolong the life of the tanks and heating elements and maintain their '
    'warranty. The magnesium anode will gradually deteriorate to protect the heating element and the tanks. The warranty '
    'on tanks and heating elements will be void in case of the non-existence of magnesium anode or in case of failure to '
    'replace it upon expiry.',
    'Accessories on tanks and solar collectors must be maintained and cleaned periodically (1-2 times per year) to '
    'prolong their lives. All accessories are considered consumables and not covered by warranty. This includes; safety '
    'valves, air vents, temperature, and pressure gauges.',
    'All spare parts and consumables should be genuine, and compatible with {brand} products to keep on the warranty.',
    "It is the buyer's responsibility to manage the service and maintenance, contact our customer service & maintenance "
    'department for advice and support at {phone}.',
    'These prices, and terms of payments are applied in standard shipping conditions only, and might be changed without '
    'prior notice.',
    'Tank-in-Tank Thermosiphon System: A solar water heating system featuring two concentric tanks, where the inner tank '
    'stores domestic hot water and the outer tank contains the solar-heated water. Heat is transferred through the tank '
    'walls by natural convection (thermosiphon effect), without requiring a circulation pump. This provides efficient '
    'solar heating and reliable hot water supply.',
    ('H', 'EXCLUSIONS (If Applicable):'),
    ('P', '* ALL DOMESTIC WATER CONNECTIONS (VALVES, ACCESSORIES, ADAPTORS...) ARE NOT INCLUDED.'),
    ('P', '* ALL CIVIL WORKS RELATED TO SOLAR ARE NOT INCLUDED.'),
    ('P', '* INSULATION / CLADDING NOT INCLUDED.'),
    ('P', '* ALL ELECTRICAL WORK IS NOT INCLUDED.'),
]


def _solar_brand(q):
    """'VERA' / 'ARISTON' when EVERY item is of the exact brand VERA SOLAR / ARISTON SOLAR (spelled 'ARISTON-SOLAR' in
    the item master too). Any other brand on the quotation (VERA EDWIN, ARISTON - ITALY, EURO ...) or no items gives
    None and the standard TERMS are used. The first item's solar brand names the warranty point."""
    try:
        lines = list(q.items.select_related('item'))
    except Exception:
        return None
    brands = []
    for qi in lines:
        firm = ' '.join(str(getattr(qi.item, 'item_firm', '') or '').upper().replace('-', ' ').split())
        if firm == 'VERA SOLAR':
            brands.append('VERA')
        elif firm == 'ARISTON SOLAR':
            brands.append('ARISTON')
        else:
            return None
    return brands[0] if brands else None


_base = ParagraphStyle('nqbase', fontName='Helvetica', fontSize=7.5, leading=9.5)
S_BOLD = ParagraphStyle('nqbold', parent=_base, fontName='Helvetica-Bold')
S_TITLE = ParagraphStyle('nqtitle', parent=_base, fontName='Helvetica-Bold', fontSize=13, leading=16, alignment=TA_CENTER)
S_HEAD_C = ParagraphStyle('nqheadc', parent=S_BOLD, fontSize=6.5, leading=8, alignment=TA_CENTER)
S_HEAD_L = ParagraphStyle('nqheadl', parent=S_HEAD_C, alignment=TA_LEFT)
S_HEAD_R = ParagraphStyle('nqheadr', parent=S_HEAD_C, alignment=TA_RIGHT)
S_SECTION = ParagraphStyle('nqsection', parent=_base, fontName='Helvetica-Bold', fontSize=8.5, leading=11)
S_CELL = ParagraphStyle('nqcell', parent=_base, fontSize=7.5, leading=9.2)
S_CELL_C = ParagraphStyle('nqcellc', parent=S_CELL, alignment=TA_CENTER, wordWrap='CJK')
S_RIGHT = ParagraphStyle('nqright', parent=S_CELL, alignment=TA_RIGHT)
S_RIGHT_B = ParagraphStyle('nqrightb', parent=S_RIGHT, fontName='Helvetica-Bold', fontSize=8)
S_ADDR = ParagraphStyle('nqaddr', parent=_base, fontSize=8.5, leading=11.5)
S_LBL = ParagraphStyle('nqlbl', parent=_base, fontName='Helvetica-Bold', fontSize=8, leading=10)
S_VAL = ParagraphStyle('nqval', parent=_base, fontSize=8.5, leading=10.5, wordWrap='CJK')
S_VAL_B = ParagraphStyle('nqvalb', parent=S_VAL, fontName='Helvetica-Bold')
S_MID_LBL = ParagraphStyle('nqmidlbl', parent=_base, fontName='Helvetica-Bold', fontSize=7.5, leading=9)
S_MID_VAL = ParagraphStyle('nqmidval', parent=_base, fontSize=9, leading=11)
S_WORDS = ParagraphStyle('nqwords', parent=_base, fontSize=8, leading=10.5)
S_POINT = ParagraphStyle('nqpoint', parent=_base, leftIndent=16, firstLineIndent=-16)

# Relative widths of: #, Item code, UPC code, Description, Qty, Unit price, Total
_WEIGHTS = [20, 62, 62, 200, 44, 64, 68]
COL_W = [CONTENT_W * w / sum(_WEIGHTS) for w in _WEIGHTS]


def _money(value):
    return f'{value:,.2f}'


def _txt(value):
    return escape(str(value or '').strip())


def _val(text, bold=False):
    return Paragraph(_txt(text), S_VAL_B if bold else S_VAL)


def _label_table(rows, width):
    t = Table([[Paragraph(label, S_LBL), value] for label, value in rows],
              colWidths=[width * 0.36, width * 0.64])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 1.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5),
    ]))
    return t


def _header_block(q, cfg):
    """Three boxes of equal height: To / quotation reference / date."""
    sep = 6
    avail = CONTENT_W - 2 * sep
    w1, w2, w3 = avail * 0.40, avail * 0.38, avail * 0.22
    gap = 14   # a box's left + right padding

    cust = q.customer
    display_name = q.customer_display_name or cust.customer_name
    d = customer_details(cust, q)
    address = '<br/>'.join(escape(ln.strip()) for ln in d['address'].splitlines() if ln.strip())
    to_rows = [
        ('Customer name', _val(display_name, True)),
        ('Contact person', _val(d['contact_person'])),
        ('Contact number', _val(d['contact_number'])),
        ('Address', Paragraph(address, S_VAL)),
        ('TRN number', _val(d['trn'])),
        ('Customer ID', _val(cust.customer_code)),
        ('Customer email', _val(d['email'])),
    ]
    to = [Paragraph('<b>TO:</b>', S_ADDR), Spacer(1, 2), _label_table(to_rows, w1 - gap)]

    sm = q.salesman
    ref_rows = [
        ('Quotation no.', _val(q.quotation_number, True)),
        ('Customer ref.', _val(q.customer_ref)),
        ('Salesperson', _val(sm.salesman_name if sm else '')),
        ('Email', _val(sm.email if sm else '')),
        ('Contact', _val(sm.phone if sm else '')),
        ('Project reference', _val(q.project_reference)),
    ]
    if cfg['show_license']:
        ref_rows.append(('Company', _val(LICENSE_NAMES.get(q.license_name or 'JUNAID_SME', ''))))
    ref = _label_table(ref_rows, w2 - gap)

    mid_rows = []
    for label, value in (('Date', q.quotation_date.strftime('%d.%m.%Y')), ('Validity', q.validity),
                         ('Delivery availability', q.delivery_availability), ('Payment terms', q.payment_terms)):
        mid_rows += [[Paragraph(label, S_MID_LBL)], [Paragraph(_txt(value) or '&nbsp;', S_MID_VAL)]]
    right = Table(mid_rows, colWidths=[w3 - gap])
    right.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 1), (0, 1), 5), ('BOTTOMPADDING', (0, 3), (0, 3), 5), ('BOTTOMPADDING', (0, 5), (0, 5), 5),
    ]))

    # columns 0, 2 and 4 are the boxes; 1 and 3 are the empty gaps between them
    outer = Table([[to, '', ref, '', right]], colWidths=[w1, sep, w2, sep, w3])
    outer.setStyle(TableStyle([
        ('BOX', (0, 0), (0, 0), 0.8, colors.black),
        ('BOX', (2, 0), (2, 0), 0.8, colors.black),
        ('BOX', (4, 0), (4, 0), 0.8, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (1, 0), (1, 0), 0), ('RIGHTPADDING', (1, 0), (1, 0), 0),
        ('LEFTPADDING', (3, 0), (3, 0), 0), ('RIGHTPADDING', (3, 0), (3, 0), 0),
    ]))
    return outer


def _items_table(q):
    """Ruled items table + totals. Returns (table, grand_total). Totals are computed from the lines, as before."""
    heads = ['#', 'ITEM CODE', 'UPC CODE', 'DESCRIPTION', 'QTY', f'UNIT PRICE<br/>{CURRENCY}', f'TOTAL<br/>{CURRENCY}']
    head_style = [S_HEAD_C, S_HEAD_C, S_HEAD_C, S_HEAD_L, S_HEAD_C, S_HEAD_R, S_HEAD_R]
    rows = [[Paragraph(h, hs) for h, hs in zip(heads, head_style)]]
    style = [
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (0, -1), 0),
        ('LINEABOVE', (0, 0), (-1, 0), 0.8, colors.black),
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, colors.black),
    ]
    subtotal = 0.0
    for n, qi in enumerate(q.items.select_related('item'), 1):
        line_total = qi.quantity * qi.price
        subtotal += line_total
        item = qi.item
        description = getattr(item, 'item_description', None) or 'No description available'
        rows.append([
            Paragraph(str(n), S_CELL_C),
            Paragraph(_txt(getattr(item, 'item_code', '')), S_CELL_C),
            Paragraph(_txt(getattr(item, 'item_upvc', '')), S_CELL_C),
            Paragraph(_txt(description), S_CELL),
            Paragraph(f'{qi.quantity} {_txt(qi.unit)}', S_CELL_C),
            Paragraph(_money(qi.price), S_RIGHT),
            Paragraph(_money(line_total), S_RIGHT),
        ])
    first_total_row = len(rows)
    if first_total_row > 1:
        style.append(('LINEBELOW', (0, first_total_row - 1), (-1, first_total_row - 1), 0.6, colors.black))

    vat = subtotal * VAT_RATE
    grand_total = subtotal + vat
    for label, value, bold in ((f'TOTAL AMOUNT EXCL. VAT ({CURRENCY})', subtotal, False),
                               (f'VAT {VAT_RATE:.0%} ({CURRENCY})', vat, False),
                               (f'GRAND TOTAL ({CURRENCY})', grand_total, True)):
        r = len(rows)
        s = S_RIGHT_B if bold else S_RIGHT
        rows.append([''] * 3 + [Paragraph(label, s)] + [''] * 2 + [Paragraph(_money(value), s)])
        style += [('SPAN', (3, r), (5, r)), ('TOPPADDING', (0, r), (-1, r), 2), ('BOTTOMPADDING', (0, r), (-1, r), 2)]
        if bold:
            style += [('LINEABOVE', (3, r), (6, r), 0.6, colors.black), ('TOPPADDING', (0, r), (-1, r), 4)]
    r = len(rows)
    words = amount_in_words(round(grand_total, 2))
    rows.append([Paragraph(f'<b>{CURRENCY} : {escape(words)}</b>', S_WORDS)] + [''] * 6)
    style += [('SPAN', (0, r), (-1, r)), ('TOPPADDING', (0, r), (-1, r), 8)]
    # keep the last item row and every totals row on one page
    style.append(('NOSPLIT', (0, first_total_row - 1), (-1, len(rows) - 1)))

    t = Table(rows, colWidths=COL_W, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def _section(title, body_rows):
    rows = [[Paragraph(title, S_SECTION)]] + list(body_rows)
    t = Table(rows, colWidths=[CONTENT_W], splitInRow=1)
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 1.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
    ]))
    return t


def _stamp_file(base):
    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
        if os.path.exists(base + ext):
            return base + ext
    return None


def _stamp(base):
    """The division's stamp as an Image fitted to 110 x 82pt, or None when there is none (never breaks the PDF).

    A stamp photo/scan on white paper (e.g. a JPEG) has its white made transparent so it does not blank out the text
    under it; a PNG that already has transparency is used as it is.
    """
    try:
        path = _stamp_file(base) if base else None
        if not path:
            return None
        with PILImage.open(path) as pil:
            pil = ImageOps.exif_transpose(pil).convert('RGBA')
        pil.thumbnail((900, 900))
        if pil.getchannel('A').getextrema()[0] == 255:      # no transparency of its own
            lum = pil.convert('L')
            lo, hi = 170, 235                                # darker than lo: solid ink, brighter than hi: paper
            pil.putalpha(lum.point(lambda v: 0 if v >= hi else 255 if v <= lo else int((hi - v) * 255 / (hi - lo))))
        box = pil.getchannel('A').getbbox()                  # trim the empty margin so the stamp fills its space
        if box:
            pil = pil.crop(box)
        buf = BytesIO()
        pil.save(buf, 'PNG')
        buf.seek(0)
        w, h = pil.size
        scale = min(110 / w, 82 / h)
        return Image(buf, width=w * scale, height=h * scale, mask='auto')
    except Exception:
        log.exception('Quotation stamp could not be loaded: %s', base)
        return None


def _body(q, cfg):
    story = [
        Paragraph('<u>QUOTATION</u>', S_TITLE),
        Spacer(1, 8),
        _header_block(q, cfg),
        Spacer(1, 10),
        _items_table(q),
        Spacer(1, 14),
    ]
    if q.remarks and q.remarks.strip():
        remark_rows = [[Paragraph(escape(ln.strip()) or '&nbsp;', S_CELL)] for ln in q.remarks.splitlines() if ln.strip()]
        story += [KeepTogether([_section('REMARKS', remark_rows)]), Spacer(1, 10)]
    brand = _solar_brand(q)
    details = ' '.join((q.payment_terms_details or '').split())   # one point, however many lines were typed
    if brand:
        points, n = [], 0
        for entry in SOLAR_TERMS:
            if isinstance(entry, tuple):
                kind, text = entry
                style = S_BOLD if kind == 'H' else _base
                points.append([Paragraph(escape(text), style)])
            else:
                n += 1
                points.append([Paragraph(f'{n}.&nbsp;&nbsp;{escape(entry.format(brand=brand, phone=cfg.get('phone') or DIVISION_CONFIG['JUNAID']['phone']))}', S_POINT)])
        if details:
            n += 1
            # the payment details stay a numbered point, placed after the numbered warranty points, before the exclusions
            idx = next((i for i, e in enumerate(SOLAR_TERMS) if isinstance(e, tuple) and e[0] == 'H'), len(points))
            points.insert(idx, [Paragraph(f'{n}.&nbsp;&nbsp;Payment terms: {escape(details)}', S_POINT)])
    else:
        points = [[Paragraph(f'{n}.&nbsp;&nbsp;{escape(text)}', S_POINT)] for n, text in enumerate(TERMS, 1)]
        if details:
            points.append([Paragraph(f'{len(points) + 1}.&nbsp;&nbsp;Payment terms: {escape(details)}', S_POINT)])
    stamp = _stamp(cfg.get('stamp'))
    stamp_row = None
    if stamp is not None:
        stamp_row = Table([[stamp]], colWidths=[CONTENT_W])
        stamp_row.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                                       ('LEFTPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 10)]))
    if brand:
        # The solar block is long, so it flows across pages. The exclusions (heading + lines) and the stamp are kept
        # together: if the stamp does not fit under them, they move to the next page together, so the stamp is never
        # alone on a page.
        split_at = next((i for i, e in enumerate(SOLAR_TERMS) if isinstance(e, tuple) and e[0] == 'H'), None)
        if split_at is not None and details:
            split_at += 1                      # the payment point was inserted before the exclusions heading
        if split_at is None:
            story.append(_section('TERMS AND CONDITIONS', points))
            if stamp_row is not None:
                story.append(stamp_row)
        else:
            story.append(_section('TERMS AND CONDITIONS', points[:split_at]))
            end = Table(points[split_at:], colWidths=[CONTENT_W])
            end.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('TOPPADDING', (0, 0), (-1, -1), 1.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (0, 0), 6),
            ]))
            story.append(KeepTogether([end] + ([stamp_row] if stamp_row is not None else [])))
    else:
        tail = [_section('TERMS AND CONDITIONS', points)]
        if stamp_row is not None:
            tail.append(stamp_row)
        story.append(KeepTogether(tail))
    return story


@lru_cache(maxsize=8)
def _letterhead_bytes(path, mtime):
    """The letterhead as a one-page A4 PDF (bytes). `mtime` only keys the cache so a replaced file is picked up."""
    out = BytesIO()
    if path.lower().endswith('.pdf'):
        lh = PdfReader(path).pages[0]
        lh.add_transformation(Transformation().scale(_LH_SCALE, _LH_SCALE).translate(0, _LH_DY))
        page = PageObject.create_blank_page(width=PAGE_W, height=PAGE_H)
        page.merge_page(lh)
        writer = PdfWriter()
        writer.add_page(page)
        writer.write(out)
    else:
        with PILImage.open(path) as pil:
            pil = pil.convert('RGB')
            pil.thumbnail((1654, 2339))   # ~200 dpi on A4: sharp in print, keeps the PDF small
            img = BytesIO()
            pil.save(img, 'JPEG', quality=90)
        img.seek(0)
        from reportlab.lib.utils import ImageReader
        c = rl_canvas.Canvas(out, pagesize=A4)
        c.drawImage(ImageReader(img), 0, 0, width=PAGE_W, height=PAGE_H)
        c.showPage()
        c.save()
    return out.getvalue()


def _letterhead_page(cfg):
    """A fresh letterhead page, or a blank A4 page if the letterhead file is missing/unreadable."""
    path = cfg['letterhead']
    try:
        return PdfReader(BytesIO(_letterhead_bytes(path, os.path.getmtime(path)))).pages[0]
    except Exception:
        log.exception('Quotation letterhead could not be loaded: %s', path)
        return PageObject.create_blank_page(width=PAGE_W, height=PAGE_H)


def _branch_setup(quotation, cfg):
    """(config to use for the letterhead, address lines to draw or None).

    A Junaid quotation whose salesman has a branch uses the address-free letterhead and gets that branch's lines.
    Anything else (no salesman/branch, Alabama, missing file) uses the normal letterhead and draws nothing.
    """
    path = cfg.get('branch_letterhead')
    branch = getattr(quotation.salesman, 'branch', None) if quotation.salesman_id else None
    if not path or branch not in BRANCHES or not os.path.exists(path):
        return cfg, None
    address, landline, mobile, email = BRANCHES[branch]
    lines = (address, f'Ph: {landline}  |  Mob: {mobile}', f'{email}  |  www.junaidworld.com')
    return {**cfg, 'letterhead': path, 'phone': landline}, lines   # the branch's own landline is the one in its header


def _draw_address(canvas, doc, lines):
    """Print the branch address lines under the logo on every page (never lets a drawing error break the PDF)."""
    try:
        canvas.saveState()
        canvas.setFont(*ADDRESS_FONT)
        canvas.setFillColorRGB(0.1, 0.1, 0.1)
        for text, baseline in zip(lines, ADDRESS_BASELINES):
            canvas.drawRightString(ADDRESS_RIGHT_X, PAGE_H - baseline * _LH_SCALE - _LH_DY, text)
        canvas.restoreState()
    except Exception:
        log.exception('Branch address could not be drawn')


def build_so_quotation_pdf(quotation):
    """Return the finished PDF (letterhead + content) for an so.Quotation as bytes."""
    cfg = DIVISION_CONFIG.get(quotation.division) or DIVISION_CONFIG['JUNAID']
    cfg, address_lines = _branch_setup(quotation, cfg)
    body_buf = BytesIO()
    doc = BaseDocTemplate(
        body_buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
        topMargin=cfg['top'], bottomMargin=cfg['bottom'], title=f'Quotation {quotation.quotation_number}',
    )
    frame = Frame(MARGIN_X, cfg['bottom'], CONTENT_W, PAGE_H - cfg['top'] - cfg['bottom'],
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    on_page = (lambda canvas, doc: _draw_address(canvas, doc, address_lines)) if address_lines else (lambda canvas, doc: None)
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame], onPage=on_page)])
    doc.build(_body(quotation, cfg))
    body_buf.seek(0)

    writer = PdfWriter()
    for body_page in PdfReader(body_buf).pages:
        base = _letterhead_page(cfg)
        base.merge_page(body_page)
        writer.add_page(base)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------------------------------------------------
# SAP quotation (so.SAPQuotation -> sapquotations/<number>/ -> Export PDF). Same letterhead design as above; separate
# functions so nothing used by build_so_quotation_pdf is changed.
# ---------------------------------------------------------------------------------------------------------------------
_SAP_WEIGHTS = [20, 70, 230, 44, 64, 68]
_SAP_COL_W = [CONTENT_W * w / sum(_SAP_WEIGHTS) for w in _SAP_WEIGHTS]


def _dec(value):
    from decimal import Decimal
    if value is None:
        return Decimal('0')
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        return Decimal('0')


def _sap_header_block(q):
    sep = 6
    avail = CONTENT_W - 2 * sep
    w1, w2, w3 = avail * 0.40, avail * 0.38, avail * 0.22
    gap = 14

    address = '<br/>'.join(escape(ln.strip()) for ln in (q.bill_to or '').splitlines() if ln.strip())
    to_rows = [('Customer name', _val(q.customer_name, True)), ('Customer ID', _val(q.customer_code))]
    if address:
        to_rows.insert(1, ('Address', Paragraph(address, S_VAL)))
    to = [Paragraph('<b>TO:</b>', S_ADDR), Spacer(1, 2), _label_table(to_rows, w1 - gap)]

    ref = _label_table([
        ('Quotation no.', _val(q.q_number, True)),
        ('Customer ref.', _val(q.bp_reference_no)),
        ('Salesperson', _val(q.salesman_name)),
    ], w2 - gap)

    date_txt = q.posting_date.strftime('%d.%m.%Y') if q.posting_date else ''
    right = Table([[Paragraph('Date', S_MID_LBL)], [Paragraph(_txt(date_txt) or '&nbsp;', S_MID_VAL)]], colWidths=[w3 - gap])
    right.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))

    outer = Table([[to, '', ref, '', right]], colWidths=[w1, sep, w2, sep, w3])
    outer.setStyle(TableStyle([
        ('BOX', (0, 0), (0, 0), 0.8, colors.black),
        ('BOX', (2, 0), (2, 0), 0.8, colors.black),
        ('BOX', (4, 0), (4, 0), 0.8, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (1, 0), (1, 0), 0), ('RIGHTPADDING', (1, 0), (1, 0), 0),
        ('LEFTPADDING', (3, 0), (3, 0), 0), ('RIGHTPADDING', (3, 0), (3, 0), 0),
    ]))
    return outer


def _sap_items_table(q):
    from decimal import Decimal
    heads = ['#', 'ITEM CODE', 'DESCRIPTION', 'QTY', f'UNIT PRICE<br/>{CURRENCY}', f'TOTAL<br/>{CURRENCY}']
    head_style = [S_HEAD_C, S_HEAD_C, S_HEAD_L, S_HEAD_C, S_HEAD_R, S_HEAD_R]
    rows = [[Paragraph(h, hs) for h, hs in zip(heads, head_style)]]
    style = [
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (0, -1), 0),
        ('LINEABOVE', (0, 0), (-1, 0), 0.8, colors.black),
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, colors.black),
    ]
    subtotal = Decimal('0')
    for n, it in enumerate(q.items.all().order_by('id'), 1):
        qty, price = _dec(it.quantity), _dec(it.price)
        line_total = _dec(it.row_total) if it.row_total is not None else qty * price
        subtotal += line_total
        qty_txt = f'{qty:f}'.rstrip('0').rstrip('.') if qty else '0'
        rows.append([
            Paragraph(str(n), S_CELL_C),
            Paragraph(_txt(it.item_no), S_CELL_C),
            Paragraph(_txt(it.description) or 'No description available', S_CELL),
            Paragraph(qty_txt, S_CELL_C),
            Paragraph(_money(price), S_RIGHT),
            Paragraph(_money(line_total), S_RIGHT),
        ])
    first_total_row = len(rows)
    if first_total_row > 1:
        style.append(('LINEBELOW', (0, first_total_row - 1), (-1, first_total_row - 1), 0.6, colors.black))

    vat = (subtotal * Decimal(str(VAT_RATE))).quantize(Decimal('0.01'))
    # document_total from SAP may be with or without VAT; the grand total always includes the VAT line shown above
    # (SAP's own figure is used only when it already matches subtotal + VAT, e.g. to keep its rounding).
    grand_total = subtotal + vat
    doc_total = _dec(q.document_total)
    if doc_total and abs(doc_total - grand_total) <= Decimal('1'):
        grand_total = doc_total
    for label, value, bold in ((f'TOTAL AMOUNT EXCL. VAT ({CURRENCY})', subtotal, False),
                               (f'VAT {VAT_RATE:.0%} ({CURRENCY})', vat, False),
                               (f'GRAND TOTAL ({CURRENCY})', grand_total, True)):
        r = len(rows)
        s = S_RIGHT_B if bold else S_RIGHT
        rows.append([''] * 2 + [Paragraph(label, s)] + [''] * 2 + [Paragraph(_money(value), s)])
        style += [('SPAN', (2, r), (4, r)), ('TOPPADDING', (0, r), (-1, r), 2), ('BOTTOMPADDING', (0, r), (-1, r), 2)]
        if bold:
            style += [('LINEABOVE', (2, r), (5, r), 0.6, colors.black), ('TOPPADDING', (0, r), (-1, r), 4)]
    r = len(rows)
    words = amount_in_words(round(float(grand_total), 2))
    rows.append([Paragraph(f'<b>{CURRENCY} : {escape(words)}</b>', S_WORDS)] + [''] * 5)
    style += [('SPAN', (0, r), (-1, r)), ('TOPPADDING', (0, r), (-1, r), 8)]
    style.append(('NOSPLIT', (0, first_total_row - 1), (-1, len(rows) - 1)))

    t = Table(rows, colWidths=_SAP_COL_W, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def _sap_solar_brand(q):
    """'VERA' / 'ARISTON' when EVERY line of the SAP quotation matches a catalog item (item_no == item_code) whose
    firm is exactly VERA SOLAR / ARISTON SOLAR (same strict rule as _solar_brand). Anything else -- an unmatched code,
    mixed brands, no items, or any error -- gives None, so the standard terms are printed."""
    try:
        from .models import Items
        lines = list(q.items.all().order_by('id'))
        codes = {str(li.item_no).strip() for li in lines if li.item_no and str(li.item_no).strip()}
        if not lines or len(codes) == 0 or any(not (li.item_no and str(li.item_no).strip()) for li in lines):
            return None
        firms = {}
        for code, firm in Items.objects.filter(item_code__in=codes).values_list('item_code', 'item_firm'):
            firms[str(code).strip()] = firm
        brands = []
        for li in lines:
            firm = ' '.join(str(firms.get(str(li.item_no).strip(), '') or '').upper().replace('-', ' ').split())
            if firm == 'VERA SOLAR':
                brands.append('VERA')
            elif firm == 'ARISTON SOLAR':
                brands.append('ARISTON')
            else:
                return None
        return brands[0] if brands else None
    except Exception:
        log.exception('SAP solar brand check failed for %s', getattr(q, 'q_number', ''))
        return None


def _sap_body(q, cfg):
    story = [
        Paragraph('<u>QUOTATION</u>', S_TITLE),
        Spacer(1, 8),
        _sap_header_block(q),
        Spacer(1, 10),
        _sap_items_table(q),
        Spacer(1, 14),
    ]
    if q.remarks and q.remarks.strip():
        remark_rows = [[Paragraph(escape(ln.strip()) or '&nbsp;', S_CELL)] for ln in q.remarks.splitlines() if ln.strip()]
        story += [KeepTogether([_section('REMARKS', remark_rows)]), Spacer(1, 10)]
    stamp = _stamp(cfg.get('stamp'))
    stamp_row = None
    if stamp is not None:
        stamp_row = Table([[stamp]], colWidths=[CONTENT_W])
        stamp_row.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                                       ('LEFTPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 10)]))

    brand = _sap_solar_brand(q)
    if brand:
        # Vera Solar / Ariston Solar only: same block as the app quotation (no payment-terms point: SAP has none).
        points, n = [], 0
        for entry in SOLAR_TERMS:
            if isinstance(entry, tuple):
                kind, text = entry
                points.append([Paragraph(escape(text), S_BOLD if kind == 'H' else _base)])
            else:
                n += 1
                text = entry.format(brand=brand, phone=cfg.get('phone') or DIVISION_CONFIG['JUNAID']['phone'])
                points.append([Paragraph(f'{n}.&nbsp;&nbsp;{escape(text)}', S_POINT)])
        split_at = next((i for i, e in enumerate(SOLAR_TERMS) if isinstance(e, tuple) and e[0] == 'H'), None)
        if split_at is None:
            story.append(_section('TERMS AND CONDITIONS', points))
            if stamp_row is not None:
                story.append(stamp_row)
        else:
            story.append(_section('TERMS AND CONDITIONS', points[:split_at]))
            end = Table(points[split_at:], colWidths=[CONTENT_W])
            end.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('TOPPADDING', (0, 0), (-1, -1), 1.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (0, 0), 6),
            ]))
            story.append(KeepTogether([end] + ([stamp_row] if stamp_row is not None else [])))
        return story

    points = [[Paragraph(f'{n}.&nbsp;&nbsp;{escape(text)}', S_POINT)] for n, text in enumerate(TERMS, 1)]
    tail = [_section('TERMS AND CONDITIONS', points)]
    if stamp_row is not None:
        tail.append(stamp_row)
    story.append(KeepTogether(tail))
    return story


def build_sap_quotation_pdf(quotation):
    """Return the finished PDF (Junaid letterhead + content) for an so.SAPQuotation as bytes."""
    cfg = DIVISION_CONFIG['JUNAID']
    body_buf = BytesIO()
    doc = BaseDocTemplate(
        body_buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
        topMargin=cfg['top'], bottomMargin=cfg['bottom'], title=f'Quotation {quotation.q_number}',
    )
    frame = Frame(MARGIN_X, cfg['bottom'], CONTENT_W, PAGE_H - cfg['top'] - cfg['bottom'],
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])
    doc.build(_sap_body(quotation, cfg))
    body_buf.seek(0)

    writer = PdfWriter()
    for body_page in PdfReader(body_buf).pages:
        base = _letterhead_page(cfg)
        base.merge_page(body_page)
        writer.add_page(base)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()

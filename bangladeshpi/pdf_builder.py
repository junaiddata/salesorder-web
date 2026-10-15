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
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph, Spacer,
    Table, TableStyle,
)

PAGE_W, PAGE_H = A4
MARGIN_X = 36
TOP_RESERVED = 100      # letterhead logo band
BOTTOM_RESERVED = 95    # letterhead contact footer starts ~75pt from the bottom edge
CONTENT_W = PAGE_W - 2 * MARGIN_X

LETTERHEAD_PATH = os.path.join(settings.MEDIA_ROOT, 'BDLetterhead.pdf')

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


def _details_table(pi, variant='customer'):
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
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    return t


def _items_table(pi, variant='customer'):
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
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f2f2f2')),
    ]
    for it in pi.items.all():
        r = len(rows)
        if it.kind == 'heading':
            text = it.description or it.code
            rows.append([Paragraph(escape(text), S_BOLD), '', '', '', ''])
            style.append(('SPAN', (0, r), (-1, r)))
            continue
        rows.append([
            _p(it.code), _p(it.description),
            Paragraph(_qty(it.qty), S_CENTER),
            Paragraph(_money(it.bank_unit_price if bank else it.unit_price), S_RIGHT),
            Paragraph(_money(it.bank_amount if bank else it.amount), S_RIGHT),
        ])

    country = (pi.country_of_origin or '').upper()
    if bank:
        # Bank copy: a single total, no Ex-Works / freight lines.
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


def _signature_and_bank():
    sig = Table(
        [[Paragraph('<b>CONFIRMED AND ACCEPTED BY EXPORTER:</b>', S_BASE),
          Paragraph('<b>CONFIRMED AND ACCEPTED BY BUYER:</b>', S_BASE)]],
        colWidths=[CONTENT_W * 0.5, CONTENT_W * 0.5], rowHeights=[68],
    )
    sig.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))

    bank_rows = [[Paragraph('<b>BANK ACCOUNT DETAILS [PAYMENT IN USD]</b>', S_BASE)]]
    bank_rows += [[_p(ln)] for ln in BANK_LINES]
    bank = Table(bank_rows, colWidths=[CONTENT_W])
    bank.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([sig, Spacer(1, 12), bank])


def _body(pi, variant='customer'):
    story = [
        Paragraph('<u>PROFORMA INVOICE</u>', S_TITLE),
        Spacer(1, 6),
        _header_block(pi),
        Spacer(1, 4),
        _details_table(pi, variant),
        Spacer(1, 4),
        _items_table(pi, variant),
        Spacer(1, 10),
        Paragraph(IMPORTANT_NOTE, S_NOTE),
    ]
    if variant == 'customer' and pi.freight_valid_till:
        story.append(Spacer(1, 6))
        story.append(Paragraph(f'<b>Freight Rate Valid Till {escape(pi.freight_valid_till)} only</b>', S_NOTE))
    if variant == 'bank':
        story += _terms_flowables(pi)
    story += [Spacer(1, 10), _signature_and_bank()]
    return story


def build_pi_pdf(pi, variant='customer'):
    """Return the finished PDF (letterhead + content) as bytes.

    variant: 'customer' (Ex-Works/freight/CFR totals) or 'bank' (bank unit
    prices, single total, L/C terms).
    """
    body_buf = BytesIO()
    doc = BaseDocTemplate(
        body_buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
        topMargin=TOP_RESERVED, bottomMargin=BOTTOM_RESERVED,
        title=f'Proforma Invoice {pi.ref_no}',
    )
    frame = Frame(MARGIN_X, BOTTOM_RESERVED, CONTENT_W, PAGE_H - TOP_RESERVED - BOTTOM_RESERVED,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])
    doc.build(_body(pi, variant))
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

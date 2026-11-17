"""
Commercial Invoice PDF.

A separate builder from the PI PDF (nothing in pdf_builder.py is changed): it only
re-uses the letterhead constants and the signature/stamp image helper from it.

Design: clean and flat. No shaded backgrounds, no boxes and no divider lines; sections are
separated by spacing and labelled with small navy bold captions. Only the items table has lines.
"""
from io import BytesIO
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from .models import BangladeshPISeal
from .pdf_builder import (
    BOTTOM_RESERVED, CONTENT_W, LETTERHEAD_PATH, MARGIN_X, PAGE_H, TOP_RESERVED, _seal_image,
)

# ---- fixed text (as on the sample invoice). Edit here if any of it ever changes. ----
CUSTOMER_ID = 'IS100092'
NOTIFY_LINES = [
    'M/S.IMAA CORPORATION',
    '316, CHOWDHURY MACHINERY MARKET,',
    'JUBILEE ROAD, ENAYET BAZAR,',
    'CHITTAGONG, BANGLADESH.',
]
ALSO_NOTIFY_LINES = [
    'NATIONAL CREDIT AND COMMERCE BANK LIMITED',
    'JUBILEE ROAD BRANCH, DOMINION PLAZA (1ST FLOOR),',
    '5, JUBILEE ROAD, CHATTOGRAM,',
    'BANGLADESH.',
]
DELIVERY_TERMS = 'CFR CHATTOGRAM SEAPORT, BANGLADESH'
PACKING = 'CARDBOARD BOXES'
SHIPMENT_TEXT = '1X40 HQ FCL'
GOODS_NAME = 'ELECTRIC STORAGE WATER HEATERS'
DEFAULT_HS_CODE = '8516.10.90'
IRC_NUMBER = '260318110056219'
APPLICANT_BIN = '000514371-0503'
APPLICANT_TIN = '792633731293'
ISSUING_BANK_BIN = '000483386-0202'
SHIPPING_MARK = 'IJPL'
SIGNATORY = 'Mr. NASHEER AHAMMAD'
EXPORTER_NAME = 'JUNAID SANITARY AND ELECTRICAL REQUISITES TRADING LLC'
UOM = 'PZ'

# The right-hand blocks (NOTIFY at the top, ALSO NOTIFY at the bottom) share one left edge, so they line
# up with each other and the widest line (ALSO NOTIFY, ~218pt) ends at the table's right edge.
RIGHT_BLOCK_START = 0.58

NAVY = colors.HexColor('#1f3a68')
RULE = colors.HexColor('#9fb0cc')

_base = ParagraphStyle('ci', fontName='Helvetica', fontSize=7.9, leading=10.4)
S = _base
S_LABEL = ParagraphStyle('cil', parent=_base, fontName='Helvetica-Bold', fontSize=7, leading=9.5, textColor=colors.black)
S_TITLE = ParagraphStyle('cit', parent=_base, fontName='Helvetica-Bold', fontSize=13, leading=16,
                         alignment=TA_CENTER, textColor=colors.black)
S_CENTER = ParagraphStyle('cic', parent=_base, alignment=TA_CENTER)
S_RIGHT = ParagraphStyle('cir', parent=_base, alignment=TA_RIGHT)
S_RIGHT_B = ParagraphStyle('cirb', parent=S_RIGHT, fontName='Helvetica-Bold')
S_HEAD = ParagraphStyle('cih', parent=_base, fontName='Helvetica-Bold', alignment=TA_CENTER,
                        fontSize=7, leading=9, textColor=colors.black)
S_NOTE = ParagraphStyle('cin', parent=_base, leading=11.2, spaceAfter=1.5)


def _t(value):
    return escape(str(value or ''))


def _p(text, style=S):
    return Paragraph(text, style)


def _label(text):
    return Paragraph(text, S_LABEL)


def _money(v):
    return f'{v:,.2f}'


def _num(v):
    return '' if v is None else f'{v:,.2f}'


def _qty(v):
    return f'{v:,.0f}' if v == v.to_integral_value() else f'{v:,.2f}'


def _date(d):
    return d.strftime('%d.%m.%Y') if d else ''


def _flat(rows, widths):
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 1.6), ('BOTTOMPADDING', (0, 0), (-1, -1), 1.6),
    ]))
    return t


def _header(ci):
    q = CONTENT_W / 4
    rows = [
        [_label('DATED'), _label('COMM. REF. NO.'), _label('INVOICE NO'), _label('CUSTOMER')],
        [_p(f'<b>{_date(ci.invoice_date)}</b>'), _p(f'<b>{_t(ci.comm_ref_no) or "&nbsp;"}</b>'),
         _p(f'<b>{_t(ci.invoice_no)}</b>'), _p(f'<b>{CUSTOMER_ID}</b>')],
    ]
    return _flat(rows, [q * 0.8, q * 0.9, q * 1.4, q * 0.9])


def _parties(ci):
    left_w, right_w = CONTENT_W * RIGHT_BLOCK_START, CONTENT_W * (1 - RIGHT_BLOCK_START)
    order = '<br/>'.join(_t(l) for l in (ci.to_order_of or '').splitlines() if l.strip()) or '&nbsp;'
    notify = '<br/>'.join(_t(l) for l in NOTIFY_LINES)
    return _flat([[_label('TO THE ORDER OF'), _label('NOTIFY')], [_p(order), _p(notify)]], [left_w, right_w])


def _terms(ci):
    lc = ''
    if ci.lc_number or ci.lc_date:
        lc = f' NO: {_t(ci.lc_number)}'
        if ci.lc_date:
            lc += f' &nbsp;DATED: {_date(ci.lc_date)}'
    cont = f' &nbsp;CONTAINER NO.: <b>{_t(ci.container_no)}</b>' if ci.container_no else ' &nbsp;CONTAINER NO.:'
    rows = [
        [_label('DELIVERY TERMS'), _p(DELIVERY_TERMS)],
        [_label('PACKING'), _p(f'{PACKING} &nbsp;&nbsp;&nbsp;&nbsp; PACKAGES: <b>{_t(ci.packages)}</b>')],
        [_label('PAYMENT TERMS'), _p(f'LETTER OF CREDIT AT SIGHT{lc}')],
        [_label('SHIPMENT'), _p(f'{SHIPMENT_TEXT}{cont}')],
        [_label('WEIGHT / VOLUME'),
         _p(f'GROSS WEIGHT: <b>{_num(ci.gross_weight)} KG</b> &nbsp;&nbsp;&nbsp;&nbsp; '
            f'NET WEIGHT: <b>{_num(ci.net_weight)} KG</b> &nbsp;&nbsp;&nbsp;&nbsp; '
            f'VOLUME: <b>{_num(ci.volume)} M3</b>')],
    ]
    return _flat(rows, [92, CONTENT_W - 92])


def _goods_statement(ci):
    hs = _t(ci.hs_code or DEFAULT_HS_CODE)
    ref, d = _t(ci.pi_ref_no), _date(ci.pi_date)
    return [
        _p(f'<b>DESCRIPTION OF GOODS &amp;/OR SERVICES:</b> '
           f'{GOODS_NAME} (HS CODE NO. {hs})', S_NOTE),
        _p('DESCRIPTION, QUALITY, QUANTITY, UNIT PRICE AND ALL OTHER TERMS, CONDITIONS AND SPECIFICATIONS OF THE '
           f'GOODS AS PER BENEFICIARY\'S PROFORMA INVOICE NO <b>{ref}</b> DATED <b>{d}</b>.', S_NOTE),
        _p(f'ALL PRICE <b>{DELIVERY_TERMS}</b> (INCOTERM 2020)', S_NOTE),
    ]


def _items_table(ci):
    fixed = 30 + 44 + 44 + 52 + 40 + 34 + 58 + 62
    w = [30, 44, 44, 52, CONTENT_W - fixed, 40, 34, 58, 62]
    rows = [[_p('PACK', S_HEAD), _p('GROSS W<br/>KG', S_HEAD), _p('NET W<br/>KG', S_HEAD), _p('CODE', S_HEAD),
             _p('ITEM DESCRIPTION', S_HEAD), _p('QTY', S_HEAD), _p('UNIT<br/>MEAS.', S_HEAD),
             _p('UNIT PRICE<br/>(USD)', S_HEAD), _p('TOTAL<br/>(USD)', S_HEAD)]]
    style = [
        ('BOX', (0, 0), (-1, -1), 0.9, NAVY),
        ('INNERGRID', (0, 0), (-1, -1), 0.4, RULE),
        ('LINEBELOW', (0, 0), (-1, 0), 0.9, NAVY),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2.6), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.6),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ]
    for l in ci.lines.all():
        if l.kind != 'item':
            continue                  # the PI's product heading row is not shown on the invoice
        rows.append([
            _p(_t(l.pack if l.pack is not None else ''), S_CENTER),
            _p(_num(l.gross_kg), S_RIGHT), _p(_num(l.net_kg), S_RIGHT),
            _p(_t(l.code)), _p(_t(l.description)),
            _p(_qty(l.qty), S_CENTER), _p(UOM, S_CENTER),
            _p(_money(l.unit_price), S_RIGHT), _p(_money(l.amount), S_RIGHT),
        ])

    totals = [('CFR CHATTOGRAM, BANGLADESH', ci.cfr_total)]
    if ci.freight_charges:
        totals += [('FREIGHT CHARGES &nbsp; USD', ci.freight_charges), ('FOB CHARGES &nbsp; USD', ci.fob_total)]
    first_total = len(rows)
    for label, val in totals:
        r = len(rows)
        rows.append([_p(f'<b>{label}</b>', S_RIGHT), '', '', '', '', '', '', '', _p(_money(val), S_RIGHT_B)])
        style.append(('SPAN', (0, r), (7, r)))
    style.append(('LINEABOVE', (0, first_total), (-1, first_total), 0.9, NAVY))
    style.append(('LINEBELOW', (0, len(rows) - 1), (-1, len(rows) - 1), 0.9, NAVY))
    t = Table(rows, colWidths=w, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def _legal(ci):
    hs = _t(ci.hs_code or DEFAULT_HS_CODE)
    origin = _t((ci.country_of_origin or 'ITALY').upper())
    ref, d = _t(ci.pi_ref_no), _date(ci.pi_date)
    return [
        _p(f'<b>MERCHANDISE IS OF {origin} ORIGIN</b>', S_NOTE),
        _p(f'IRC NUMBER <b>{IRC_NUMBER}</b>, H.S CODE NUMBER <b>{hs}</b>.', S_NOTE),
        _p('WE CERTIFY THAT THE QUALITY, QUANTITY, UNIT PRICE AND ALL OTHER TERMS, CONDITIONS AND SPECIFICATIONS OF '
           f'THE GOODS AS PER BENEFICIARY\'S PROFORMA INVOICE NO <b>{ref}</b> DATE <b>{d}</b>.', S_NOTE),
        _p('IMPORT UNDER CFR', S_NOTE),
        _p(f'APPLICANTS\' BIN: <b>{APPLICANT_BIN}</b> &nbsp;&amp;&nbsp; TIN NO: <b>{APPLICANT_TIN}</b> &nbsp;AND&nbsp; '
           f'ISSUING BANK\'S BIN NO.: <b>{ISSUING_BANK_BIN}</b>', S_NOTE),
        _p(f'SHIPPING MARK: <b>{SHIPPING_MARK}</b> &nbsp;&nbsp;&nbsp;&nbsp; <b>FREIGHT PREPAID</b>', S_NOTE),
    ]


def _closing(ci, with_seals):
    """Left: the exporter's sign-off with signature and stamp. Right: ALSO NOTIFY."""
    also = '<br/>'.join(_t(l) for l in ALSO_NOTIFY_LINES)
    left = [_label('ALSO NOTIFY'), Spacer(1, 2), _p(also)]

    seal = BangladeshPISeal.get_instance()
    sig = _seal_image(seal.signature, 120, 44, remove_bg=True) if (with_seals and ci.add_signature) else None
    stamp = _seal_image(seal.stamp, 80, 60, remove_bg=True) if (with_seals and ci.add_stamp) else None
    pics = [p for p in (sig, stamp) if p is not None]
    right = [_p(f'<b>FOR {EXPORTER_NAME}</b>')]
    if pics:
        pt = Table([pics], colWidths=[p.drawWidth + 10 for p in pics], rowHeights=[max(p.drawHeight for p in pics) + 4])
        pt.hAlign = 'LEFT'
        pt.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
        right.append(pt)
    else:
        right.append(Spacer(1, 42))
    right.append(_p(f'<b>{SIGNATORY}</b>'))

    t = Table([[right, left]], colWidths=[CONTENT_W * RIGHT_BLOCK_START, CONTENT_W * (1 - RIGHT_BLOCK_START)])   # sign-off left, ALSO NOTIFY right
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                           ('RIGHTPADDING', (0, 0), (0, 0), 6), ('RIGHTPADDING', (1, 0), (1, 0), 0)]))
    return KeepTogether([t])


def _story(ci, with_seals):
    story = [
        Paragraph('COMMERCIAL INVOICE', S_TITLE), Spacer(1, 9),
        _header(ci), Spacer(1, 9),
        _parties(ci), Spacer(1, 9),
        _terms(ci), Spacer(1, 9),
    ]
    story += _goods_statement(ci)
    story += [
        Spacer(1, 4),
        _p(f'OUR DELIVERY: <b>{_t(ci.comm_ref_no)}</b> &nbsp;&nbsp;&nbsp; REF OUR ORD NO: <b>{_t(ci.comm_ref_no)}</b> '
           f'&nbsp;&nbsp;&nbsp; VS.: <b>{_t(ci.pi_ref_no)}</b>', S_CENTER),
        Spacer(1, 4),
        _items_table(ci),
        Spacer(1, 10),
    ]
    story += _legal(ci)
    story += [Spacer(1, 12), _closing(ci, with_seals)]
    return story


def build_commercial_invoice_pdf(ci, with_seals=True):
    """Return the commercial invoice (letterhead + content) as PDF bytes."""
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
        topMargin=TOP_RESERVED, bottomMargin=BOTTOM_RESERVED,
        title=f'Commercial Invoice {ci.invoice_no}',
    )
    frame = Frame(MARGIN_X, BOTTOM_RESERVED, CONTENT_W, PAGE_H - TOP_RESERVED - BOTTOM_RESERVED,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])
    doc.build(_story(ci, with_seals))
    buf.seek(0)

    writer = PdfWriter()
    for body_page in PdfReader(buf).pages:
        base = PdfReader(LETTERHEAD_PATH).pages[0]
        base.merge_page(body_page)
        writer.add_page(base)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()

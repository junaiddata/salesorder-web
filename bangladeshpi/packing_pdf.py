"""
Packing List PDF, made from the same saved Commercial Invoice (no prices, no totals).

A separate builder: commercial_pdf.py is only imported from, never changed. FOC items are not
on the invoice snapshot, so they are not on the packing list either.
"""
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle

from .commercial_pdf import (
    CUSTOMER_ID, GOODS_NAME, NAVY, RULE, S_CENTER, S_HEAD, S_RIGHT, S_TITLE, UOM, _closing, _date, _flat,
    _goods_statement, _label, _legal, _num, _p, _parties, _qty, _t, _terms,
)
from .pdf_builder import (
    BOTTOM_RESERVED, CONTENT_W, LETTERHEAD_PATH, MARGIN_X, PAGE_H, TOP_RESERVED,
)


def packing_list_no(ci):
    """Typed number if there is one, else the invoice number with /PL inserted before its last part."""
    if (ci.pl_no or '').strip():
        return ci.pl_no.strip()
    head, sep, tail = (ci.invoice_no or '').rpartition('/')
    return f'{head}/PL/{tail}' if sep else f'{ci.invoice_no}/PL'


def _header(ci):
    q = CONTENT_W / 4
    rows = [
        [_label('DATED'), _label('COMM. REF. NO.'), _label('INVOICE NO'), _label('CUSTOMER')],
        [_p(f'<b>{_date(ci.invoice_date)}</b>'), _p(f'<b>{_t(ci.comm_ref_no) or "&nbsp;"}</b>'),
         _p(f'<b>{_t(packing_list_no(ci))}</b>'), _p(f'<b>{CUSTOMER_ID}</b>')],
    ]
    return _flat(rows, [q * 0.8, q * 0.9, q * 1.4, q * 0.9])


def _items_table(ci):
    fixed = 34 + 52 + 52 + 60 + 44 + 44
    w = [34, 52, 52, 60, CONTENT_W - fixed, 44, 44]
    rows = [[_p('PACK', S_HEAD), _p('GROSS W<br/>KG', S_HEAD), _p('NET W<br/>KG', S_HEAD), _p('CODE', S_HEAD),
             _p('ITEM DESCRIPTION', S_HEAD), _p('QTY', S_HEAD), _p('UNIT<br/>MEAS.', S_HEAD)]]
    hs = (ci.hs_code or '').replace('.', '')
    head_text = f'{hs} : {GOODS_NAME}' if hs else GOODS_NAME
    rows.append(['', '', '', '', _p(f'<b>{_t(head_text)}</b>'), '', ''])
    for l in ci.lines.all():
        if l.kind != 'item':
            continue
        rows.append([
            _p(_t(l.pack if l.pack is not None else ''), S_CENTER),
            _p(_num(l.gross_kg), S_RIGHT), _p(_num(l.net_kg), S_RIGHT),
            _p(_t(l.code)), _p(_t(l.description)), _p(_qty(l.qty), S_CENTER), _p(UOM, S_CENTER),
        ])
    t = Table(rows, colWidths=w, repeatRows=1)
    t.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.9, NAVY), ('INNERGRID', (0, 0), (-1, -1), 0.4, RULE),
        ('LINEBELOW', (0, 0), (-1, 0), 0.9, NAVY), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2.6), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.6),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ]))
    return t


def _story(ci, with_seals):
    story = [
        Paragraph('PACKING LIST', S_TITLE), Spacer(1, 9),
        _header(ci), Spacer(1, 9),
        _parties(ci), Spacer(1, 9),
        _terms(ci), Spacer(1, 9),
    ]
    story += _goods_statement(ci)
    story += [
        Spacer(1, 4),
        _p(f'OUR DELIVERY: <b>{_t(ci.our_delivery)}</b> &nbsp;&nbsp;&nbsp; REF OUR ORD NO: <b>{_t(ci.ref_our_ord_no)}</b> '
           f'&nbsp;&nbsp;&nbsp; VS.: <b>{_t(ci.pi_ref_no)}</b>', S_CENTER),
        Spacer(1, 4),
        _items_table(ci),
        Spacer(1, 10),
    ]
    story += _legal(ci)
    story += [Spacer(1, 12), _closing(ci, with_seals)]
    return story


def build_packing_list_pdf(ci, with_seals=True):
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
        topMargin=TOP_RESERVED, bottomMargin=BOTTOM_RESERVED, title=f'Packing List {packing_list_no(ci)}',
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

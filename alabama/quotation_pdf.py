"""PDF export of a single Alabama SAP quotation (customer-safe: never shows cost / profit / margin)."""
from io import BytesIO
from xml.sax.saxutils import escape

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse

from .models import AlabamaSAPQuotation


class _QuotationView:
    """Read-only adapter so the app's SAP-quotation PDF layout can print an AlabamaSAPQuotation (it has no remarks)."""
    remarks = None

    def __init__(self, q):
        self._q = q

    def __getattr__(self, name):
        return getattr(self._q, name)


def build_alabama_quotation_pdf(quotation):
    """Same design as the app's quotation PDF (letterhead, TO / reference / date boxes, items, VAT, words, terms, stamp),
    on the Alabama letterhead. Reuses so.quotation_pdf_new unchanged."""
    from io import BytesIO
    from PyPDF2 import PdfReader, PdfWriter
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate
    from reportlab.lib.pagesizes import A4
    from so import quotation_pdf_new as qp

    cfg = qp.DIVISION_CONFIG['ALABAMA']
    body = BytesIO()
    doc = BaseDocTemplate(body, pagesize=A4, leftMargin=qp.MARGIN_X, rightMargin=qp.MARGIN_X,
                          topMargin=cfg['top'], bottomMargin=cfg['bottom'], title=f'Quotation {quotation.q_number}')
    frame = Frame(qp.MARGIN_X, cfg['bottom'], qp.CONTENT_W, qp.PAGE_H - cfg['top'] - cfg['bottom'],
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])
    doc.build(qp._sap_body(_QuotationView(quotation), cfg))
    body.seek(0)
    writer = PdfWriter()
    for page in PdfReader(body).pages:
        base = qp._letterhead_page(cfg)
        base.merge_page(page)
        writer.add_page(base)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _money(v):
    try:
        return f"{float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


@login_required
def export_quotation_pdf(request, q_number):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    quotation = AlabamaSAPQuotation.objects.filter(q_number=q_number).first()
    if not quotation:
        raise Http404("Quotation not found")
    safe = ''.join(c for c in quotation.q_number if c.isalnum() or c in '-_') or 'quotation'
    try:
        resp = HttpResponse(build_alabama_quotation_pdf(quotation), content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="Alabama_Quotation_{safe}.pdf"'
        return resp
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            'Alabama quotation PDF (app design) failed for %s; using the simple layout', quotation.q_number)

    items = list(quotation.items.all().order_by('id'))

    styles = getSampleStyleSheet()
    cell = ParagraphStyle('cell', parent=styles['Normal'], fontSize=8, leading=10)
    title = ParagraphStyle('t', parent=styles['Title'], fontSize=16, spaceAfter=4)
    right = ParagraphStyle('r', parent=styles['Normal'], fontSize=10, alignment=TA_RIGHT)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Quotation {quotation.q_number}")
    story = [Paragraph("Alabama - Quotation", title),
             Paragraph(f"No. {escape(quotation.q_number)}", styles['Heading3']), Spacer(1, 4)]

    def p(text):
        return Paragraph(escape(str(text)) if text not in (None, '') else '-', cell)

    info = [
        [p('Customer'), p(quotation.customer_name + (f" ({quotation.customer_code})" if quotation.customer_code else '')),
         p('Date'), p(quotation.posting_date.strftime('%d %b %Y') if quotation.posting_date else '-')],
        [p('Salesman'), p(quotation.salesman_name), p('Reference'), p(quotation.bp_reference_no)],
        [p('Brand'), p(quotation.brand), p('Status'), p(quotation.status)],
    ]
    t = Table(info, colWidths=[24 * mm, 70 * mm, 24 * mm, 64 * mm])
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.3, colors.lightgrey),
                           ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
                           ('BACKGROUND', (2, 0), (2, -1), colors.whitesmoke),
                           ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story += [t, Spacer(1, 6)]
    if quotation.bill_to:
        story += [Paragraph("<b>Bill To</b>", cell),
                  Paragraph(escape(quotation.bill_to).replace('\n', '<br/>'), cell), Spacer(1, 6)]

    rows = [[p('#'), p('Item Code'), p('Description'), p('Qty'), p('Price'), p('Total')]]
    for i, it in enumerate(items, 1):
        rows.append([p(i), p(it.item_no), p(it.description), p(_money(it.quantity)), p(_money(it.price)),
                     p(_money(it.row_total))])
    lt = Table(rows, colWidths=[8 * mm, 28 * mm, 80 * mm, 18 * mm, 22 * mm, 26 * mm], repeatRows=1)
    lt.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e5e7eb')),
                            ('GRID', (0, 0), (-1, -1), 0.3, colors.lightgrey),
                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                            ('ALIGN', (3, 0), (-1, -1), 'RIGHT')]))
    story += [lt, Spacer(1, 8), Paragraph(f"<b>Document Total: {_money(quotation.document_total)}</b>", right)]

    doc.build(story)
    resp = HttpResponse(buf.getvalue(), content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="Alabama_Quotation_{safe}.pdf"'
    return resp

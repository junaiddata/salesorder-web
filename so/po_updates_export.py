"""
Excel / PDF exports for the PO Updates list page (open PO lines with Received / Balance).
Respects the same filters as the page. Amount columns are admin-only (and only when the
"Include prices" box is ticked), exactly like the legacy PO list exports.
"""
from datetime import datetime
from decimal import Decimal
from io import BytesIO

import pandas as pd
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from django.http import HttpResponse

from .models import POReceipt, POLineBaseline
from .po_receiving_views import _apply_list_filters, _line_calc, _user_can_see_price

ZERO = Decimal("0")


def _export_rows(request):
    """List of (item, calc dict) for every open line matching the page filters."""
    qs, _ = _apply_list_filters(request)
    received = {
        (r["po_number"], r["line_no"]): r["total"] or ZERO
        for r in POReceipt.objects.values("po_number", "line_no").annotate(total=Sum("qty_received"))
    }
    bases = {(p, l): d for p, l, d in POLineBaseline.objects.values_list(
        "po_number", "line_no", "sap_delivered_at_start")}
    rows = []
    for it in qs:
        key = (it.purchaseorder.po_number, it.line_no)
        rec = received.get(key, ZERO)
        open_qty, absorbed, pending, balance = _line_calc(it, rec, bases.get(key))
        rows.append((it, {
            "open": open_qty, "received": rec, "in_sap": min(rec, absorbed), "pending": pending, "balance": balance,
        }))
    return qs, rows


def _show_price(request) -> bool:
    return _user_can_see_price(request) and request.GET.get("show_price") == "1"


@login_required
def export_po_updates_excel(request):
    qs, rows = _export_rows(request)
    show_price = _show_price(request)

    data = []
    for it, c in rows:
        po = it.purchaseorder
        row = {
            "Doc No": po.po_number or "",
            "Posting Date": po.posting_date.strftime("%Y-%m-%d") if po.posting_date else "",
            "Supplier Code": po.supplier_code or "",
            "Supplier Name": po.supplier_name or "",
            "LPO Reference": po.bp_reference_no or "",
            "Purchaser": po.salesman_name or "",
            "Item No": it.item_no or "",
            "Description": it.description or "",
            "Quantity": float(it.quantity or 0),
            "Open Qty": float(c["open"]),
            "Received (App)": float(c["received"]),
            "Posted in SAP": float(c["in_sap"]),
            "Pending in SAP": float(c["pending"]),
            "Balance": float(c["balance"]),
        }
        if show_price:
            row["Price"] = float(it.price or 0)
            row["Row Total"] = float(it.row_total or 0)
            row["Pending Amount"] = float(it.pending_amount or 0)
        data.append(row)

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="PO Updates")
        ws = writer.sheets["PO Updates"]
        from openpyxl.utils import get_column_letter
        for idx, col in enumerate(df.columns, 1):
            col_max = int(df[col].astype(str).map(len).max()) if len(df) > 0 else 0
            ws.column_dimensions[get_column_letter(idx)].width = min(max(col_max, len(str(col))) + 2, 50)
    output.seek(0)

    response = HttpResponse(
        output.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="po_updates_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    return response


@login_required
def export_po_updates_pdf(request):
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, Paragraph, Spacer
    from .finance_statement_pdf_export import (
        _build_styles, _build_document_header, _build_kpi_bar,
        _standard_data_table_style, _build_page_footer, _fmt,
    )

    qs, rows = _export_rows(request)
    show_price = _show_price(request)
    item_count = len(rows)

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="po_updates_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'

    buffer = BytesIO()
    page_w, _ = landscape(A4)
    margin = 24
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=margin, leftMargin=margin,
                            topMargin=margin, bottomMargin=margin + 6)
    usable = page_w - 2 * margin
    styles = _build_styles()
    elements = []

    elements.extend(_build_document_header(
        styles, title_text="PO UPDATES",
        subtitle_text="Open PO lines with received quantity and balance", page_width=usable))

    kpis = [("Line Items", str(item_count)),
            ("Received (App)", _fmt(sum((c["received"] for _, c in rows), ZERO))),
            ("Pending in SAP", _fmt(sum((c["pending"] for _, c in rows), ZERO)))]
    if show_price:
        agg = qs.aggregate(p=Coalesce(Sum("pending_amount"), Value(0, output_field=DecimalField())),
                           t=Coalesce(Sum("row_total"), Value(0, output_field=DecimalField())))
        kpis += [("Pending Total (AED)", _fmt(agg["p"])), ("Row Total (AED)", _fmt(agg["t"]))]
    elements.append(_build_kpi_bar(kpis, styles, usable))
    elements.append(Spacer(1, 10))

    g = request.GET
    notes = []
    if g.get("q", "").strip():
        notes.append(f'Search: "{g.get("q").strip()}"')
    if g.get("item", "").strip():
        notes.append(f'Item: "{g.get("item").strip()}"')
    if g.getlist("firm"):
        notes.append("Brand: " + ", ".join(g.getlist("firm")[:3]) + ("…" if len(g.getlist("firm")) > 3 else ""))
    if g.getlist("purchaser"):
        notes.append("Purchaser: " + ", ".join(g.getlist("purchaser")[:3]) + ("…" if len(g.getlist("purchaser")) > 3 else ""))
    if g.get("start", "").strip():
        notes.append(f'From: {g.get("start").strip()}')
    if g.get("end", "").strip():
        notes.append(f'To: {g.get("end").strip()}')
    if notes:
        elements.append(Paragraph(f'<font color="#6B7280">Filters applied: {"  •  ".join(notes)}</font>', styles["label"]))
        elements.append(Spacer(1, 4))

    def q(n):
        n = Decimal(n)
        return f"{n:,.2f}".rstrip("0").rstrip(".") if n % 1 else f"{int(n):,}"

    widths = [0.85, 0.5, 1.75, 0.65, 2.0, 0.5, 0.5, 0.6, 0.6]
    if show_price:
        widths += [0.55, 0.7]
    widths = [w * inch for w in widths]
    widths[4] += max(0, usable - sum(widths))

    hs, hr = styles["header_cell"], styles["header_cell_r"]
    hdr = [Paragraph("Doc No", hs), Paragraph("Date", hs), Paragraph("Supplier", hs), Paragraph("Item No", hs),
           Paragraph("Description", hs), Paragraph("Qty", hr), Paragraph("Open", hr), Paragraph("Received", hr),
           Paragraph("Balance", hr)]
    if show_price:
        hdr += [Paragraph("Price", hr), Paragraph("Pending Amt", hr)]
    table = [hdr]
    for it, c in rows:
        po = it.purchaseorder
        row = [
            Paragraph(po.po_number or "—", styles["cell"]),
            Paragraph(po.posting_date.strftime("%d/%m/%y") if po.posting_date else "—", styles["cell"]),
            Paragraph((po.supplier_name or "—")[:40], styles["cell"]),
            Paragraph(it.item_no or "—", styles["cell"]),
            Paragraph((it.description or "—")[:60], styles["cell"]),
            Paragraph(q(it.quantity or 0), styles["cell_r"]),
            Paragraph(q(c["open"]), styles["cell_r"]),
            Paragraph(q(c["received"]), styles["cell_r"]),
            Paragraph(q(c["balance"]), styles["cell_r"]),
        ]
        if show_price:
            row += [Paragraph(_fmt(it.price or 0), styles["cell_r"]), Paragraph(_fmt(it.pending_amount or 0), styles["cell_r"])]
        table.append(row)

    t = Table(table, colWidths=widths, repeatRows=1)
    ts = _standard_data_table_style(len(table), has_total_row=False)
    ts.add("ALIGN", (5, 0), (-1, -1), "RIGHT")
    t.setStyle(ts)
    elements.append(t)

    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response

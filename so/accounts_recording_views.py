"""
Accounts Recording list: combined AR invoices + credit memos with per-document tracking.
"""

import json
import logging
import os
from datetime import datetime, date
from decimal import Decimal
from html import escape as html_escape
from io import BytesIO

import pandas as pd
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .combined_ar_query import (
    ACCOUNTS_RECORDING_POSTING_DATE_FLOOR,
    combined_ar_ho_salesmen_list,
    combined_ar_salesmen_list,
    combined_ar_summary_metrics,
    get_combined_ar_filtered_querysets,
)
from .models import (
    AccountsRecordingChangeLog,
    AccountsRecordingEntry,
    SAPARCreditMemo,
    SAPARInvoice,
)
from .sap_salesorder_views import salesman_scope_q_salesorder

logger = logging.getLogger(__name__)

_STATUS_VALUES = {c[0] for c in AccountsRecordingEntry.Status.choices}
# Handed-to dropdown: explicit "same as document salesman" (stored as NULL in DB)
HANDED_SAME_AS_DOC = "__doc_salesman__"

_STATUS_LABELS = dict(AccountsRecordingEntry.Status.choices)

_ACC_LOG_FIELD_LABELS = {
    "ctrl_number": "Ctrl no.",
    "handed_to_salesman": "Handed to",
    "handed_over_date": "Handover date",
    "received_back": "Received back",
    "status": "Status",
    "accounts_internal_remark": "A/c internal remark",
}


def _fmt_acc_log_value(val):
    if val is None or val == "":
        return "—"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    return str(val)


def _entry_snapshot_for_log(entry):
    if not entry:
        return None
    return {
        "ctrl_number": (entry.ctrl_number or "").strip() or None,
        "handed_to_salesman": (entry.handed_to_salesman or "").strip() or None,
        "handed_over_date": entry.handed_over_date.isoformat()
        if entry.handed_over_date
        else None,
        "received_back": bool(entry.received_back),
        "status": entry.status,
        "accounts_internal_remark": (entry.accounts_internal_remark or "").strip() or None,
    }


def _diff_acc_snapshots(old_snap, new_snap):
    """Return {field: {old, new}} for display; empty if nothing changed."""
    if old_snap is None:
        out = {}
        for k, nv in new_snap.items():
            ov = None
            if nv != ov:
                out[k] = {"old": _fmt_acc_log_value(ov), "new": _fmt_acc_log_value(nv)}
        return out
    out = {}
    for k in new_snap:
        ov, nv = old_snap.get(k), new_snap.get(k)
        if ov != nv:
            out[k] = {"old": _fmt_acc_log_value(ov), "new": _fmt_acc_log_value(nv)}
    return out


def _append_accounts_recording_change_log(request, document_kind, document_number, existing_entry, saved_entry):
    old_snap = _entry_snapshot_for_log(existing_entry)
    new_snap = _entry_snapshot_for_log(saved_entry)
    changes = _diff_acc_snapshots(old_snap, new_snap)
    if not changes:
        return
    if "status" in changes:
        ch = changes["status"]
        if old_snap and old_snap.get("status") is not None:
            ch["old"] = _STATUS_LABELS.get(old_snap.get("status"), ch["old"])
        ch["new"] = _STATUS_LABELS.get(new_snap.get("status"), ch["new"])
    AccountsRecordingChangeLog.objects.create(
        user=request.user,
        username=(request.user.get_username() or "")[:150],
        document_kind=document_kind,
        document_number=document_number,
        changes=changes,
    )


def _accounts_recording_access_allowed(user):
    """Salesmen and other non-admin users cannot use Accounts Recording (list, exports, AJAX save)."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    role = getattr(user, "role", None)
    return bool(role and getattr(role, "role", None) == "Admin")


def _require_accounts_recording_access(request):
    if not _accounts_recording_access_allowed(request.user):
        raise PermissionDenied("Accounts Recording is only available to administrators.")


def _get_scoped_document(user, document_kind, document_number):
    if document_kind == AccountsRecordingEntry.DocumentKind.INVOICE:
        qs = SAPARInvoice.objects.filter(invoice_number=document_number)
    elif document_kind == AccountsRecordingEntry.DocumentKind.CREDIT_MEMO:
        qs = SAPARCreditMemo.objects.filter(credit_memo_number=document_number)
    else:
        return None
    if not (user.is_superuser or user.is_staff):
        qs = qs.filter(salesman_scope_q_salesorder(user))
    return qs.first()


def _auto_status(handed_to, handed_date, received_back):
    if received_back:
        return AccountsRecordingEntry.Status.RECEIVED_BY_AC
    hand_to_set = handed_to and handed_to.strip()
    has_handover = handed_date is not None or bool(hand_to_set)
    if has_handover:
        return AccountsRecordingEntry.Status.WITH_SALESMAN
    return AccountsRecordingEntry.Status.PENDING


def _handover_fields_changed(existing, handed_to_salesman, handed_over_date, received_back):
    if not existing:
        return True
    return (
        (existing.handed_to_salesman or "") != (handed_to_salesman or "")
        or existing.handed_over_date != handed_over_date
        or existing.received_back != received_back
    )


def _resolve_final_status(
    existing,
    handed_to,
    handed_date,
    received_back,
    manual_status,
    incoming_status,
):
    if received_back:
        return AccountsRecordingEntry.Status.RECEIVED_BY_AC
    if manual_status and incoming_status in _STATUS_VALUES:
        return incoming_status
    auto = _auto_status(handed_to, handed_date, received_back)
    if (
        existing
        and not _handover_fields_changed(existing, handed_to, handed_date, received_back)
        and existing.status == AccountsRecordingEntry.Status.IN_STORE
    ):
        return AccountsRecordingEntry.Status.IN_STORE
    return auto


def _handed_select_value(entry, doc_salesman_name=None):
    """
    Value for the Handed-to <select>.
    HANDED_SAME_AS_DOC when stored name matches document salesman, or date-only handover (NULL hand-to).
    """
    if not entry:
        return ""
    doc_nm = (doc_salesman_name or "").strip()
    stored = (entry.handed_to_salesman or "").strip()
    if stored and doc_nm and stored == doc_nm:
        return HANDED_SAME_AS_DOC
    if stored:
        return entry.handed_to_salesman.strip()
    if entry.handed_over_date:
        return HANDED_SAME_AS_DOC
    return ""


def _serialize_entry(entry, doc=None):
    raw = (entry.handed_to_salesman or "").strip()
    doc_nm = getattr(doc, "salesman_name", None) if doc else None
    sel = _handed_select_value(entry, doc_nm)
    return {
        "ctrl_number": entry.ctrl_number or "",
        "handed_to_salesman": raw,
        "handed_select_value": sel,
        "handed_over_date": entry.handed_over_date.isoformat() if entry.handed_over_date else "",
        "received_back": entry.received_back,
        "status": entry.status,
        "accounts_internal_remark": entry.accounts_internal_remark or "",
    }


def _parse_date(s):
    if not s or not str(s).strip():
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _entry_map_for_combined_list(combined_list):
    """Batch-load AccountsRecordingEntry rows for all documents in combined_list."""
    pairs = []
    for doc in combined_list:
        if doc.document_type == "Invoice":
            pairs.append((AccountsRecordingEntry.DocumentKind.INVOICE, doc.invoice_number))
        else:
            pairs.append((AccountsRecordingEntry.DocumentKind.CREDIT_MEMO, doc.credit_memo_number))
    entry_map = {}
    if pairs:
        q_obj = Q()
        for dk, dn in pairs:
            q_obj |= Q(document_kind=dk, document_number=dn)
        for e in AccountsRecordingEntry.objects.filter(q_obj):
            entry_map[(e.document_kind, e.document_number)] = e
    return entry_map


def _effective_accounts_status(doc, entry_map):
    if doc.document_type == "Invoice":
        k = (AccountsRecordingEntry.DocumentKind.INVOICE, doc.invoice_number)
    else:
        k = (AccountsRecordingEntry.DocumentKind.CREDIT_MEMO, doc.credit_memo_number)
    ent = entry_map.get(k)
    if not ent:
        return AccountsRecordingEntry.Status.PENDING
    return ent.status


def _filter_combined_list_by_accounts_status(combined_list, accounts_status_filter):
    """
    Filter by Accounts Recording workflow status (pending / with_salesman / in_store / received_by_ac).
    Documents with no saved row count as pending.
    """
    if not accounts_status_filter or accounts_status_filter == "All":
        return combined_list
    if accounts_status_filter not in _STATUS_VALUES:
        return combined_list
    entry_map = _entry_map_for_combined_list(combined_list)
    return [
        d
        for d in combined_list
        if _effective_accounts_status(d, entry_map) == accounts_status_filter
    ]


def _totals_from_combined_doc_list(combined_list):
    tw = Decimal("0")
    gp = Decimal("0")
    for d in combined_list:
        v = getattr(d, "doc_total_without_vat", None)
        if v is not None:
            tw += v
        g = getattr(d, "total_gross_profit", None)
        if g is not None:
            gp += g
    return tw, gp


def _build_accounts_recording_export_data(request, user):
    """Full filtered combined list (no pagination) + AccountsRecordingEntry map."""
    invoice_qs, creditmemo_qs, p = get_combined_ar_filtered_querysets(
        request,
        user,
        salesman_scope_q_salesorder,
        default_store_when_unspecified="HO",
        posting_date_start_floor=ACCOUNTS_RECORDING_POSTING_DATE_FLOOR,
    )
    invoice_qs = invoice_qs.order_by("-posting_date", "-invoice_number")
    creditmemo_qs = creditmemo_qs.order_by("-posting_date", "-credit_memo_number")

    invoice_list = list(invoice_qs)
    creditmemo_list = list(creditmemo_qs)
    for inv in invoice_list:
        setattr(inv, "document_type", "Invoice")
        setattr(inv, "document_number", inv.invoice_number)
    for cm in creditmemo_list:
        setattr(cm, "document_type", "Credit Memo")
        setattr(cm, "document_number", cm.credit_memo_number)

    combined_list = invoice_list + creditmemo_list
    combined_list.sort(
        key=lambda x: (
            x.posting_date if x.posting_date else datetime.min.date(),
            x.document_number if x.document_number else "",
        ),
        reverse=True,
    )

    accounts_status_filter = request.GET.get("accounts_status", "").strip()
    combined_list = _filter_combined_list_by_accounts_status(
        combined_list, accounts_status_filter
    )

    pairs = []
    for doc in combined_list:
        if doc.document_type == "Invoice":
            pairs.append((AccountsRecordingEntry.DocumentKind.INVOICE, doc.invoice_number))
        else:
            pairs.append((AccountsRecordingEntry.DocumentKind.CREDIT_MEMO, doc.credit_memo_number))

    entry_map = {}
    if pairs:
        q_obj = Q()
        for dk, dn in pairs:
            q_obj |= Q(document_kind=dk, document_number=dn)
        for e in AccountsRecordingEntry.objects.filter(q_obj):
            entry_map[(e.document_kind, e.document_number)] = e

    p_out = dict(p)
    p_out["accounts_status"] = accounts_status_filter
    return combined_list, entry_map, p_out


def _handed_to_export_text(entry, doc):
    if not entry:
        return ""
    stored = (entry.handed_to_salesman or "").strip()
    doc_nm = (getattr(doc, "salesman_name", None) or "").strip()
    if stored:
        if doc_nm and stored == doc_nm:
            return "Same as invoiced"
        return stored
    if entry.handed_over_date and doc_nm:
        return "Same as invoiced"
    return ""


def _accounts_recording_export_rows(combined_list, entry_map):
    rows = []
    for doc in combined_list:
        if doc.document_type == "Invoice":
            k = (AccountsRecordingEntry.DocumentKind.INVOICE, doc.invoice_number)
            doc_no = doc.invoice_number
        else:
            k = (AccountsRecordingEntry.DocumentKind.CREDIT_MEMO, doc.credit_memo_number)
            doc_no = doc.credit_memo_number
        entry = entry_map.get(k)
        st = entry.status if entry else AccountsRecordingEntry.Status.PENDING
        rows.append(
            {
                "Type": doc.document_type,
                "Posting Date": doc.posting_date.strftime("%Y-%m-%d") if doc.posting_date else "",
                "Document No.": doc_no,
                "Ctrl No.": (entry.ctrl_number or "") if entry else "",
                "Customer": doc.customer_name or "",
                "Salesman": doc.salesman_name or "",
                "LPO / BP Ref": doc.bp_reference_no or "",
                "Store": doc.store or "",
                "Handed To": _handed_to_export_text(entry, doc),
                "Handed Date": entry.handed_over_date.strftime("%Y-%m-%d")
                if entry and entry.handed_over_date
                else "",
                "Received Back": "Yes" if entry and entry.received_back else "No",
                "Status": _STATUS_LABELS.get(st, st),
                "Accounts Internal Remark": (entry.accounts_internal_remark or "") if entry else "",
            }
        )
    return rows


@login_required
def export_accounts_recording_excel(request):
    _require_accounts_recording_access(request)
    combined_list, entry_map, _p = _build_accounts_recording_export_data(request, request.user)
    data = _accounts_recording_export_rows(combined_list, entry_map)
    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Accounts Recording")

    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    filename = f"accounts_recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def export_accounts_recording_pdf(request):
    """
    Accounts Recording list PDF — same visual theme as Proforma Invoices list
    (logo, company block, orange title bar, meta row, gray header row, rule lines).
    """
    _require_accounts_recording_access(request)
    MAX_ROWS = 2500
    combined_list, entry_map, p = _build_accounts_recording_export_data(request, request.user)
    total_matching = len(combined_list)
    truncated = total_matching > MAX_ROWS
    export_docs = combined_list[:MAX_ROWS] if truncated else combined_list
    row_dicts = _accounts_recording_export_rows(export_docs, entry_map)

    DARK_BLUE = HexColor("#1E3A5F")
    ORANGE = HexColor("#f0ab00")
    LIGHT_GRAY = HexColor("#F5F5F5")
    GRAY_TEXT = HexColor("#808080")
    LIGHT_BLUE_LINE = HexColor("#4A90D9")

    margin_x = 0.4 * inch
    margin_y = 0.45 * inch
    pagesize = landscape(A4)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=margin_y,
        bottomMargin=margin_y,
    )
    available_width = pagesize[0] - 2 * margin_x

    pdf_styles = getSampleStyleSheet()
    company_style = ParagraphStyle(
        "AccRecCompany",
        parent=pdf_styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        textColor=DARK_BLUE,
        leading=11,
    )
    address_style = ParagraphStyle(
        "AccRecAddress",
        parent=pdf_styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        textColor=colors.black,
        leading=9,
    )
    title_style = ParagraphStyle(
        "AccRecTitle",
        parent=pdf_styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        textColor=DARK_BLUE,
    )
    gray_label = ParagraphStyle(
        "AccRecGray",
        parent=pdf_styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        textColor=GRAY_TEXT,
    )
    bold_style = ParagraphStyle(
        "AccRecBold",
        parent=pdf_styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        textColor=colors.black,
    )
    cell_style = ParagraphStyle(
        "AccRecCell",
        parent=pdf_styles["Normal"],
        fontName="Helvetica",
        fontSize=6.5,
        textColor=colors.black,
        leading=8,
    )
    cell_center = ParagraphStyle(
        "AccRecCellCenter",
        parent=cell_style,
        alignment=TA_CENTER,
    )

    generated = datetime.now().strftime("%d.%m.%y %H:%M")

    def on_page(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GRAY_TEXT)
        num = canvas.getPageNumber()
        canvas.drawRightString(pagesize[0] - margin_x, margin_y * 0.5, f"Page {num}")
        if num > 1:
            canvas.setFont("Helvetica-Bold", 9)
            canvas.setFillColor(DARK_BLUE)
            canvas.drawString(
                margin_x,
                pagesize[1] - 0.32 * inch,
                "Junaid Sanitary & Electrical — Accounts Recording",
            )
            canvas.setStrokeColor(LIGHT_BLUE_LINE)
            canvas.setLineWidth(1)
            canvas.line(
                margin_x,
                pagesize[1] - 0.38 * inch,
                pagesize[0] - margin_x,
                pagesize[1] - 0.38 * inch,
            )
        canvas.restoreState()

    elements = []

    logo_path = os.path.join(settings.BASE_DIR, "media", "footer-logo1.png")
    logo_img = None
    if os.path.exists(logo_path):
        try:
            logo_img = Image(logo_path, width=2.0 * inch, height=0.78 * inch)
        except Exception:
            logo_img = None

    left_block = []
    if logo_img:
        left_block.append([logo_img])
    else:
        left_block.append([Paragraph("<b>JUNAID</b>", company_style)])
    left_block.append([Spacer(1, 0.06 * inch)])
    left_block.append(
        [Paragraph("<b>Junaid Sanitary & Electrical Materials Trading (L.L.C)</b>", company_style)]
    )
    left_block.append([Paragraph("Dubai Investment Park-2, Dubai", address_style)])
    left_block.append([Paragraph("04-2367723", address_style)])
    left_block.append([Paragraph("100225006400003", address_style)])

    left_table = Table(left_block, colWidths=[3.2 * inch])
    left_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )

    title_with_bar = Table(
        [["", Paragraph("<b>ACCOUNTS RECORDING — LIST</b>", title_style)]],
        colWidths=[0.1 * inch, 4.2 * inch],
    )
    title_with_bar.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), ORANGE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 0),
                ("LEFTPADDING", (1, 0), (1, 0), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    meta_row = Table(
        [
            [
                Paragraph("Generated", gray_label),
                Paragraph(f"<b>{html_escape(generated)}</b>", bold_style),
                Paragraph("Rows in this PDF", gray_label),
                Paragraph(f"<b>{len(row_dicts)}</b>", bold_style),
                Paragraph("Total matching filters", gray_label),
                Paragraph(f"<b>{total_matching}</b>", bold_style),
            ]
        ],
        colWidths=[0.75 * inch, 1.15 * inch, 1.05 * inch, 0.65 * inch, 1.35 * inch, 0.75 * inch],
    )
    meta_row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    header_top = Table(
        [[left_table, title_with_bar]],
        colWidths=[3.4 * inch, available_width - 3.4 * inch],
    )
    header_top.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    elements.append(header_top)
    elements.append(meta_row)

    store_lbl = (p.get("store_filter") or "All").strip() or "All"
    period_parts = []
    if p.get("start"):
        period_parts.append(f"from {html_escape(str(p['start']))}")
    if p.get("end"):
        period_parts.append(f"to {html_escape(str(p['end']))}")
    period_txt = ", ".join(period_parts) if period_parts else "posting date per filters"
    acc_st = (p.get("accounts_status") or "").strip()
    if acc_st and acc_st in _STATUS_VALUES:
        acc_status_lbl = _STATUS_LABELS.get(acc_st, acc_st)
    else:
        acc_status_lbl = "All"
    elements.append(
        Paragraph(
            f"<i>Store: {html_escape(store_lbl)} · Period: {period_txt} · "
            f"A/c status: {html_escape(acc_status_lbl)}</i>",
            gray_label,
        )
    )

    if truncated:
        elements.append(
            Paragraph(
                f'<font color="#C0392B"><b>Note:</b> Export limited to {MAX_ROWS} rows; '
                "refine filters to narrow results.</font>",
                cell_style,
            )
        )
    elements.append(Spacer(1, 0.12 * inch))

    hdr = [
        Paragraph("<b>Type</b>", bold_style),
        Paragraph("<b>Date</b>", bold_style),
        Paragraph("<b>Document No.</b>", bold_style),
        Paragraph("<b>Ctrl</b>", bold_style),
        Paragraph("<b>Customer</b>", bold_style),
        Paragraph("<b>Salesman</b>", bold_style),
        Paragraph("<b>LPO / BP</b>", bold_style),
        Paragraph("<b>Store</b>", bold_style),
        Paragraph("<b>Handed to</b>", bold_style),
        Paragraph("<b>H. date</b>", bold_style),
        Paragraph("<b>Recv.</b>", bold_style),
        Paragraph("<b>Status</b>", bold_style),
        Paragraph("<b>A/c remark</b>", bold_style),
    ]
    table_rows = [hdr]

    sum_ex_vat = Decimal("0")
    for idx, r in enumerate(row_dicts):
        pd_raw = (r.get("Posting Date") or "").strip()
        date_s = pd_raw
        if pd_raw and len(pd_raw) >= 10:
            try:
                y, m, d = int(pd_raw[0:4]), int(pd_raw[5:7]), int(pd_raw[8:10])
                date_s = f"{d:02d}/{m:02d}/{y}"
            except ValueError:
                pass
        typ = (r.get("Type") or "").strip()
        if typ.lower() == "invoice":
            type_cell = Paragraph(
                '<font backColor="#DBEAFE" color="#1e40af"><b> Inv </b></font>',
                cell_center,
            )
        elif "credit" in typ.lower():
            type_cell = Paragraph(
                '<font backColor="#FEF3C7" color="#92400e"><b> Cr </b></font>',
                cell_center,
            )
        else:
            type_cell = Paragraph(html_escape(typ or "—"), cell_center)

        doc_no = html_escape(str(r.get("Document No.") or ""))
        handed = html_escape(str(r.get("Handed To") or ""))
        remark = html_escape(str(r.get("Accounts Internal Remark") or ""))
        st_label = html_escape(str(r.get("Status") or ""))

        if 0 <= idx < len(export_docs):
            ddoc = export_docs[idx]
            sum_ex_vat += getattr(ddoc, "doc_total_without_vat", None) or Decimal("0")

        table_rows.append(
            [
                type_cell,
                Paragraph(html_escape(date_s), cell_style),
                Paragraph(doc_no, cell_style),
                Paragraph(html_escape(str(r.get("Ctrl No.") or "")), cell_style),
                Paragraph(html_escape(str(r.get("Customer") or "")), cell_style),
                Paragraph(html_escape(str(r.get("Salesman") or "")), cell_style),
                Paragraph(html_escape(str(r.get("LPO / BP Ref") or "")), cell_style),
                Paragraph(html_escape(str(r.get("Store") or "")), cell_center),
                Paragraph(handed, cell_style),
                Paragraph(html_escape(str(r.get("Handed Date") or "")), cell_style),
                Paragraph(html_escape(str(r.get("Received Back") or "")), cell_center),
                Paragraph(st_label, cell_style),
                Paragraph(remark, cell_style),
            ]
        )

    if not row_dicts:
        data_table = Table(
            [[Paragraph("<i>No documents for current filters.</i>", cell_style)]],
            colWidths=[available_width],
        )
        data_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                    ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#CCCCCC")),
                ]
            )
        )
    else:
        col_widths = [
            0.46 * inch,
            0.62 * inch,
            0.92 * inch,
            0.48 * inch,
            1.55 * inch,
            0.78 * inch,
            0.68 * inch,
            0.40 * inch,
            0.82 * inch,
            0.60 * inch,
            0.38 * inch,
            0.58 * inch,
            1.15 * inch,
        ]
        scale = available_width / sum(col_widths)
        col_widths = [w * scale for w in col_widths]

        data_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
        data_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), LIGHT_GRAY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 7),
                    ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBELOW", (0, 0), (-1, 0), 1, HexColor("#CCCCCC")),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.5, HexColor("#EEEEEE")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
    elements.append(data_table)

    if row_dicts:
        elements.append(Spacer(1, 0.14 * inch))
        sum_table = Table(
            [
                [
                    Paragraph(
                        "<b>Total amount in this PDF (ex-VAT, AED)</b>",
                        bold_style,
                    ),
                    Paragraph(
                        f"<b>{sum_ex_vat:,.2f}</b>",
                        ParagraphStyle("AccRecSumRight", parent=bold_style, alignment=TA_RIGHT),
                    ),
                ]
            ],
            colWidths=[available_width - 1.35 * inch, 1.35 * inch],
        )
        sum_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LINEABOVE", (0, 0), (-1, -1), 1, HexColor("#CCCCCC")),
                ]
            )
        )
        elements.append(sum_table)

    doc.build(elements, onFirstPage=on_page, onLaterPages=on_page)

    pdf_bytes = buffer.getvalue()
    buffer.close()
    fname = f"accounts_recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


@login_required
def accounts_recording_list(request):
    _require_accounts_recording_access(request)
    invoice_qs, creditmemo_qs, p = get_combined_ar_filtered_querysets(
        request,
        request.user,
        salesman_scope_q_salesorder,
        default_store_when_unspecified="HO",
        posting_date_start_floor=ACCOUNTS_RECORDING_POSTING_DATE_FLOOR,
    )
    store_filter = p["store_filter"]
    salesmen_filter = p["salesmen_filter"]
    cancel_status_filter = p["cancel_status_filter"]
    document_type_filter = p["document_type_filter"]
    q = p["q"]
    start = p["start"]
    end = p["end"]
    total_range = p["total_range"]
    accounts_status_filter = request.GET.get("accounts_status", "").strip()

    summary = combined_ar_summary_metrics(
        request.user,
        salesman_scope_q_salesorder,
        store_filter,
        salesmen_filter,
        posting_date_floor=ACCOUNTS_RECORDING_POSTING_DATE_FLOOR,
    )

    invoice_qs = invoice_qs.order_by("-posting_date", "-invoice_number")
    creditmemo_qs = creditmemo_qs.order_by("-posting_date", "-credit_memo_number")

    invoice_list = list(invoice_qs)
    creditmemo_list = list(creditmemo_qs)
    for inv in invoice_list:
        setattr(inv, "document_type", "Invoice")
        setattr(inv, "document_number", inv.invoice_number)
    for cm in creditmemo_list:
        setattr(cm, "document_type", "Credit Memo")
        setattr(cm, "document_number", cm.credit_memo_number)

    combined_list = invoice_list + creditmemo_list
    combined_list.sort(
        key=lambda x: (
            x.posting_date if x.posting_date else datetime.min.date(),
            x.document_number if x.document_number else "",
        ),
        reverse=True,
    )

    combined_list = _filter_combined_list_by_accounts_status(
        combined_list, accounts_status_filter
    )
    combined_total_without_vat, combined_total_gp = _totals_from_combined_doc_list(
        combined_list
    )

    try:
        page_size = int(request.GET.get("page_size", 100))
    except ValueError:
        page_size = 20
    page_size = max(5, min(page_size, 100))

    paginator = Paginator(combined_list, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))

    pairs = []
    for doc in page_obj:
        if doc.document_type == "Invoice":
            pairs.append((AccountsRecordingEntry.DocumentKind.INVOICE, doc.invoice_number))
        else:
            pairs.append((AccountsRecordingEntry.DocumentKind.CREDIT_MEMO, doc.credit_memo_number))

    q_obj = Q()
    for dk, dn in pairs:
        q_obj |= Q(document_kind=dk, document_number=dn)
    entry_map = {}
    if pairs:
        for e in AccountsRecordingEntry.objects.filter(q_obj):
            entry_map[(e.document_kind, e.document_number)] = e

    for doc in page_obj:
        if doc.document_type == "Invoice":
            k = (AccountsRecordingEntry.DocumentKind.INVOICE, doc.invoice_number)
        else:
            k = (AccountsRecordingEntry.DocumentKind.CREDIT_MEMO, doc.credit_memo_number)
        ent = entry_map.get(k)
        setattr(doc, "accounts_entry", ent)
        setattr(doc, "accounts_handed_select_value", _handed_select_value(ent, getattr(doc, "salesman_name", None)))

    all_salesmen = combined_ar_salesmen_list(request.user, salesman_scope_q_salesorder)
    salesmen_ho = combined_ar_ho_salesmen_list(request.user, salesman_scope_q_salesorder)
    is_admin = request.user.is_superuser or request.user.is_staff or (
        hasattr(request.user, "role") and request.user.role.role == "Admin"
    )

    return render(
        request,
        "salesorders/accounts_recording_list.html",
        {
            "page_obj": page_obj,
            "total_count": paginator.count,
            "total_without_vat_value": combined_total_without_vat,
            "total_gross_profit_value": combined_total_gp,
            "salesmen": all_salesmen,
            "salesmen_ho": salesmen_ho,
            "today_sales": summary["today_sales"],
            "today_gp": summary["today_gp"],
            "month_sales": summary["month_sales"],
            "month_gp": summary["month_gp"],
            "year_sales": summary["year_sales"],
            "year_gp": summary["year_gp"],
            "is_admin": is_admin,
            "status_choices": AccountsRecordingEntry.Status.choices,
            "acc_handover_today": date.today().isoformat(),
            "filters": {
                "q": q,
                "salesmen_filter": salesmen_filter,
                "cancel_status": cancel_status_filter or "All",
                "store": store_filter,
                "start": start,
                "end": end,
                "page_size": page_size,
                "total": total_range,
                "document_type": document_type_filter or "All",
                "accounts_status": accounts_status_filter,
            },
        },
    )


@login_required
def accounts_recording_activity_log(request):
    """Who changed what on Accounts Recording (same access as the main list)."""
    _require_accounts_recording_access(request)
    try:
        page_size = int(request.GET.get("page_size", 50))
    except ValueError:
        page_size = 50
    page_size = max(10, min(page_size, 200))
    q = request.GET.get("q", "").strip()

    qs = AccountsRecordingChangeLog.objects.all()
    if q:
        qs = qs.filter(
            Q(document_number__icontains=q) | Q(username__icontains=q)
        )

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))

    for log in page_obj:
        lines = []
        changes = log.changes if isinstance(log.changes, dict) else {}
        for field, pair in changes.items():
            label = _ACC_LOG_FIELD_LABELS.get(
                field, field.replace("_", " ").title()
            )
            if isinstance(pair, dict):
                old_v = pair.get("old", "")
                new_v = pair.get("new", "")
            else:
                old_v, new_v = "", ""
            lines.append({"label": label, "old": old_v, "new": new_v})
        log.change_lines = lines

    return render(
        request,
        "salesorders/accounts_recording_activity.html",
        {
            "page_obj": page_obj,
            "total_count": paginator.count,
            "filters": {"q": q, "page_size": page_size},
        },
    )


@login_required
@require_POST
def accounts_recording_save(request):
    _require_accounts_recording_access(request)
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    document_kind = (data.get("document_kind") or "").strip()
    document_number = (data.get("document_number") or "").strip()
    if document_kind not in (
        AccountsRecordingEntry.DocumentKind.INVOICE,
        AccountsRecordingEntry.DocumentKind.CREDIT_MEMO,
    ) or not document_number:
        return JsonResponse({"success": False, "error": "document_kind and document_number required"}, status=400)

    doc = _get_scoped_document(request.user, document_kind, document_number)
    if not doc:
        return JsonResponse({"success": False, "error": "Document not found"}, status=404)

    existing = AccountsRecordingEntry.objects.filter(
        document_kind=document_kind, document_number=document_number
    ).first()

    ctrl_number = existing.ctrl_number if existing else None
    handed_to_salesman = existing.handed_to_salesman if existing else None
    handed_over_date = existing.handed_over_date if existing else None
    received_back = existing.received_back if existing else False
    status_val = existing.status if existing else AccountsRecordingEntry.Status.PENDING
    accounts_internal_remark = (
        existing.accounts_internal_remark if existing else None
    )

    if "accounts_internal_remark" in data:
        v = data.get("accounts_internal_remark")
        if v is None:
            accounts_internal_remark = None
        else:
            s = str(v).strip()
            accounts_internal_remark = s if s else None

    if "ctrl_number" in data:
        v = data.get("ctrl_number")
        ctrl_number = (str(v).strip() if v is not None else "") or None

    if "handed_to_salesman" in data:
        v = data.get("handed_to_salesman")
        vs = str(v).strip() if v is not None else ""
        if not vs:
            handed_to_salesman = None
        elif vs == HANDED_SAME_AS_DOC:
            handed_to_salesman = (getattr(doc, "salesman_name", None) or "").strip() or None
        else:
            handed_to_salesman = vs

    if "handed_over_date" in data:
        handed_over_date = _parse_date(data.get("handed_over_date"))

    if "received_back" in data:
        received_back = bool(data.get("received_back"))

    manual_status = bool(data.get("manual_status"))
    incoming_status = (data.get("status") or "").strip() or None
    if incoming_status and incoming_status not in _STATUS_VALUES:
        return JsonResponse({"success": False, "error": "Invalid status"}, status=400)

    status_val = _resolve_final_status(
        existing,
        handed_to_salesman,
        handed_over_date,
        received_back,
        manual_status,
        incoming_status,
    )

    entry, _ = AccountsRecordingEntry.objects.update_or_create(
        document_kind=document_kind,
        document_number=document_number,
        defaults={
            "ctrl_number": ctrl_number,
            "handed_to_salesman": handed_to_salesman,
            "handed_over_date": handed_over_date,
            "received_back": received_back,
            "status": status_val,
            "accounts_internal_remark": accounts_internal_remark,
        },
    )

    _append_accounts_recording_change_log(
        request, document_kind, document_number, existing, entry
    )

    return JsonResponse({"success": True, "row": _serialize_entry(entry, doc)})


"""
Telegram remarks: send Management Remarks to salesman Telegram groups.
Salesman names map to chat IDs; multiple names can share one chat.
Uses TELEGRAM_MD_APPROVAL_BOT_TOKEN from .env.
"""
import re
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from .utils import send_telegram_message, send_telegram_document


# Multiple salesman names can map to the same chat ID
SALESMAN_TELEGRAM_GROUPS = {
    "TESTING": "-5266252930",
    #RAFIQ
    "A.MR.RAFIQ": "-5206945591",
    "A.MR.RAFIQ NE": "-5206945591",
    "A. RAFIQ SHABBIR - RASHID": "-5195382862",

    #ANISH
    "B.ANISH DIP": "-5231217364",

    #ABU BAQAR
    "A.MR.RAFIQ ABU-TRD": "-5195382862",
    "B. MR.RAFIQ ABU- PROJ": "-5195382862",
    "A. RAFIQ ABU - RASHID": "-5195382862",
    "A.RAFIQ ABU - RASHID": "-5195382862",  # normalized form
    


    #SIYAB
    "A.MR.SIYAB": "-5289303049",
    "A.MR.SIYAB CONT": "-5289303049",

    #MUZAIN
    "B.MR.MUZAIN": "-5011292246",

    #MUZAMMIL
    "A.DIP MUZAMMIL": "-5020536884",


}


def _normalize_salesman_name(name):
    """Normalize salesman name for matching."""
    if not name:
        return ""
    normalized = str(name).upper().strip()
    normalized = re.sub(r'\.\s+', '.', normalized)
    normalized = re.sub(r'\s+\.', '.', normalized)
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def get_chat_id_for_salesman(salesman_name):
    """Return chat_id if salesman is in mapping, else None."""
    if not salesman_name:
        return None
    key = _normalize_salesman_name(salesman_name)
    # Direct lookup first
    if key in SALESMAN_TELEGRAM_GROUPS:
        return SALESMAN_TELEGRAM_GROUPS[key]
    # Fallback: match against normalized dict keys (handles "A. RAFIQ" vs "A.RAFIQ")
    for dict_key, chat_id in SALESMAN_TELEGRAM_GROUPS.items():
        if _normalize_salesman_name(dict_key) == key:
            return chat_id
    return None


def can_send_remarks_telegram(salesorder):
    """Return True if the SO's salesman has a Telegram chat ID."""
    return get_chat_id_for_salesman(salesorder.salesman_name) is not None


def _get_approval_emoji(status):
    """Return emoji based on approval status."""
    status_map = {
        "approved": "✅",
        "rejected": "❌",
        "pending": "⏳",
        "on_hold": "⏸",
        "review": "🔍",
        "scheduled": "📅",
    }
    if not status:
        return "📋"
    return status_map.get(str(status).lower().strip(), "📋")


def _get_status_label(status):
    """Return human-readable status label."""
    status_map = {
        "approved": "Approved",
        "rejected": "Rejected",
        "pending": "Pending Review",
        "on_hold": "On Hold",
        "review": "Under Review",
    }
    if not status:
        return "—"
    return status_map.get(str(status).lower().strip(), str(status))


def _format_currency(value):
    """Format number as AED currency string."""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f} AED"
    except (ValueError, TypeError):
        return "—"


def _escape_html(text):
    """Escape HTML special characters for Telegram HTML parse mode."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_remarks_to_salesman_telegram(salesorder, remark_text):
    """
    Send a well-formatted SO remark notification to the salesman's Telegram group.
    Returns (success: bool, error: str | None).
    """
    chat_id = get_chat_id_for_salesman(salesorder.salesman_name)
    if not chat_id:
        return False, "No Telegram group for this salesman"

    # ── Gather data ──
    so_num = salesorder.so_number or "—"
    customer = _escape_html(salesorder.customer_name or "—")
    customer_code = _escape_html(getattr(salesorder, 'customer_code', '') or "")
    salesman = _escape_html(salesorder.salesman_name or "—")
    doc_total = _format_currency(getattr(salesorder, 'document_total', None))
    remarks = _escape_html((remark_text or "").strip())
    approval_status = getattr(salesorder, 'approval_status', None)
    approval_emoji = _get_approval_emoji(approval_status)
    approval_label = _get_status_label(approval_status)
    posting_date = getattr(salesorder, 'posting_date', None)
    bp_ref = _escape_html(getattr(salesorder, 'bp_reference_no', '') or "")

    # Timestamp in UAE time
    try:
        now_uae = timezone.now().astimezone(timezone.pytz.timezone("Asia/Dubai"))
        timestamp = now_uae.strftime("%d %b %Y, %I:%M %p UAE")
    except Exception:
        timestamp = timezone.now().strftime("%d %b %Y, %I:%M %p")

    # ── Build message ──
    lines = [
        f"{approval_emoji} <b>Management Remark Update</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📄 <b>SO:</b>  <code>{so_num}</code>",
        f"🏢 <b>Customer:</b>  {customer}",
    ]

    if customer_code:
        lines.append(f"🔖 <b>Code:</b>  <code>{customer_code}</code>")

    lines.append(f"👤 <b>Salesman:</b>  {salesman}")

    if posting_date:
        date_str = posting_date.strftime("%d %b %Y") if hasattr(posting_date, 'strftime') else str(posting_date)
        lines.append(f"📅 <b>Date:</b>  {date_str}")

    if bp_ref:
        lines.append(f"🔗 <b>BP Ref:</b>  {bp_ref}")

    lines.append(f"💰 <b>Total (excl. VAT):</b>  <b>{doc_total}</b>")

    if approval_status:
        lines.append(f"📌 <b>Status:</b>  {approval_emoji} {approval_label}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")

    if remarks:
        lines.append("💬 <b>Remarks:</b>")
        lines.append("")
        lines.append(f"<blockquote>{remarks}</blockquote>")
    else:
        lines.append("💬 <i>No remarks provided</i>")

    lines.append("")
    lines.append(f"🕐 <i>{timestamp}</i>")

    msg = "\n".join(lines)

    # ── Send ──
    token = getattr(settings, 'TELEGRAM_MD_APPROVAL_BOT_TOKEN', '') or settings.TELEGRAM_BOT_TOKEN
    ok, err = send_telegram_message(chat_id, msg, parse_mode="HTML", token=token)

    if ok:
        return True, None
    return False, err or "Unknown error"


def send_remarks_with_pdf_to_salesman_telegram(salesorder, remark_text):
    """
    Send Management Remarks + SO PDF (same design as Export) to salesman's Telegram group.
    Uses generate_sap_salesorder_pdf_bytes for the PDF.
    Returns (success: bool, error: str | None).
    """
    chat_id = get_chat_id_for_salesman(salesorder.salesman_name)
    if not chat_id:
        return False, "No Telegram group for this salesman"

    # ── Gather data ──
    so_num = salesorder.so_number or "—"
    so_num_escaped = _escape_html(so_num)
    customer = _escape_html(salesorder.customer_name or "—")
    customer_code = _escape_html(getattr(salesorder, 'customer_code', '') or "")
    salesman = _escape_html(salesorder.salesman_name or "—")
    doc_total = _format_currency(getattr(salesorder, 'document_total', None))
    remarks = _escape_html((remark_text or "").strip())
    approval_status = getattr(salesorder, 'approval_status', None)
    approval_emoji = _get_approval_emoji(approval_status)
    approval_label = _get_status_label(approval_status)
    posting_date = getattr(salesorder, 'posting_date', None)
    bp_ref = _escape_html(getattr(salesorder, 'bp_reference_no', '') or "")

    # Timestamp in UAE time
    try:
        now_uae = timezone.now().astimezone(timezone.pytz.timezone("Asia/Dubai"))
        timestamp = now_uae.strftime("%d %b %Y, %I:%M %p UAE")
    except Exception:
        timestamp = timezone.now().strftime("%d %b %Y, %I:%M %p")

    # ── Build caption (Telegram document caption limit: 1024 chars) ──
    lines = [
        f"{approval_emoji} <b>Management Remark Update</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📄 <b>SO:</b>  <code>{so_num_escaped}</code>",
        f"🏢 <b>Customer:</b>  {customer}",
    ]

    if customer_code:
        lines.append(f"🔖 <b>Code:</b>  <code>{customer_code}</code>")

    lines.append(f"👤 <b>Salesman:</b>  {salesman}")

    if posting_date:
        date_str = posting_date.strftime("%d %b %Y") if hasattr(posting_date, 'strftime') else str(posting_date)
        lines.append(f"📅 <b>Date:</b>  {date_str}")

    if bp_ref:
        lines.append(f"🔗 <b>BP Ref:</b>  {bp_ref}")

    lines.append(f"💰 <b>Total (excl. VAT):</b>  <b>{doc_total}</b>")

    if approval_status:
        lines.append(f"📌 <b>Status:</b>  {approval_emoji} {approval_label}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")

    if remarks:
        lines.append("💬 <b>Remarks:</b>")
        lines.append("")
        lines.append(f"<blockquote>{remarks}</blockquote>")
    else:
        lines.append("💬 <i>No remarks provided</i>")

    lines.append("")
    lines.append(f"🕐 <i>{timestamp}</i>")
    lines.append("📎 <i>PDF attached below</i>")

    caption = "\n".join(lines)

    # ── Trim caption if exceeds Telegram's 1024 char limit ──
    if len(caption) > 1024:
        # Rebuild a shorter version without optional fields
        short_lines = [
            f"{approval_emoji} <b>Management Remark Update</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"📄 <b>SO:</b>  <code>{so_num_escaped}</code>",
            f"🏢 <b>Customer:</b>  {customer}",
            f"💰 <b>Total (excl. VAT):</b>  <b>{doc_total}</b>",
        ]

        if approval_status:
            short_lines.append(f"📌 {approval_emoji} {approval_label}")

        short_lines.append("━━━━━━━━━━━━━━━━━━━━━")

        if remarks:
            max_remark_len = 600
            truncated = remarks[:max_remark_len] + ("…" if len(remarks) > max_remark_len else "")
            short_lines.append(f"💬 <b>Remarks:</b>\n{truncated}")
        else:
            short_lines.append("💬 <i>No remarks</i>")

        short_lines.append(f"\n🕐 <i>{timestamp}</i>")
        short_lines.append("📎 <i>PDF attached</i>")

        caption = "\n".join(short_lines)

    # ── Generate PDF (same design as Export) ──
    from .sap_salesorder_pdf import generate_sap_salesorder_pdf_bytes

    try:
        pdf_bytes = generate_sap_salesorder_pdf_bytes(salesorder)
    except Exception as e:
        return False, f"PDF generation failed: {str(e)}"

    if not pdf_bytes:
        return False, "PDF generation returned empty content"

    date_str = salesorder.posting_date.strftime("%Y%m%d") if salesorder.posting_date else "NA"
    filename = f"SO_{so_num}_{date_str}.pdf"

    # ── Send document ──
    token = getattr(settings, 'TELEGRAM_MD_APPROVAL_BOT_TOKEN', '') or settings.TELEGRAM_BOT_TOKEN
    ok, err = send_telegram_document(
        chat_id,
        pdf_bytes,
        filename,
        caption=caption,
        parse_mode="HTML",
        token=token,
    )

    if ok:
        return True, None
    return False, err or "Unknown error"

def send_approval_status_change_telegram(salesorder, old_status, new_status, changed_by=None):
    """
    Send approval status change notification to salesman's Telegram group.
    Returns (success: bool, error: str | None).
    """
    chat_id = get_chat_id_for_salesman(salesorder.salesman_name)
    if not chat_id:
        return False, "No Telegram group for this salesman"

    so_num = _escape_html(salesorder.so_number or "—")
    customer = _escape_html(salesorder.customer_name or "—")
    doc_total = _format_currency(getattr(salesorder, 'document_total', None))
    new_emoji = _get_approval_emoji(new_status)
    old_label = _get_status_label(old_status)
    new_label = _get_status_label(new_status)

    changed_by_str = ""
    if changed_by:
        name = getattr(changed_by, 'get_full_name', lambda: '')()
        if not name:
            name = getattr(changed_by, 'username', 'Unknown')
        changed_by_str = _escape_html(name)

    try:
        now_uae = timezone.now().astimezone(timezone.pytz.timezone("Asia/Dubai"))
        timestamp = now_uae.strftime("%d %b %Y, %I:%M %p UAE")
    except Exception:
        timestamp = timezone.now().strftime("%d %b %Y, %I:%M %p")

    lines = [
        f"{new_emoji} <b>Approval Status Changed</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📄 <b>SO:</b>  <code>{so_num}</code>",
        f"🏢 <b>Customer:</b>  {customer}",
        f"💰 <b>Total (excl. VAT):</b>  <b>{doc_total}</b>",
        "",
        f"🔄 <b>Status:</b>  {old_label}  ➜  <b>{new_label}</b> {new_emoji}",
    ]

    if changed_by_str:
        lines.append(f"👤 <b>Changed by:</b>  {changed_by_str}")

    remarks_raw = (getattr(salesorder, "remarks", None) or "").strip()
    if remarks_raw:
        remarks_esc = _escape_html(remarks_raw)
        if len(remarks_esc) > 3500:
            remarks_esc = remarks_esc[:3500] + "…"
        lines.append("")
        lines.append("💬 <b>Management remarks:</b>")
        lines.append("")
        lines.append(f"<blockquote>{remarks_esc}</blockquote>")

    lines.append("")
    lines.append(f"🕐 <i>{timestamp}</i>")

    msg = "\n".join(lines)

    token = getattr(settings, 'TELEGRAM_MD_APPROVAL_BOT_TOKEN', '') or settings.TELEGRAM_BOT_TOKEN
    ok, err = send_telegram_message(chat_id, msg, parse_mode="HTML", token=token)

    if ok:
        return True, None
    return False, err or "Unknown error"
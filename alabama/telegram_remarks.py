"""
Telegram notifications for Alabama portal sales orders (management remarks).

WHERE TO CONFIGURE SALESMAN → TELEGRAM CHAT
────────────────────────────────────────────
Edit the dict below in THIS file:

    ALABAMA_SALESMAN_TELEGRAM_GROUPS

  • Keys: salesman identifiers as they appear after Alabama **Settings** name
    mappings (canonical names work best), e.g. \"KADER\", \"MUSHARAF\".
  • Values: Telegram **group** chat id (string), e.g. \"-1001234567890\".

Names from Excel are normalized with `normalize_alabama_salesman()` then matched
with the same rules as Junaid SO (`_normalize_salesman_name`).

Bots / tokens (same as Junaid SO remarks):
  • `TELEGRAM_MD_APPROVAL_BOT_TOKEN` in `.env` if set, else `TELEGRAM_BOT_TOKEN`.
  • The bot must be a member of each group you post to.
"""
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from so.telegram_remarks import _normalize_salesman_name
from so.utils import send_telegram_message

from .views import normalize_alabama_salesman

# ── Add Alabama salesman → Telegram group chat id here ─────────────────────
ALABAMA_SALESMAN_TELEGRAM_GROUPS = {
    # Example (replace with real group ids):
    "KADER": "-5118018577",
    "MUSHARAF": "-5118018577",
    "AIJAZ": "-5118018577",
}


def _escape_html(text):
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_currency(value):
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f} AED"
    except (ValueError, TypeError):
        return "—"


def get_alabama_chat_id_for_salesman(salesman_name):
    """Resolve Telegram chat id for Alabama SO salesman."""
    if not salesman_name:
        return None
    canonical = normalize_alabama_salesman(salesman_name) or salesman_name
    key = _normalize_salesman_name(canonical)
    if key in ALABAMA_SALESMAN_TELEGRAM_GROUPS:
        return ALABAMA_SALESMAN_TELEGRAM_GROUPS[key]
    for dict_key, chat_id in ALABAMA_SALESMAN_TELEGRAM_GROUPS.items():
        if _normalize_salesman_name(dict_key) == key:
            return chat_id
    key_raw = _normalize_salesman_name(salesman_name)
    if key_raw in ALABAMA_SALESMAN_TELEGRAM_GROUPS:
        return ALABAMA_SALESMAN_TELEGRAM_GROUPS[key_raw]
    for dict_key, chat_id in ALABAMA_SALESMAN_TELEGRAM_GROUPS.items():
        if _normalize_salesman_name(dict_key) == key_raw:
            return chat_id
    return None


def can_send_alabama_remarks_telegram(salesorder):
    return get_alabama_chat_id_for_salesman(salesorder.salesman_name) is not None


def send_alabama_remarks_to_salesman_telegram(salesorder, remark_text):
    """
    Send management remark to the salesman's Alabama Telegram group.
    Returns (success: bool, error: str | None).
    """
    chat_id = get_alabama_chat_id_for_salesman(salesorder.salesman_name)
    if not chat_id:
        return False, (
            "No Telegram group for this salesman. Add a mapping in "
            "alabama/telegram_remarks.py → ALABAMA_SALESMAN_TELEGRAM_GROUPS."
        )

    so_num = salesorder.so_number or "—"
    customer = _escape_html(salesorder.customer_name or "—")
    customer_code = _escape_html(getattr(salesorder, "customer_code", "") or "")
    salesman = _escape_html(salesorder.salesman_name or "—")
    doc_total = _format_currency(getattr(salesorder, "document_total", None))
    remarks = _escape_html((remark_text or "").strip())
    posting_date = getattr(salesorder, "posting_date", None)
    bp_ref = _escape_html(getattr(salesorder, "bp_reference_no", "") or "")

    try:
        now_uae = timezone.now().astimezone(ZoneInfo("Asia/Dubai"))
        timestamp = now_uae.strftime("%d %b %Y, %I:%M %p UAE")
    except Exception:
        timestamp = timezone.now().strftime("%d %b %Y, %I:%M %p")

    lines = [
        "🏷 <b>Alabama · Management Remark</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📄 <b>SO:</b>  <code>{so_num}</code>",
        f"🏢 <b>Customer:</b>  {customer}",
    ]
    if customer_code:
        lines.append(f"🔖 <b>Code:</b>  <code>{customer_code}</code>")
    lines.append(f"👤 <b>Salesman:</b>  {salesman}")
    if posting_date:
        date_str = posting_date.strftime("%d %b %Y") if hasattr(posting_date, "strftime") else str(posting_date)
        lines.append(f"📅 <b>Date:</b>  {date_str}")
    if bp_ref:
        lines.append(f"🔗 <b>BP Ref:</b>  {bp_ref}")
    lines.append(f"💰 <b>Document total:</b>  <b>{doc_total}</b>")
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
    token = getattr(settings, "TELEGRAM_MD_APPROVAL_BOT_TOKEN", "") or settings.TELEGRAM_BOT_TOKEN
    ok, err = send_telegram_message(chat_id, msg, parse_mode="HTML", token=token)
    if ok:
        return True, None
    return False, err or "Unknown error"

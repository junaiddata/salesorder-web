"""
PO Updates: new list/detail pages over SAP open purchase orders where logged-in
users record goods received (multiple deliveries per line, with history).

Balance = SAP Open Qty - receipts SAP has not absorbed yet ("pending in app").
SAP delivered = Quantity - RemainingOpenQuantity. A baseline of that value is saved when
a line's first receipt is entered (POLineBaseline); any growth since then means SAP has
posted the goods receipt, so that much of the app's receipts is no longer subtracted again.
The legacy PO pages (sap_purchaseorder_views) are not modified; only their
read-only helpers are reused.
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum, Max, Value, DecimalField, Exists, OuterRef
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import SAPPurchaseOrder, SAPPurchaseOrderItem, Items, POReceipt, POReceiptLog, POLineBaseline
from .sap_purchaseorder_views import _purchaseorder_items_qs, _open_row_status_q_po

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


# ---------------------------------------------------------------- helpers

def _user_can_see_price(request) -> bool:
    """Amounts (prices, totals) are for admins only; a Salesman role never sees them, even if staff."""
    user = request.user
    role = getattr(getattr(user, "role", None), "role", None)
    if role == "Salesman":
        return False
    return bool(user.is_staff or user.is_superuser or role == "Admin")


def _open_qty(item) -> Decimal:
    """Open qty of a PO line: SAP remaining_open_quantity, else ordered quantity."""
    val = item.remaining_open_quantity
    if val is None:
        val = item.quantity
    return val or ZERO


def _sap_delivered(item) -> Decimal:
    """What SAP says has been delivered on the line: Quantity - RemainingOpenQuantity."""
    rem = item.remaining_open_quantity
    if rem is None:
        return ZERO
    return max(ZERO, (item.quantity or ZERO) - rem)


def _absorbed(item, d0) -> Decimal:
    """Delivered qty SAP has recorded since the baseline (0 when there is no baseline yet)."""
    if d0 is None:
        return ZERO
    return max(ZERO, _sap_delivered(item) - d0)


def _baselines(po_numbers):
    rows = POLineBaseline.objects.filter(po_number__in=set(po_numbers)).values_list(
        "po_number", "line_no", "sap_delivered_at_start")
    return {(p, l): d for p, l, d in rows}


def _line_calc(item, received, d0):
    """(open_qty, absorbed, pending, balance) for a line given total received in the app."""
    open_qty = _open_qty(item)
    absorbed = _absorbed(item, d0)
    pending = max(ZERO, received - absorbed)
    return open_qty, absorbed, pending, open_qty - pending


def _last_synced():
    """Latest PO creation time = last full sync (each sync recreates every PO)."""
    return SAPPurchaseOrder.objects.aggregate(t=Max("created_at"))["t"]


def _is_open(item) -> bool:
    return (item.row_status or "").strip().upper() in ("O", "OPEN")


def _received_totals(po_numbers):
    """{(po_number, line_no): total received} for the given PO numbers."""
    rows = (
        POReceipt.objects.filter(po_number__in=set(po_numbers))
        .values("po_number", "line_no")
        .annotate(total=Sum("qty_received"))
    )
    return {(r["po_number"], r["line_no"]): r["total"] or ZERO for r in rows}


def _line_total(po_number, line_no, exclude_id=None) -> Decimal:
    qs = POReceipt.objects.filter(po_number=po_number, line_no=line_no)
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs.aggregate(t=Coalesce(Sum("qty_received"), Value(0, output_field=DecimalField())))["t"] or ZERO


def _parse_date(s):
    if not s:
        return None
    try:
        if len(s) == 7:
            return datetime.strptime(s + "-01", "%Y-%m-%d").date()
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _user_label(user):
    if not user:
        return "—"
    return (user.get_full_name() or user.username)


def _receipt_dict(r):
    return {
        "id": r.id,
        "qty": float(r.qty_received),
        "date": r.received_date.isoformat(),
        "date_display": r.received_date.strftime("%d.%m.%y"),
        "remarks": r.remarks,
        "user": _user_label(r.received_by),
        "created_at": r.updated_at.strftime("%d.%m.%y %H:%M"),  # last updated (equals entry time until edited)
    }


def _apply_list_filters(request):
    """Same filters as the legacy open-PO list; returns (queryset, filters dict)."""
    qs = _purchaseorder_items_qs()
    g = request.GET
    q = g.get("q", "").strip()
    item_filter = g.get("item", "").strip()
    firm_filter = g.getlist("firm")
    purchaser_filter = g.getlist("purchaser")
    start = g.get("start", "").strip()
    end = g.get("end", "").strip()

    clean_firms = [f.strip() for f in firm_filter if f and f.strip()]
    if clean_firms:
        qs = qs.filter(Exists(Items.objects.filter(item_code=OuterRef("item_no"), item_firm__in=clean_firms)))

    clean_p = [p for p in purchaser_filter if p.strip()]
    if clean_p:
        qs = qs.filter(purchaseorder__salesman_name__in=clean_p)

    if item_filter:
        qs = qs.filter(Q(item_no__icontains=item_filter) | Q(description__icontains=item_filter))

    if q:
        if q.isdigit():
            qs = qs.filter(
                Q(purchaseorder__po_number__istartswith=q) | Q(item_no__istartswith=q) | Q(item_no__icontains=q)
            )
        elif len(q) < 3:
            qs = qs.filter(
                Q(purchaseorder__supplier_name__istartswith=q)
                | Q(purchaseorder__salesman_name__istartswith=q)
                | Q(purchaseorder__supplier_code__istartswith=q)
                | Q(purchaseorder__bp_reference_no__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(purchaseorder__po_number__icontains=q)
                | Q(purchaseorder__supplier_name__icontains=q)
                | Q(purchaseorder__salesman_name__icontains=q)
                | Q(purchaseorder__bp_reference_no__icontains=q)
                | Q(purchaseorder__supplier_code__icontains=q)
                | Q(item_no__icontains=q)
                | Q(description__icontains=q)
            )

    start_date, end_date = _parse_date(start), _parse_date(end)
    if start_date:
        qs = qs.filter(purchaseorder__posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(purchaseorder__posting_date__lte=end_date)

    qs = qs.order_by("-purchaseorder__posting_date", "-purchaseorder__po_number", "line_no")
    filters = {
        "q": q, "item": item_filter, "firm_filter": firm_filter, "purchaser_filter": purchaser_filter,
        "start": start, "end": end,
    }
    return qs, filters


def _paginate(request, qs):
    try:
        page_size = int(request.GET.get("page_size", 100))
    except ValueError:
        page_size = 100
    page_size = max(5, min(page_size, 200))
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    po_numbers = [i.purchaseorder.po_number for i in page_obj]
    received = _received_totals(po_numbers)
    bases = _baselines(po_numbers)
    for it in page_obj:
        key = (it.purchaseorder.po_number, it.line_no)
        it.received_total = received.get(key, ZERO)
        it.open_qty, it.absorbed, it.pending_app, it.balance = _line_calc(it, it.received_total, bases.get(key))
    return paginator, page_obj, page_size


# ---------------------------------------------------------------- list

@login_required
def po_receiving_list(request):
    qs, filters = _apply_list_filters(request)
    paginator, page_obj, page_size = _paginate(request, qs)
    filters["page_size"] = page_size

    firms = list(
        Items.objects.exclude(item_firm__isnull=True).exclude(item_firm="")
        .values_list("item_firm", flat=True).distinct().order_by("item_firm")
    )
    purchasers = [
        p for p in _purchaseorder_items_qs()
        .values_list("purchaseorder__salesman_name", flat=True).distinct().order_by("purchaseorder__salesman_name") if p
    ]
    return render(request, "po_receiving/po_receiving_list.html", {
        "page_obj": page_obj,
        "total_count": paginator.count,
        "firms": firms,
        "purchasers": purchasers,
        "show_price": _user_can_see_price(request),
        "filters": filters,
        "last_synced": _last_synced(),
    })


@login_required
def po_receiving_search(request):
    qs, _ = _apply_list_filters(request)
    paginator, page_obj, _ = _paginate(request, qs)
    show_price = _user_can_see_price(request)
    return JsonResponse({
        "rows_html": render_to_string("po_receiving/_po_receiving_rows.html",
                                      {"page_obj": page_obj, "show_price": show_price}, request=request),
        "pagination_html": render_to_string("purchaseorders/_pagination.html", {"page_obj": page_obj}, request=request),
        "count": paginator.count,
    })


# ---------------------------------------------------------------- detail

@login_required
def po_receiving_detail(request, po_number):
    po = get_object_or_404(SAPPurchaseOrder, po_number=po_number)
    items = list(po.items.all().order_by("line_no", "id"))

    receipts_by_line = defaultdict(list)
    for r in POReceipt.objects.filter(po_number=po_number).select_related("received_by"):
        receipts_by_line[r.line_no].append(r)

    bases = _baselines([po_number])
    for it in items:
        rs = receipts_by_line.get(it.line_no, [])
        it.can_receive = _is_open(it)
        it.received_total = sum((r.qty_received for r in rs), ZERO)
        if it.can_receive:
            it.open_qty, it.absorbed, it.pending_app, it.balance = _line_calc(
                it, it.received_total, bases.get((po_number, it.line_no)))
        else:
            it.open_qty = it.absorbed = it.pending_app = it.balance = ZERO
        it.receipts = rs
        it.last_receipt = rs[-1] if rs else None
        it.receipts_json = json.dumps([_receipt_dict(r) for r in rs])
        qty = it.quantity or ZERO
        it.unit_price = (it.row_total / qty).quantize(Decimal("0.01")) if qty and it.row_total else Decimal("0.00")

    any_open = any(_is_open(i) for i in items)
    pending_total = sum((i.pending_amount or ZERO for i in items), ZERO)

    subtotal = po.row_total_sum or ZERO
    discount_percentage = po.discount_percentage or Decimal("0.00")
    discount_amount = (subtotal * discount_percentage / 100).quantize(Decimal("0.01")) if subtotal else Decimal("0.00")
    total_before_tax = (subtotal - discount_amount).quantize(Decimal("0.01")) if subtotal else Decimal("0.00")
    vat_sum = po.vat_sum or Decimal("0.00")

    return render(request, "po_receiving/po_receiving_detail.html", {
        "purchaseorder": po,
        "items": items,
        "status_label": "Open" if any_open else "Closed",
        "pending_total": pending_total,
        "subtotal": subtotal,
        "discount_percentage": round(float(discount_percentage), 1),
        "discount_amount": discount_amount,
        "total_before_tax": total_before_tax,
        "vat_sum": vat_sum,
        "grand_total": (total_before_tax + vat_sum).quantize(Decimal("0.01")),
        "show_price": _user_can_see_price(request),
        "today": date.today().isoformat(),
        "last_synced": _last_synced(),
    })


# ---------------------------------------------------------------- receipt actions

def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except Exception:
        return request.POST.dict()


def _validate_fields(data):
    """Return (qty, date, remarks, error)."""
    try:
        qty = Decimal(str(data.get("qty_received", "")).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None, None, None, "Enter a valid quantity."
    if qty <= 0:
        return None, None, None, "Quantity must be greater than zero."
    rdate = _parse_date(str(data.get("received_date", "")).strip())
    if not rdate or len(str(data.get("received_date", "")).strip()) != 10:
        return None, None, None, "Enter a valid date."
    remarks = (data.get("remarks") or "").strip()
    if len(remarks) > 1000:
        return None, None, None, "Remarks are too long (max 1000 characters)."
    return qty, rdate, remarks, None


def _get_open_line(po_number, line_no):
    return (
        SAPPurchaseOrderItem.objects.filter(purchaseorder__po_number=po_number, line_no=line_no)
        .filter(_open_row_status_q_po()).first()
    )


def _line_response(po_number, line_no, item):
    total = _line_total(po_number, line_no)
    d0 = POLineBaseline.objects.filter(po_number=po_number, line_no=line_no).values_list(
        "sap_delivered_at_start", flat=True).first()
    open_qty, absorbed, pending, balance = _line_calc(item, total, d0)
    receipts = POReceipt.objects.filter(po_number=po_number, line_no=line_no).select_related("received_by")
    return {
        "success": True,
        "open_qty": float(open_qty),
        "absorbed": float(absorbed),
        "received_total": float(total),
        "pending": float(pending),
        "balance": float(balance),
        "receipts": [_receipt_dict(r) for r in receipts],
    }


def _lock_line(po_number, line_no, item):
    """
    Serialise writes per PO line and return the baseline (SAP delivered at start).
    Must be called inside transaction.atomic(). The baseline row doubles as the lock.
    When the line has no receipts the baseline is re-based to SAP's current delivered qty.
    """
    base, _ = POLineBaseline.objects.get_or_create(
        po_number=po_number, line_no=line_no, defaults={"sap_delivered_at_start": _sap_delivered(item)})
    base = POLineBaseline.objects.select_for_update().get(pk=base.pk)
    if not POReceipt.objects.filter(po_number=po_number, line_no=line_no).exists():
        base.sap_delivered_at_start = _sap_delivered(item)
        base.save()
    return base


def _room_error(item, d0, received_excl, qty):
    """Error text if adding qty would leave the pending-in-app qty above SAP's open qty, else None."""
    absorbed = _absorbed(item, d0)
    pending_before = max(ZERO, received_excl - absorbed)
    pending_after = max(ZERO, received_excl + qty - absorbed)
    open_qty = _open_qty(item)
    if pending_after > open_qty:
        return f"Exceeds open qty. Maximum you can still receive: {max(ZERO, open_qty - pending_before)}."
    return None


def _err(msg, status=400):
    return JsonResponse({"success": False, "error": msg}, status=status)


@login_required
@require_POST
def po_receiving_add(request, po_number):
    data = _json_body(request)
    try:
        line_no = int(data.get("line_no"))
    except (TypeError, ValueError):
        return _err("Invalid line.")
    qty, rdate, remarks, error = _validate_fields(data)
    if error:
        return _err(error)

    with transaction.atomic():
        item = _get_open_line(po_number, line_no)
        if not item:
            return _err("This line is not open.", 404)
        base = _lock_line(po_number, line_no, item)
        dup = POReceipt.objects.filter(
            po_number=po_number, line_no=line_no, received_by=request.user, qty_received=qty,
            received_date=rdate, remarks=remarks, created_at__gte=timezone.now() - timedelta(seconds=10),
        ).exists()
        if dup:
            return _err("This exact entry was just saved. For a separate delivery, wait a few seconds or change the remarks.")
        room_error = _room_error(item, base.sap_delivered_at_start, _line_total(po_number, line_no), qty)
        if room_error:
            return _err(room_error)
        r = POReceipt.objects.create(
            po_number=po_number, line_no=line_no, item_no=item.item_no or "",
            qty_received=qty, received_date=rdate, remarks=remarks, received_by=request.user,
        )
        POReceiptLog.objects.create(
            po_number=po_number, line_no=line_no, receipt_id=r.id, action="add",
            new_qty=qty, new_date=rdate, new_remarks=remarks, user=request.user,
        )
    return JsonResponse(_line_response(po_number, line_no, item))


@login_required
@require_POST
def po_receiving_edit(request, po_number, receipt_id):
    data = _json_body(request)
    qty, rdate, remarks, error = _validate_fields(data)
    if error:
        return _err(error)

    with transaction.atomic():
        r = POReceipt.objects.filter(id=receipt_id, po_number=po_number).first()
        if not r:
            return _err("Receipt not found.", 404)
        item = _get_open_line(po_number, r.line_no)
        if not item:
            return _err("This line is not open.", 404)
        base = _lock_line(po_number, r.line_no, item)
        r = POReceipt.objects.select_for_update().filter(id=receipt_id, po_number=po_number).first()
        if not r:
            return _err("Receipt not found.", 404)
        room_error = _room_error(item, base.sap_delivered_at_start,
                                 _line_total(po_number, r.line_no, exclude_id=r.id), qty)
        if room_error:
            return _err(room_error)
        POReceiptLog.objects.create(
            po_number=po_number, line_no=r.line_no, receipt_id=r.id, action="edit",
            old_qty=r.qty_received, new_qty=qty, old_date=r.received_date, new_date=rdate,
            old_remarks=r.remarks, new_remarks=remarks, user=request.user,
        )
        r.qty_received, r.received_date, r.remarks, r.received_by = qty, rdate, remarks, request.user
        r.save()
    return JsonResponse(_line_response(po_number, r.line_no, item))


@login_required
@require_POST
def po_receiving_delete(request, po_number, receipt_id):
    with transaction.atomic():
        r = POReceipt.objects.filter(id=receipt_id, po_number=po_number).first()
        if not r:
            return _err("Receipt not found.", 404)
        line_no = r.line_no
        item = SAPPurchaseOrderItem.objects.filter(purchaseorder__po_number=po_number, line_no=line_no).first()
        if item:
            _lock_line(po_number, line_no, item)
        r = POReceipt.objects.select_for_update().filter(id=receipt_id, po_number=po_number).first()
        if not r:
            return _err("Receipt not found.", 404)
        POReceiptLog.objects.create(
            po_number=po_number, line_no=line_no, receipt_id=r.id, action="delete",
            old_qty=r.qty_received, old_date=r.received_date, old_remarks=r.remarks, user=request.user,
        )
        r.delete()
    if not item:
        return JsonResponse({"success": True, "open_qty": 0, "absorbed": 0,
                             "received_total": float(_line_total(po_number, line_no)),
                             "pending": 0, "balance": 0, "receipts": []})
    return JsonResponse(_line_response(po_number, line_no, item))

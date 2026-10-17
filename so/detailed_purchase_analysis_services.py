"""
Detailed Purchase Analysis: mapping + saving of open AND closed purchase orders,
including the per-line WarehouseCode.

Everything here is independent from the existing open-PO sync
(SAPPurchaseOrder tables are never read or written).
"""
import logging
from datetime import datetime
from decimal import Decimal

from django.db import transaction

from .models import DetailedPurchaseOrder, DetailedPurchaseOrderItem

logger = logging.getLogger(__name__)


def _dec(x, places=2) -> Decimal:
    try:
        if x is None or (isinstance(x, float) and x != x):
            return Decimal("0")
        d = Decimal(str(x))
        return d.quantize(Decimal(1).scaleb(-places)) if places is not None else d
    except Exception:
        return Decimal("0")


def _parse_date(value):
    if not value:
        return None
    if hasattr(value, 'date') and not isinstance(value, str):
        return value.date()
    if isinstance(value, str):
        v = value.strip()[:10]
        for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
            try:
                return datetime.strptime(v, fmt).date()
            except ValueError:
                continue
    return None


def map_api_purchaseorder(api_order: dict) -> dict:
    """Map one raw SAP PurchaseOrder API record to a plain dict (JSON-safe)."""
    bp = api_order.get('BusinessPartner') or {}
    sales_person = api_order.get('SalesPerson') or {}

    posting_date = _parse_date(api_order.get('DocDate'))

    num_at_card = api_order.get('NumAtCard')
    closing_remarks = str(api_order.get('ClosingRemarks') or '').strip().replace('\r', '\n')

    doc_status = api_order.get('DocumentStatus', '')
    status = 'O' if doc_status == 'bost_Open' else 'C'

    items = []
    for idx, line in enumerate(api_order.get('DocumentLines') or []):
        line_num = line.get('LineNum', idx)
        line_no = line_num + 1 if isinstance(line_num, int) and line_num >= 0 else idx + 1

        qty = line.get('Quantity', 0) or 0
        price = line.get('Price', 0) or 0
        remaining = line.get('RemainingOpenQuantity', 0) or 0
        row_status = 'O' if line.get('LineStatus', '') == 'bost_Open' else 'C'

        items.append({
            'line_no': line_no,
            'item_no': str(line.get('ItemCode') or ''),
            'description': line.get('ItemDescription') or '',
            'warehouse_code': str(line.get('WarehouseCode') or '').strip(),
            'quantity': qty,
            'price': price,
            'row_total': line.get('LineTotal', 0) or 0,
            'row_status': row_status,
            'remaining_open_quantity': remaining,
            # closed lines have nothing pending
            'pending_amount': (remaining * price) if row_status == 'O' else 0,
        })

    return {
        'po_number': str(api_order.get('DocNum', '')),
        'internal_number': str(api_order.get('DocEntry', '')) if api_order.get('DocEntry') else '',
        'posting_date': posting_date.isoformat() if posting_date else None,
        'supplier_code': bp.get('CardCode', '') or api_order.get('CardCode', '') or '',
        'supplier_name': bp.get('CardName', '') or api_order.get('CardName', '') or '',
        'supplier_address': str(api_order.get('Address') or '').strip(),
        'supplier_phone': str(bp.get('Phone1') or '').strip(),
        'vat_number': str(bp.get('FederalTaxID') or '').strip(),
        'bp_reference_no': str(num_at_card).strip() if num_at_card else '',
        'salesman_name': sales_person.get('SalesEmployeeName', '') or str(api_order.get('SalesPersonCode', '') or ''),
        'discount_percentage': api_order.get('DiscountPercent', 0) or 0,
        'document_total': api_order.get('DocTotal', 0) or 0,
        'row_total_sum': sum((it['row_total'] or 0) for it in items),
        'vat_sum': api_order.get('VatSum', 0) or 0,
        'total_discount': api_order.get('TotalDiscount', 0) or 0,
        'status': status,
        'closing_remarks': closing_remarks,
        'items': items,
    }


def save_detailed_purchaseorders(purchase_orders: list) -> dict:
    """
    Upsert POs by po_number (never deletes other POs) and rebuild each PO's lines.
    Only touches the DetailedPurchaseOrder* tables.
    """
    stats = {'created': 0, 'updated': 0, 'total_items': 0, 'skipped': 0}

    with transaction.atomic():
        for mapped in purchase_orders:
            po_no = (mapped.get('po_number') or '').strip()
            if not po_no:
                stats['skipped'] += 1
                continue

            defaults = {
                'internal_number': mapped.get('internal_number') or '',
                'posting_date': _parse_date(mapped.get('posting_date')),
                'supplier_code': mapped.get('supplier_code') or '',
                'supplier_name': mapped.get('supplier_name') or '',
                'supplier_address': mapped.get('supplier_address') or '',
                'supplier_phone': mapped.get('supplier_phone') or '',
                'vat_number': mapped.get('vat_number') or '',
                'bp_reference_no': mapped.get('bp_reference_no') or '',
                'salesman_name': mapped.get('salesman_name') or '',
                'discount_percentage': _dec(mapped.get('discount_percentage')),
                'document_total': _dec(mapped.get('document_total')),
                'row_total_sum': _dec(mapped.get('row_total_sum')),
                'vat_sum': _dec(mapped.get('vat_sum')),
                'total_discount': _dec(mapped.get('total_discount')),
                'status': mapped.get('status') or 'C',
                'closing_remarks': mapped.get('closing_remarks') or '',
            }
            po, created = DetailedPurchaseOrder.objects.update_or_create(po_number=po_no, defaults=defaults)
            stats['created' if created else 'updated'] += 1

            po.items.all().delete()
            items = [
                DetailedPurchaseOrderItem(
                    purchaseorder=po,
                    line_no=it.get('line_no', 1),
                    item_no=it.get('item_no') or '',
                    description=(it.get('description') or '')[:255],
                    warehouse_code=(it.get('warehouse_code') or '')[:20],
                    quantity=_dec(it.get('quantity')),
                    price=_dec(it.get('price')),
                    row_total=_dec(it.get('row_total')),
                    row_status=it.get('row_status') or 'C',
                    remaining_open_quantity=_dec(it.get('remaining_open_quantity')),
                    pending_amount=_dec(it.get('pending_amount')),
                )
                for it in mapped.get('items', [])
            ]
            DetailedPurchaseOrderItem.objects.bulk_create(items, batch_size=2000)
            stats['total_items'] += len(items)

    return stats

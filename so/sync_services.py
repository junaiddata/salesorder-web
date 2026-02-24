"""
Sync services - core sync logic for VPS management commands and views.
Extracted from sync-from-api views for reuse by crontab/Celery.
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
from django.db import transaction

from .api_client import SAPAPIClient
from .models import (
    SAPSalesorder,
    SAPSalesorderItem,
    SAPProformaInvoice,
    SAPProformaInvoiceLine,
    SAPQuotation,
    SAPQuotationItem,
    SAPARInvoice,
    SAPARInvoiceItem,
    SAPARCreditMemo,
    SAPARCreditMemoItem,
    Customer,
)
logger = logging.getLogger(__name__)


def _dec2(x):
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return Decimal("0.00")
        return Decimal(str(x)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _dec_any(x):
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return Decimal("0")
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


# =====================
# Sales Orders
# =====================
def sync_salesorders_core(days_back=3, specific_date=None, docnum=None):
    """
    Fetch sales orders from SAP API and save to DB.
    Returns dict with created, updated, closed, total_orders, total_items, api_calls, errors.
    """
    sync_stats = {
        'created': 0,
        'updated': 0,
        'closed': 0,
        'total_orders': 0,
        'total_items': 0,
        'api_calls': 0,
        'errors': []
    }
    try:
        client = SAPAPIClient()
        all_orders = []

        if docnum:
            orders = client.fetch_salesorders_by_docnum(int(docnum))
            all_orders.extend(orders)
            sync_stats['api_calls'] = 1
        elif specific_date:
            orders = client.fetch_salesorders_by_date(specific_date)
            all_orders.extend(orders)
            sync_stats['api_calls'] = 1
        else:
            all_orders = client.sync_all_salesorders(days_back=days_back)
            sync_stats['api_calls'] = 1 + days_back

        all_orders = client._filter_ho_customers(all_orders)

        if not all_orders:
            return sync_stats

        mapped_orders = []
        for api_order in all_orders:
            try:
                mapped = client._map_api_response_to_model(api_order)
                mapped_orders.append(mapped)
            except Exception as e:
                logger.error(f"Error mapping order {api_order.get('DocNum')}: {e}")
                sync_stats['errors'].append(f"Error mapping order {api_order.get('DocNum')}: {str(e)}")

        if not mapped_orders:
            return sync_stats

        api_so_numbers = set(mapped['so_number'] for mapped in mapped_orders if mapped.get('so_number'))
        so_numbers = [m['so_number'] for m in mapped_orders if m.get('so_number')]
        sync_stats['total_orders'] = len(so_numbers)

        with transaction.atomic():
            try:
                existing_map = SAPSalesorder.objects.in_bulk(so_numbers, field_name="so_number")
            except TypeError:
                existing_map = {o.so_number: o for o in SAPSalesorder.objects.filter(so_number__in=so_numbers)}

            to_create = []
            to_update = []

            for mapped in mapped_orders:
                so_no = mapped.get('so_number')
                if not so_no:
                    continue

                defaults = {
                    "posting_date": mapped.get('posting_date'),
                    "customer_code": mapped.get('customer_code', ''),
                    "customer_name": mapped.get('customer_name', ''),
                    "bp_reference_no": mapped.get('bp_reference_no', ''),
                    "salesman_name": mapped.get('salesman_name', ''),
                    "discount_percentage": _dec2(mapped.get('discount_percentage', 0)),
                    "document_total": _dec2(mapped.get('document_total', 0)),
                    "row_total_sum": _dec2(mapped.get('row_total_sum', 0)),
                    "status": mapped.get('status', 'C'),
                    "vat_number": mapped.get('vat_number', '') or '',
                    "customer_address": mapped.get('customer_address', '') or '',
                    "customer_phone": mapped.get('customer_phone', '') or '',
                    "closing_remarks": mapped.get('closing_remarks', '') or '',
                    "is_sap_pi": mapped.get('is_sap_pi', False),
                    "nf_ref": mapped.get('nf_ref', '') or '',
                }
                if mapped.get('internal_number'):
                    defaults["internal_number"] = mapped.get('internal_number')
                if 'last_synced_at' in [f.name for f in SAPSalesorder._meta.get_fields()]:
                    defaults["last_synced_at"] = datetime.now()

                obj = existing_map.get(so_no)
                if obj is None:
                    to_create.append(SAPSalesorder(so_number=so_no, **defaults))
                    sync_stats['created'] += 1
                else:
                    for k, v in defaults.items():
                        setattr(obj, k, v)
                    to_update.append(obj)
                    sync_stats['updated'] += 1

            if to_create:
                SAPSalesorder.objects.bulk_create(to_create, batch_size=5000)
            if to_update:
                update_fields = [
                    "posting_date", "customer_code", "customer_name", "bp_reference_no",
                    "salesman_name", "discount_percentage", "document_total", "row_total_sum",
                    "status", "vat_number", "customer_address", "customer_phone", "closing_remarks",
                    "internal_number", "is_sap_pi", "nf_ref"
                ]
                if 'last_synced_at' in [f.name for f in SAPSalesorder._meta.get_fields()]:
                    update_fields.append("last_synced_at")
                    for obj in to_update:
                        obj.last_synced_at = datetime.now()
                SAPSalesorder.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)

            order_id_map = dict(
                SAPSalesorder.objects.filter(so_number__in=so_numbers).values_list("so_number", "id")
            )
            SAPSalesorderItem.objects.filter(salesorder__so_number__in=so_numbers).delete()

            items_to_create = []
            for mapped in mapped_orders:
                so_no = mapped.get('so_number')
                so_id = order_id_map.get(so_no)
                if not so_id:
                    continue
                for item_data in mapped.get('items', []):
                    items_to_create.append(
                        SAPSalesorderItem(
                            salesorder_id=so_id,
                            line_no=item_data.get('line_no', 1),
                            item_no=item_data.get('item_no', ''),
                            description=item_data.get('description', ''),
                            quantity=_dec_any(item_data.get('quantity', 0)),
                            price=_dec_any(item_data.get('price', 0)),
                            row_total=_dec_any(item_data.get('row_total', 0)),
                            row_status=item_data.get('row_status', 'C'),
                            job_type=item_data.get('job_type', ''),
                            manufacture=item_data.get('manufacture', ''),
                            remaining_open_quantity=_dec_any(item_data.get('remaining_open_quantity', 0)),
                            pending_amount=_dec_any(item_data.get('pending_amount', 0)),
                            total_available_stock=_dec_any(item_data.get('total_available_stock', 0)),
                            dip_warehouse_stock=_dec_any(item_data.get('dip_warehouse_stock', 0)),
                        )
                    )
                    if len(items_to_create) >= 10000:
                        SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=10000)
                        items_to_create = []

            if items_to_create:
                SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=20000)

            sync_stats['total_items'] = sum(len(m.get('items', [])) for m in mapped_orders)

            previously_open_orders = SAPSalesorder.objects.filter(
                status__in=['O', 'OPEN'],
                so_number__isnull=False
            ).exclude(so_number__in=api_so_numbers)

            closed_count = 0
            for order in previously_open_orders:
                order.status = 'C'
                order.save(update_fields=['status'])
                order.items.all().update(
                    row_status='C',
                    remaining_open_quantity=Decimal('0'),
                    pending_amount=Decimal('0')
                )
                closed_count += 1
            sync_stats['closed'] = closed_count

            for mapped in mapped_orders:
                so_no = mapped.get('so_number')
                is_sap_pi = mapped.get('is_sap_pi', False)
                sap_pi_lpo_date = mapped.get('sap_pi_lpo_date')
                if not is_sap_pi or not so_no:
                    continue
                try:
                    salesorder = SAPSalesorder.objects.get(so_number=so_no)
                    desired_pi_number = f"{so_no}"
                    legacy_pi_number = f"{so_no}-SAP"
                    sap_pi = SAPProformaInvoice.objects.filter(pi_number=desired_pi_number).first()
                    created = False
                    if sap_pi is None:
                        sap_pi = SAPProformaInvoice.objects.filter(pi_number=legacy_pi_number).first()
                        if sap_pi is not None:
                            if not SAPProformaInvoice.objects.filter(pi_number=desired_pi_number).exists():
                                sap_pi.pi_number = desired_pi_number
                                sap_pi.save(update_fields=["pi_number"])
                    if sap_pi is None:
                        remarks = salesorder.closing_remarks if salesorder.closing_remarks else ''
                        sap_pi = SAPProformaInvoice.objects.create(
                            pi_number=desired_pi_number,
                            salesorder=salesorder,
                            sequence=0,
                            status='ACTIVE',
                            is_sap_pi=True,
                            pi_date=salesorder.posting_date,
                            lpo_date=sap_pi_lpo_date,
                            remarks=remarks,
                        )
                        created = True
                    if not created:
                        sap_pi.salesorder = salesorder
                        sap_pi.is_sap_pi = True
                        sap_pi.pi_date = salesorder.posting_date
                        if sap_pi_lpo_date:
                            sap_pi.lpo_date = sap_pi_lpo_date
                        if salesorder.closing_remarks:
                            sap_pi.remarks = salesorder.closing_remarks
                        sap_pi.save()

                    sap_pi.lines.all().delete()
                    so_items = salesorder.items.all().order_by('line_no')
                    pi_lines_to_create = [
                        SAPProformaInvoiceLine(
                            pi=sap_pi,
                            so_item=so_item,
                            so_number=so_no,
                            line_no=so_item.line_no,
                            item_no=so_item.item_no or '',
                            description=so_item.description or '',
                            manufacture=so_item.manufacture or '',
                            job_type=so_item.job_type or '',
                            quantity=so_item.quantity or Decimal('0'),
                        )
                        for so_item in so_items
                    ]
                    if pi_lines_to_create:
                        SAPProformaInvoiceLine.objects.bulk_create(pi_lines_to_create, batch_size=1000)
                except SAPSalesorder.DoesNotExist:
                    logger.warning(f"Salesorder {so_no} not found when creating SAP PI")
                except Exception as e:
                    logger.error(f"Error creating SAP PI for {so_no}: {e}")

        for mapped in mapped_orders:
            customer_code = mapped.get('customer_code', '').strip()
            customer_address = mapped.get('customer_address', '').strip()
            customer_phone = mapped.get('customer_phone', '').strip()
            if customer_code:
                try:
                    if customer_phone:
                        max_phone_len = Customer._meta.get_field('phone_number').max_length
                        if len(customer_phone) > max_phone_len:
                            customer_phone = customer_phone[:max_phone_len]
                    customer, _ = Customer.objects.get_or_create(
                        customer_code=customer_code,
                        defaults={'customer_name': mapped.get('customer_name', '').strip() or customer_code}
                    )
                    if customer_address:
                        customer.address = customer_address
                    if customer_phone:
                        customer.phone_number = customer_phone
                    vat_num = mapped.get('vat_number', '').strip()
                    if vat_num:
                        customer.vat_number = vat_num
                    customer.save()
                except Exception as e:
                    logger.warning(f"Error updating Customer {customer_code}: {e}")

    except Exception as e:
        logger.exception("Error syncing sales orders from API")
        sync_stats['errors'].append(str(e))
    return sync_stats


# =====================
# Quotations
# =====================
def sync_quotations_core(days_back=3, specific_date=None):
    """
    Fetch quotations from SAP API and save to DB.
    Returns dict with created, updated, closed, total_quotations, total_items, api_calls, errors.
    """
    sync_stats = {
        'created': 0,
        'updated': 0,
        'closed': 0,
        'total_quotations': 0,
        'total_items': 0,
        'api_calls': 0,
        'errors': []
    }
    try:
        client = SAPAPIClient()
        if specific_date:
            all_quotations = client.fetch_quotations_by_date_range(specific_date, specific_date)
            sync_stats['api_calls'] = 1
        else:
            all_quotations = client.sync_all_quotations(days_back=days_back)
            sync_stats['api_calls'] = 1 + days_back

        if not all_quotations:
            return sync_stats

        mapped_quotations = []
        for api_quotation in all_quotations:
            try:
                mapped = client._map_quotation_api_response_to_model(api_quotation)
                mapped_quotations.append(mapped)
            except Exception as e:
                logger.error(f"Error mapping quotation {api_quotation.get('DocNum')}: {e}")
                sync_stats['errors'].append(f"Error mapping quotation {api_quotation.get('DocNum')}: {str(e)}")

        if not mapped_quotations:
            return sync_stats

        api_q_numbers = set(mapped['q_number'] for mapped in mapped_quotations if mapped.get('q_number'))
        q_numbers = [m['q_number'] for m in mapped_quotations if m.get('q_number')]
        sync_stats['total_quotations'] = len(q_numbers)

        with transaction.atomic():
            try:
                existing_map = SAPQuotation.objects.in_bulk(q_numbers, field_name="q_number")
            except TypeError:
                existing_map = {q.q_number: q for q in SAPQuotation.objects.filter(q_number__in=q_numbers)}

            to_create = []
            to_update = []
            for mapped in mapped_quotations:
                q_no = mapped.get('q_number')
                if not q_no:
                    continue
                defaults = {
                    "posting_date": mapped.get('posting_date'),
                    "customer_code": mapped.get('customer_code', ''),
                    "customer_name": mapped.get('customer_name', ''),
                    "bp_reference_no": mapped.get('bp_reference_no', ''),
                    "salesman_name": mapped.get('salesman_name', ''),
                    "document_total": _dec2(mapped.get('document_total', 0)),
                    "vat_sum": _dec2(mapped.get('vat_sum', 0)),
                    "total_discount": _dec2(mapped.get('total_discount', 0)),
                    "rounding_diff_amount": _dec2(mapped.get('rounding_diff_amount', 0)),
                    "discount_percent": _dec2(mapped.get('discount_percent', 0)),
                    "status": mapped.get('status', 'CLOSED'),
                    "bill_to": mapped.get('bill_to', '') or '',
                    "remarks": mapped.get('remarks', '') or '',
                }
                if mapped.get('internal_number'):
                    defaults["internal_number"] = mapped.get('internal_number')

                obj = existing_map.get(q_no)
                if obj is None:
                    to_create.append(SAPQuotation(q_number=q_no, **defaults))
                    sync_stats['created'] += 1
                else:
                    for k, v in defaults.items():
                        setattr(obj, k, v)
                    to_update.append(obj)
                    sync_stats['updated'] += 1

            if to_create:
                SAPQuotation.objects.bulk_create(to_create, batch_size=5000)
            if to_update:
                update_fields = [
                    "posting_date", "customer_code", "customer_name", "bp_reference_no",
                    "salesman_name", "document_total", "vat_sum", "total_discount",
                    "rounding_diff_amount", "discount_percent", "status", "bill_to",
                    "remarks", "internal_number"
                ]
                SAPQuotation.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)

            quotation_id_map = dict(
                SAPQuotation.objects.filter(q_number__in=q_numbers).values_list("q_number", "id")
            )
            SAPQuotationItem.objects.filter(quotation__q_number__in=q_numbers).delete()

            items_to_create = []
            for mapped in mapped_quotations:
                q_no = mapped.get('q_number')
                q_id = quotation_id_map.get(q_no)
                if not q_id:
                    continue
                for item_data in mapped.get('items', []):
                    items_to_create.append(
                        SAPQuotationItem(
                            quotation_id=q_id,
                            item_no=item_data.get('item_no', ''),
                            description=item_data.get('description', ''),
                            quantity=_dec_any(item_data.get('quantity', 0)),
                            price=_dec_any(item_data.get('price', 0)),
                            row_total=_dec_any(item_data.get('row_total', 0)),
                        )
                    )
                    if len(items_to_create) >= 10000:
                        SAPQuotationItem.objects.bulk_create(items_to_create, batch_size=10000)
                        items_to_create = []

            if items_to_create:
                SAPQuotationItem.objects.bulk_create(items_to_create, batch_size=20000)

            sync_stats['total_items'] = sum(len(m.get('items', [])) for m in mapped_quotations)

            previously_open = SAPQuotation.objects.filter(
                status__in=['O', 'OPEN', 'Open', 'open'],
                q_number__isnull=False
            ).exclude(q_number__in=api_q_numbers)
            closed_count = 0
            for quotation in previously_open:
                quotation.status = 'CLOSED'
                quotation.save(update_fields=['status'])
                closed_count += 1
            sync_stats['closed'] = closed_count

    except Exception as e:
        logger.exception("Error syncing quotations from API")
        sync_stats['errors'].append(str(e))
    return sync_stats


# =====================
# AR Invoices
# =====================
def _parse_date(val):
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return datetime.strptime(val, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None
    if hasattr(val, 'date'):
        return val.date() if hasattr(val, 'date') else val
    return val


def sync_arinvoices_core(days_back=3, specific_date=None, docnum=None):
    """
    Fetch AR invoices from SAP API and save to DB.
    Returns dict with created, updated, total_invoices, total_items, api_calls, errors.
    """
    sync_stats = {
        'created': 0,
        'updated': 0,
        'total_invoices': 0,
        'total_items': 0,
        'api_calls': 0,
        'errors': []
    }
    try:
        client = SAPAPIClient()
        all_invoices = []

        if docnum:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            invoices = client.fetch_arinvoices_by_date_range(
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d')
            )
            all_invoices = [inv for inv in invoices if str(inv.get('DocNum', '')) == docnum]
            sync_stats['api_calls'] = 1
        elif specific_date:
            invoices = client.fetch_arinvoices_by_date_range(specific_date, specific_date)
            all_invoices.extend(invoices)
            sync_stats['api_calls'] = 1
        else:
            all_invoices = client.fetch_arinvoices_last_n_days(days=days_back)
            sync_stats['api_calls'] = 1

        if not all_invoices:
            return sync_stats

        mapped_invoices = []
        for api_invoice in all_invoices:
            try:
                mapped = client._map_arinvoice_api_response(api_invoice)
                mapped_invoices.append(mapped)
            except Exception as e:
                logger.error(f"Error mapping invoice {api_invoice.get('DocNum')}: {e}")
                sync_stats['errors'].append(f"Error mapping invoice {api_invoice.get('DocNum')}: {str(e)}")

        if not mapped_invoices:
            return sync_stats

        invoice_numbers = [m['invoice_number'] for m in mapped_invoices if m.get('invoice_number')]
        sync_stats['total_invoices'] = len(invoice_numbers)

        with transaction.atomic():
            try:
                existing_map = SAPARInvoice.objects.in_bulk(invoice_numbers, field_name="invoice_number")
            except TypeError:
                existing_map = {o.invoice_number: o for o in SAPARInvoice.objects.filter(invoice_number__in=invoice_numbers)}

            to_create = []
            to_update = []
            for mapped in mapped_invoices:
                invoice_no = mapped.get('invoice_number')
                if not invoice_no:
                    continue
                posting_date = _parse_date(mapped.get('posting_date'))
                doc_due_date = _parse_date(mapped.get('doc_due_date'))
                defaults = {
                    "internal_number": mapped.get('internal_number'),
                    "posting_date": posting_date,
                    "doc_due_date": doc_due_date,
                    "customer_code": mapped.get('customer_code', ''),
                    "customer_name": mapped.get('customer_name', ''),
                    "customer_address": mapped.get('customer_address', ''),
                    "salesman_name": mapped.get('salesman_name', ''),
                    "salesman_code": mapped.get('salesman_code'),
                    "store": mapped.get('store', 'HO'),
                    "bp_reference_no": mapped.get('bp_reference_no', ''),
                    "doc_total": _dec2(mapped.get('doc_total', 0)),
                    "doc_total_without_vat": _dec2(mapped.get('doc_total_without_vat', 0)),
                    "subtotal_before_discount": _dec2(mapped.get('subtotal_before_discount', 0)),
                    "vat_sum": _dec2(mapped.get('vat_sum', 0)),
                    "rounding_diff_amount": _dec2(mapped.get('rounding_diff_amount', 0)),
                    "total_gross_profit": _dec2(mapped.get('total_gross_profit', 0)),
                    "discount_percent": _dec2(mapped.get('discount_percent', 0)),
                    "cancel_status": mapped.get('cancel_status', ''),
                    "document_status": mapped.get('document_status', ''),
                    "vat_number": mapped.get('vat_number', ''),
                    "comments": mapped.get('comments', ''),
                }

                obj = existing_map.get(invoice_no)
                if obj is None:
                    to_create.append(SAPARInvoice(invoice_number=invoice_no, **defaults))
                    sync_stats['created'] += 1
                else:
                    for k, v in defaults.items():
                        setattr(obj, k, v)
                    to_update.append(obj)
                    sync_stats['updated'] += 1

            if to_create:
                SAPARInvoice.objects.bulk_create(to_create, batch_size=5000)
            if to_update:
                update_fields = [
                    "internal_number", "posting_date", "doc_due_date", "customer_code", "customer_name",
                    "customer_address", "salesman_name", "salesman_code", "store", "bp_reference_no",
                    "doc_total", "doc_total_without_vat", "subtotal_before_discount", "vat_sum",
                    "rounding_diff_amount", "total_gross_profit", "discount_percent",
                    "cancel_status", "document_status", "vat_number", "comments"
                ]
                SAPARInvoice.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)

            invoice_id_map = dict(
                SAPARInvoice.objects.filter(invoice_number__in=invoice_numbers).values_list("invoice_number", "id")
            )
            SAPARInvoiceItem.objects.filter(invoice__invoice_number__in=invoice_numbers).delete()

            items_to_create = []
            for mapped in mapped_invoices:
                invoice_no = mapped.get('invoice_number')
                invoice_id = invoice_id_map.get(invoice_no)
                if not invoice_id:
                    continue
                for item_data in mapped.get('items', []):
                    item_id = item_data.get('item_id')
                    items_to_create.append(
                        SAPARInvoiceItem(
                            invoice_id=invoice_id,
                            item_id=item_id,
                            line_no=item_data.get('line_no', 1),
                            item_code=item_data.get('item_code', ''),
                            item_description=item_data.get('item_description', ''),
                            quantity=_dec_any(item_data.get('quantity', 0)),
                            price=_dec_any(item_data.get('price', 0)),
                            price_after_vat=_dec_any(item_data.get('price_after_vat', 0)),
                            discount_percent=_dec_any(item_data.get('discount_percent', 0)),
                            line_total=_dec_any(item_data.get('line_total', 0)),
                            line_total_after_discount=_dec_any(item_data.get('line_total_after_discount', 0)),
                            cost_price=_dec_any(item_data.get('cost_price', 0)),
                            gross_profit=_dec_any(item_data.get('gross_profit', 0)),
                            tax_percentage=_dec_any(item_data.get('tax_percentage', 0)),
                            tax_total=_dec_any(item_data.get('tax_total', 0)),
                            upc_code=item_data.get('upc_code', ''),
                        )
                    )
                    if len(items_to_create) >= 20000:
                        SAPARInvoiceItem.objects.bulk_create(items_to_create, batch_size=20000)
                        items_to_create = []

            if items_to_create:
                SAPARInvoiceItem.objects.bulk_create(items_to_create, batch_size=20000)
            sync_stats['total_items'] = sum(len(m.get('items', [])) for m in mapped_invoices)

    except Exception as e:
        logger.exception("Error syncing AR invoices")
        sync_stats['errors'].append(str(e))
    return sync_stats


# =====================
# AR Credit Memos
# =====================
def sync_arcreditmemos_core(days_back=3, specific_date=None, docnum=None):
    """
    Fetch AR credit memos from SAP API and save to DB.
    Returns dict with created, updated, total_creditmemos, total_items, api_calls, errors.
    """
    sync_stats = {
        'created': 0,
        'updated': 0,
        'total_creditmemos': 0,
        'total_items': 0,
        'api_calls': 0,
        'errors': []
    }
    try:
        client = SAPAPIClient()
        all_creditmemos = []

        if docnum:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            creditmemos = client.fetch_arcreditmemos_by_date_range(
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d')
            )
            all_creditmemos = [cm for cm in creditmemos if str(cm.get('DocNum', '')) == docnum]
            sync_stats['api_calls'] = 1
        elif specific_date:
            creditmemos = client.fetch_arcreditmemos_by_date_range(specific_date, specific_date)
            all_creditmemos.extend(creditmemos)
            sync_stats['api_calls'] = 1
        else:
            all_creditmemos = client.fetch_arcreditmemos_last_n_days(days=days_back)
            sync_stats['api_calls'] = 1

        if not all_creditmemos:
            return sync_stats

        mapped_creditmemos = []
        for api_creditmemo in all_creditmemos:
            try:
                mapped = client._map_arcreditmemo_api_response(api_creditmemo)
                mapped_creditmemos.append(mapped)
            except Exception as e:
                logger.error(f"Error mapping credit memo {api_creditmemo.get('DocNum')}: {e}")
                sync_stats['errors'].append(f"Error mapping credit memo {api_creditmemo.get('DocNum')}: {str(e)}")

        if not mapped_creditmemos:
            return sync_stats

        creditmemo_numbers = [m['credit_memo_number'] for m in mapped_creditmemos if m.get('credit_memo_number')]
        sync_stats['total_creditmemos'] = len(creditmemo_numbers)

        with transaction.atomic():
            try:
                existing_map = SAPARCreditMemo.objects.in_bulk(creditmemo_numbers, field_name="credit_memo_number")
            except TypeError:
                existing_map = {o.credit_memo_number: o for o in SAPARCreditMemo.objects.filter(credit_memo_number__in=creditmemo_numbers)}

            to_create = []
            to_update = []
            for mapped in mapped_creditmemos:
                creditmemo_no = mapped.get('credit_memo_number')
                if not creditmemo_no:
                    continue
                posting_date = _parse_date(mapped.get('posting_date'))
                doc_due_date = _parse_date(mapped.get('doc_due_date'))
                defaults = {
                    "internal_number": mapped.get('internal_number'),
                    "posting_date": posting_date,
                    "doc_due_date": doc_due_date,
                    "customer_code": mapped.get('customer_code', ''),
                    "customer_name": mapped.get('customer_name', ''),
                    "customer_address": mapped.get('customer_address', ''),
                    "salesman_name": mapped.get('salesman_name', ''),
                    "salesman_code": mapped.get('salesman_code'),
                    "store": mapped.get('store', 'HO'),
                    "bp_reference_no": mapped.get('bp_reference_no', ''),
                    "doc_total": _dec2(mapped.get('doc_total', 0)),
                    "doc_total_without_vat": _dec2(mapped.get('doc_total_without_vat', 0)),
                    "subtotal_before_discount": _dec2(mapped.get('subtotal_before_discount', 0)),
                    "vat_sum": _dec2(mapped.get('vat_sum', 0)),
                    "rounding_diff_amount": _dec2(mapped.get('rounding_diff_amount', 0)),
                    "total_gross_profit": _dec2(mapped.get('total_gross_profit', 0)),
                    "discount_percent": _dec2(mapped.get('discount_percent', 0)),
                    "cancel_status": mapped.get('cancel_status', ''),
                    "document_status": mapped.get('document_status', ''),
                    "vat_number": mapped.get('vat_number', ''),
                    "comments": mapped.get('comments', ''),
                }

                obj = existing_map.get(creditmemo_no)
                if obj is None:
                    to_create.append(SAPARCreditMemo(credit_memo_number=creditmemo_no, **defaults))
                    sync_stats['created'] += 1
                else:
                    for k, v in defaults.items():
                        setattr(obj, k, v)
                    to_update.append(obj)
                    sync_stats['updated'] += 1

            if to_create:
                SAPARCreditMemo.objects.bulk_create(to_create, batch_size=5000)
            if to_update:
                update_fields = [
                    "internal_number", "posting_date", "doc_due_date", "customer_code", "customer_name",
                    "customer_address", "salesman_name", "salesman_code", "store", "bp_reference_no",
                    "doc_total", "doc_total_without_vat", "subtotal_before_discount", "vat_sum",
                    "rounding_diff_amount", "total_gross_profit", "discount_percent",
                    "cancel_status", "document_status", "vat_number", "comments"
                ]
                SAPARCreditMemo.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)

            creditmemo_id_map = dict(
                SAPARCreditMemo.objects.filter(credit_memo_number__in=creditmemo_numbers).values_list("credit_memo_number", "id")
            )
            SAPARCreditMemoItem.objects.filter(credit_memo__credit_memo_number__in=creditmemo_numbers).delete()

            items_to_create = []
            for mapped in mapped_creditmemos:
                creditmemo_no = mapped.get('credit_memo_number')
                creditmemo_id = creditmemo_id_map.get(creditmemo_no)
                if not creditmemo_id:
                    continue
                for item_data in mapped.get('items', []):
                    item_id = item_data.get('item_id')
                    items_to_create.append(
                        SAPARCreditMemoItem(
                            credit_memo_id=creditmemo_id,
                            item_id=item_id,
                            line_no=item_data.get('line_no', 1),
                            item_code=item_data.get('item_code', ''),
                            item_description=item_data.get('item_description', ''),
                            quantity=_dec_any(item_data.get('quantity', 0)),
                            price=_dec_any(item_data.get('price', 0)),
                            price_after_vat=_dec_any(item_data.get('price_after_vat', 0)),
                            discount_percent=_dec_any(item_data.get('discount_percent', 0)),
                            line_total=_dec_any(item_data.get('line_total', 0)),
                            line_total_after_discount=_dec_any(item_data.get('line_total_after_discount', 0)),
                            cost_price=_dec_any(item_data.get('cost_price', 0)),
                            gross_profit=_dec_any(item_data.get('gross_profit', 0)),
                            tax_percentage=_dec_any(item_data.get('tax_percentage', 0)),
                            tax_total=_dec_any(item_data.get('tax_total', 0)),
                            upc_code=item_data.get('upc_code', ''),
                        )
                    )
                    if len(items_to_create) >= 20000:
                        SAPARCreditMemoItem.objects.bulk_create(items_to_create, batch_size=20000)
                        items_to_create = []

            if items_to_create:
                SAPARCreditMemoItem.objects.bulk_create(items_to_create, batch_size=20000)
            sync_stats['total_items'] = sum(len(m.get('items', [])) for m in mapped_creditmemos)

    except Exception as e:
        logger.exception("Error syncing AR credit memos")
        sync_stats['errors'].append(str(e))
    return sync_stats


# =====================
# Purchase Orders
# =====================
def sync_purchaseorders_core():
    """
    Fetch OPEN purchase orders from SAP API and save to DB (full replace).
    Returns dict with replaced, total_items, errors.
    """
    sync_stats = {
        'replaced': 0,
        'total_items': 0,
        'errors': []
    }
    try:
        client = SAPAPIClient()
        open_orders = client.fetch_open_purchaseorders()
        seen_docnums = set()
        all_orders = []
        for order in open_orders:
            docnum_val = order.get('DocNum')
            if docnum_val and docnum_val not in seen_docnums:
                all_orders.append(order)
                seen_docnums.add(docnum_val)

        if not all_orders:
            return sync_stats

        mapped_orders = []
        for api_order in all_orders:
            try:
                mapped = client._map_purchaseorder_api_response(api_order)
                mapped_orders.append(mapped)
            except Exception as e:
                logger.error(f"Error mapping PO {api_order.get('DocNum')}: {e}")
                sync_stats['errors'].append(f"Error mapping PO {api_order.get('DocNum')}: {str(e)}")

        if not mapped_orders:
            return sync_stats

        api_po_numbers = [m['po_number'] for m in mapped_orders if m.get('po_number')]

        from .sap_purchaseorder_views import save_purchaseorders_locally
        stats = save_purchaseorders_locally(mapped_orders, api_po_numbers)
        sync_stats['replaced'] = stats.get('replaced', 0)
        sync_stats['total_items'] = stats.get('total_items', 0)

    except Exception as e:
        logger.exception("Error syncing purchase orders from API")
        sync_stats['errors'].append(str(e))
    return sync_stats

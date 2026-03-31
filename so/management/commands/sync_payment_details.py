"""
Django management command to sync customer pending invoices from GetPaymentDetails API.
"""
from datetime import date, datetime
import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from so.api_client import SAPAPIClient
from so.models import Customer, CustomerPendingInvoice


logger = logging.getLogger('sync_payment_details')


def _safe_float(value, default=0.0):
    if value is None:
        return default
    try:
        if isinstance(value, str) and value.strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            if text == "":
                return None
            return int(float(text))
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_doc_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def process_payment_details_data(payment_details_data, full_refresh=True):
    """
    Sync payment details into CustomerPendingInvoice table.
    Skips rows when CardCode does not match an existing customer.
    """
    stats = {
        "total_received": len(payment_details_data or []),
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "skipped_no_customer": 0,
        "skipped_non_pending": 0,
        "skipped_invalid_docnum": 0,
        "errors": [],
    }

    if not payment_details_data:
        return stats

    try:
        with transaction.atomic():
            card_codes = {
                (record.get("CardCode") or "").strip()
                for record in payment_details_data
                if (record.get("CardCode") or "").strip()
            }
            customer_map = {
                c.customer_code: c
                for c in Customer.objects.filter(customer_code__in=card_codes).only("id", "customer_code")
            }

            if full_refresh:
                stats["deleted"] = CustomerPendingInvoice.objects.all().delete()[0]

            deduped_records = {}
            for record in payment_details_data:
                try:
                    card_code = (record.get("CardCode") or "").strip()
                    if not card_code:
                        stats["skipped_no_customer"] += 1
                        continue

                    customer = customer_map.get(card_code)
                    if customer is None:
                        stats["skipped_no_customer"] += 1
                        continue

                    doc_num = _safe_int(record.get("DocNum"))
                    if doc_num is None:
                        stats["skipped_invalid_docnum"] += 1
                        continue

                    balance_due = _safe_float(record.get("Balance Due", record.get("BalanceDue", 0)))
                    if balance_due <= 1e-6:
                        stats["skipped_non_pending"] += 1
                        continue

                    deduped_records[(customer.id, doc_num)] = CustomerPendingInvoice(
                        customer=customer,
                        doc_num=doc_num,
                        doc_date=_parse_doc_date(record.get("DocDate")),
                        num_at_card=(record.get("NumAtCard") or "").strip() or None,
                        doc_total=_safe_float(record.get("DocTotal", 0)),
                        paid_to_date=_safe_float(record.get("PaidToDate", 0)),
                        balance_due=balance_due,
                        slp_name=(record.get("SlpName") or "").strip() or None,
                    )
                except Exception as exc:
                    stats["errors"].append(f"Error processing row: {exc}")

            if deduped_records:
                CustomerPendingInvoice.objects.bulk_create(
                    list(deduped_records.values()),
                    batch_size=1000,
                )
                stats["created"] = len(deduped_records)

    except Exception as exc:
        logger.exception("Error during payment details sync")
        stats["errors"].append(f"Sync transaction failed: {exc}")
        raise

    return stats


def sync_payment_details():
    """
    Fetch payment details from SAP and sync to DB (pending invoices only).
    """
    client = SAPAPIClient()
    from_date = date(2020, 1, 1).strftime("%Y-%m-%d")
    to_date = date.today().strftime("%Y-%m-%d")

    payment_details_data = client.fetch_payment_details(from_date=from_date, to_date=to_date)
    return process_payment_details_data(payment_details_data, full_refresh=True)


class Command(BaseCommand):
    help = "Sync customer pending invoices from GetPaymentDetails API"

    def handle(self, *args, **options):
        sync_start = datetime.now()
        self.stdout.write("=" * 70)
        self.stdout.write("SAP Pending Invoice Sync (GetPaymentDetails)")
        self.stdout.write("=" * 70)
        self.stdout.write(f"Started at: {sync_start.strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            stats = sync_payment_details()
        except Exception as exc:
            self.stderr.write(f"Sync failed: {exc}")
            raise SystemExit(1)

        duration = (datetime.now() - sync_start).total_seconds()
        self.stdout.write(
            "Created: {created} | Deleted: {deleted} | Skipped(no customer): {skipped_no_customer} | "
            "Skipped(non-pending): {skipped_non_pending} | Errors: {errors_count}".format(
                created=stats.get("created", 0),
                deleted=stats.get("deleted", 0),
                skipped_no_customer=stats.get("skipped_no_customer", 0),
                skipped_non_pending=stats.get("skipped_non_pending", 0),
                errors_count=len(stats.get("errors", [])),
            )
        )
        self.stdout.write(f"Duration: {duration:.2f}s")
        self.stdout.write("=" * 70)

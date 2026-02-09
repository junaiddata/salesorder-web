"""
Django management command to sync customer finance summary from FinanceSummary API
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from so.api_client import SAPAPIClient
from so.models import Customer, Salesman
from so.salesman_mapping import map_salesman_name
import logging

logger = logging.getLogger(__name__)


def sync_customer_finance_summary():
    """
    Sync customer finance summary data from FinanceSummary API to Customer model
    
    Returns:
        Dictionary with sync statistics: {'created': int, 'updated': int, 'errors': list}
    """
    stats = {
        'created': 0,
        'updated': 0,
        'errors': []
    }
    
    client = SAPAPIClient()
    
    # Fetch finance summary data from API
    finance_data = client.fetch_finance_summary()
    
    if not finance_data:
        logger.warning("No finance summary data received from API")
        return stats
    
    logger.info(f"Processing {len(finance_data)} customer finance records...")
    
    # Helper function to safely convert to float
    def safe_float(value, default=0.0):
        """Safely convert value to float"""
        if value is None:
            return default
        try:
            if isinstance(value, str) and value.strip() == '':
                return default
            return float(value)
        except (ValueError, TypeError):
            return default
    
    # Helper function to safely convert to string
    def safe_str(value, default='0', max_length=None):
        """Safely convert value to string"""
        if value is None:
            return default
        try:
            result = str(value).strip()
            if result == '' or result.lower() == 'null' or result == '-Null-':
                return default
            if max_length and len(result) > max_length:
                return result[:max_length]
            return result
        except Exception:
            return default
    
    try:
        with transaction.atomic():
            # Get all customer codes from API data
            customer_codes = [record.get('CardCode') for record in finance_data if record.get('CardCode')]
            
            # Fetch existing customers in bulk
            existing_customers = Customer.objects.filter(customer_code__in=customer_codes)
            existing_map = {c.customer_code: c for c in existing_customers}
            
            # Get all unique salesman names and map them
            salesman_names = set()
            salesman_name_mapping = {}  # Maps SAP name to mapped name
            for record in finance_data:
                sap_salesman_name = record.get('Sales Employee', '').strip()
                if sap_salesman_name:
                    # Map the SAP name to simplified name
                    mapped_name = map_salesman_name(sap_salesman_name)
                    if mapped_name:
                        salesman_names.add(mapped_name)
                        salesman_name_mapping[sap_salesman_name] = mapped_name
            
            # Get or create salesmen in bulk (using mapped names)
            salesman_map = {}
            for mapped_name in salesman_names:
                salesman, _ = Salesman.objects.get_or_create(salesman_name=mapped_name)
                salesman_map[mapped_name] = salesman
            
            to_create = []
            to_update = []
            
            for record in finance_data:
                try:
                    card_code = record.get('CardCode', '').strip()
                    if not card_code:
                        stats['errors'].append("Record missing CardCode, skipping")
                        continue
                    
                    card_name = record.get('CardName', '').strip() or card_code
                    sap_salesman_name = record.get('Sales Employee', '').strip()
                    
                    # Handle salesman - map SAP name to simplified name
                    salesman = None
                    if sap_salesman_name:
                        mapped_name = map_salesman_name(sap_salesman_name)
                        if mapped_name:
                            salesman = salesman_map.get(mapped_name)
                    
                    # Map finance fields
                    credit_limit = safe_float(record.get('CreditLimit', 0))
                    credit_days = safe_str(record.get('CreditDays', '0'), default='0', max_length=30)
                    # Reverse mapping: API "1" → month_pending_6, API "2" → month_pending_5, etc.
                    month_pending_1 = safe_float(record.get('6', 0))  # API "6" → month_pending_1
                    month_pending_2 = safe_float(record.get('5', 0))  # API "5" → month_pending_2
                    month_pending_3 = safe_float(record.get('4', 0))  # API "4" → month_pending_3
                    month_pending_4 = safe_float(record.get('3', 0))  # API "3" → month_pending_4
                    month_pending_5 = safe_float(record.get('2', 0))  # API "2" → month_pending_5
                    month_pending_6 = safe_float(record.get('1', 0))  # API "1" → month_pending_6
                    old_months_pending = safe_float(record.get('6+', 0))
                    total_outstanding = safe_float(record.get('BalanceDue', 0))
                    pdc_received = safe_float(record.get('ChecksBal', 0))
                    total_outstanding_with_pdc = total_outstanding + pdc_received
                    
                    # Check if customer exists
                    customer = existing_map.get(card_code)
                    
                    if customer is None:
                        # Create new customer
                        customer = Customer(
                            customer_code=card_code,
                            customer_name=card_name,
                            salesman=salesman,
                            credit_limit=credit_limit,
                            credit_days=credit_days,
                            month_pending_1=month_pending_1,
                            month_pending_2=month_pending_2,
                            month_pending_3=month_pending_3,
                            month_pending_4=month_pending_4,
                            month_pending_5=month_pending_5,
                            month_pending_6=month_pending_6,
                            old_months_pending=old_months_pending,
                            total_outstanding=total_outstanding,
                            pdc_received=pdc_received,
                            total_outstanding_with_pdc=total_outstanding_with_pdc
                        )
                        to_create.append(customer)
                        stats['created'] += 1
                    else:
                        # Update existing customer
                        customer.customer_name = card_name
                        customer.salesman = salesman
                        customer.credit_limit = credit_limit
                        customer.credit_days = credit_days
                        customer.month_pending_1 = month_pending_1
                        customer.month_pending_2 = month_pending_2
                        customer.month_pending_3 = month_pending_3
                        customer.month_pending_4 = month_pending_4
                        customer.month_pending_5 = month_pending_5
                        customer.month_pending_6 = month_pending_6
                        customer.old_months_pending = old_months_pending
                        customer.total_outstanding = total_outstanding
                        customer.pdc_received = pdc_received
                        customer.total_outstanding_with_pdc = total_outstanding_with_pdc
                        to_update.append(customer)
                        stats['updated'] += 1
                        
                except Exception as e:
                    error_msg = f"Error processing record {record.get('CardCode', 'UNKNOWN')}: {str(e)}"
                    logger.error(error_msg)
                    logger.exception(f"Error processing finance record")
                    stats['errors'].append(error_msg)
                    continue
            
            # Bulk create/update
            if to_create:
                Customer.objects.bulk_create(to_create, batch_size=1000)
                logger.info(f"Created {len(to_create)} customers")
            
            if to_update:
                update_fields = [
                    'customer_name', 'salesman', 'credit_limit', 'credit_days',
                    'month_pending_1', 'month_pending_2', 'month_pending_3',
                    'month_pending_4', 'month_pending_5', 'month_pending_6',
                    'old_months_pending', 'total_outstanding', 'pdc_received',
                    'total_outstanding_with_pdc'
                ]
                Customer.objects.bulk_update(to_update, fields=update_fields, batch_size=1000)
                logger.info(f"Updated {len(to_update)} customers")
            
            logger.info(f"Sync completed: {stats['created']} created, {stats['updated']} updated, {len(stats['errors'])} errors")
            
    except Exception as e:
        error_msg = f"Error during sync transaction: {str(e)}"
        logger.error(error_msg)
        logger.exception("Error during sync transaction")
        stats['errors'].append(error_msg)
        raise
    
    return stats


class Command(BaseCommand):
    help = 'Sync customer finance summary from FinanceSummary API'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting customer finance summary sync...'))
        
        try:
            stats = sync_customer_finance_summary()
            
            self.stdout.write(self.style.SUCCESS(f'\nSync completed successfully!'))
            self.stdout.write(f'Created: {stats["created"]} customers')
            self.stdout.write(f'Updated: {stats["updated"]} customers')
            
            if stats['errors']:
                self.stdout.write(self.style.WARNING(f'\nErrors encountered: {len(stats["errors"])}'))
                for error in stats['errors'][:10]:  # Show first 10 errors
                    self.stdout.write(self.style.ERROR(f'  - {error}'))
                if len(stats['errors']) > 10:
                    self.stdout.write(self.style.WARNING(f'  ... and {len(stats["errors"]) - 10} more errors'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\nError during sync: {str(e)}'))
            logger.exception("Error during sync")
            raise

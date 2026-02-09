"""
Django management command to fix salesman names using mapping rules
Run this to fix existing salesman names that were synced from SAP
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from so.models import Salesman, Customer
from so.salesman_mapping import map_salesman_name
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Fix salesman names using mapping rules'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be changed without actually changing it'
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        
        self.stdout.write(self.style.SUCCESS('Starting salesman name mapping fix...'))
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))
        
        stats = {
            'salesmen_created': 0,
            'salesmen_updated': 0,
            'customers_updated': 0,
            'errors': []
        }
        
        try:
            with transaction.atomic():
                # Get all unique salesman names from customers
                all_salesman_names = Customer.objects.exclude(salesman__isnull=True).values_list('salesman__salesman_name', flat=True).distinct()
                
                # Also get salesmen that might not be linked to customers
                all_salesmen = Salesman.objects.all()
                
                # Collect all unique salesman names
                unique_names = set()
                for salesman in all_salesmen:
                    unique_names.add(salesman.salesman_name)
                
                self.stdout.write(f'Found {len(unique_names)} unique salesman names to process')
                
                # Create mapping of old names to new names
                name_mapping = {}
                for old_name in unique_names:
                    new_name = map_salesman_name(old_name)
                    if new_name != old_name:
                        name_mapping[old_name] = new_name
                
                self.stdout.write(f'Found {len(name_mapping)} names that need mapping')
                
                if not name_mapping:
                    self.stdout.write(self.style.SUCCESS('No salesman names need mapping!'))
                    return
                
                # Process each mapping
                for old_name, new_name in name_mapping.items():
                    try:
                        self.stdout.write(f'  Mapping: "{old_name}" → "{new_name}"')
                        
                        # Get or create the target salesman
                        target_salesman, created = Salesman.objects.get_or_create(salesman_name=new_name)
                        if created:
                            stats['salesmen_created'] += 1
                            self.stdout.write(f'    Created new salesman: {new_name}')
                        
                        # Get the old salesman
                        try:
                            old_salesman = Salesman.objects.get(salesman_name=old_name)
                        except Salesman.DoesNotExist:
                            self.stdout.write(self.style.WARNING(f'    Old salesman "{old_name}" not found, skipping'))
                            continue
                        
                        # Update all customers with old salesman to use new salesman
                        customers_updated = Customer.objects.filter(salesman=old_salesman).update(salesman=target_salesman)
                        stats['customers_updated'] += customers_updated
                        
                        if customers_updated > 0:
                            self.stdout.write(f'    Updated {customers_updated} customers')
                        
                        # Delete old salesman if it's different from new one
                        if old_name != new_name:
                            if not dry_run:
                                old_salesman.delete()
                                stats['salesmen_updated'] += 1
                            self.stdout.write(f'    Deleted old salesman: {old_name}')
                        
                    except Exception as e:
                        error_msg = f"Error processing {old_name}: {str(e)}"
                        stats['errors'].append(error_msg)
                        self.stdout.write(self.style.ERROR(f'    {error_msg}'))
                        logger.exception(f"Error processing salesman {old_name}")
                
                if dry_run:
                    self.stdout.write(self.style.WARNING('\nDRY RUN - Rolling back changes'))
                    raise transaction.TransactionManagementError("Dry run - rollback")
                
                self.stdout.write(self.style.SUCCESS('\n' + '=' * 70))
                self.stdout.write(self.style.SUCCESS('MAPPING COMPLETED'))
                self.stdout.write(self.style.SUCCESS('=' * 70))
                self.stdout.write(f'Salesmen created: {stats["salesmen_created"]}')
                self.stdout.write(f'Salesmen deleted: {stats["salesmen_updated"]}')
                self.stdout.write(f'Customers updated: {stats["customers_updated"]}')
                
                if stats['errors']:
                    self.stdout.write(self.style.WARNING(f'\nErrors: {len(stats["errors"])}'))
                    for error in stats['errors'][:10]:
                        self.stdout.write(self.style.ERROR(f'  - {error}'))
                
        except transaction.TransactionManagementError:
            # Dry run rollback
            pass
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\nError during mapping: {str(e)}'))
            logger.exception("Error during salesman mapping")
            raise

"""
Django management command to sync both AR Invoices and AR Credit Memos from SAP API.
This command can be run periodically (e.g., every 7 minutes) to keep data live.
"""
from django.core.management.base import BaseCommand
from django.core.management import call_command
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Sync AR Invoices and AR Credit Memos from SAP API'

    def add_arguments(self, parser):
        parser.add_argument(
            '--from-date',
            type=str,
            help='Start date for syncing (YYYY-MM-DD). If not provided, syncs from last sync date or all data.',
        )
        parser.add_argument(
            '--to-date',
            type=str,
            help='End date for syncing (YYYY-MM-DD). If not provided, syncs up to today.',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting sync of AR Invoices and Credit Memos...'))
        
        from_date = options.get('from_date')
        to_date = options.get('to_date')
        
        # Build command arguments
        invoice_args = {}
        creditmemo_args = {}
        
        if from_date:
            invoice_args['--from-date'] = from_date
            creditmemo_args['--from-date'] = from_date
        
        if to_date:
            invoice_args['--to-date'] = to_date
            creditmemo_args['--to-date'] = to_date
        
        try:
            # Sync AR Invoices
            self.stdout.write(self.style.WARNING('Syncing AR Invoices...'))
            call_command('sync_arinvoices_api', **invoice_args)
            self.stdout.write(self.style.SUCCESS('✓ AR Invoices sync completed'))
            
            # Sync AR Credit Memos
            self.stdout.write(self.style.WARNING('Syncing AR Credit Memos...'))
            call_command('sync_arcreditmemos_api', **creditmemo_args)
            self.stdout.write(self.style.SUCCESS('✓ AR Credit Memos sync completed'))
            
            self.stdout.write(self.style.SUCCESS('\n✓ All sync operations completed successfully!'))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\n✗ Error during sync: {str(e)}'))
            logger.error(f'Error syncing sales data: {str(e)}', exc_info=True)
            raise

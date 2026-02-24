"""
VPS management command: Sync AR Invoices and AR Credit Memos from SAP API.
Same as local sync_all_sales_data but runs on VPS via tunnel.

Usage:
    python manage.py sync_all_sales_data_vps
    python manage.py sync_all_sales_data_vps --days-back 7
"""
import sys
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
import logging
from logging.handlers import RotatingFileHandler

BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'sync_all_sales_data.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('sync_all_sales_data_vps')
logger.setLevel(logging.INFO)
logger.handlers = []

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(console_handler)


class Command(BaseCommand):
    help = 'Sync AR Invoices and AR Credit Memos from SAP API (runs on VPS via tunnel)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days-back',
            type=int,
            default=getattr(settings, 'SAP_SYNC_DAYS_BACK', 3),
            help='Number of days to fetch (default: 3)',
        )

    def handle(self, *args, **options):
        days_back = options['days_back']
        sync_start = datetime.now()

        logger.info('=' * 70)
        logger.info('SAP Sync All Sales Data (VPS) - AR Invoices + AR Credit Memos')
        logger.info('=' * 70)
        logger.info(f'Started at: {sync_start.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'Days back: {days_back}')
        logger.info('-' * 70)

        commands = [
            ('sync_arinvoices_vps', {'days_back': days_back}, 'AR Invoices'),
            ('sync_arcreditmemos_vps', {'days_back': days_back}, 'AR Credit Memos'),
        ]

        errors = []
        for cmd_name, cmd_kwargs, label in commands:
            try:
                logger.info(f'Running {label}...')
                call_command(cmd_name, **cmd_kwargs)
                logger.info(f'[OK] {label} completed')
            except Exception as e:
                logger.exception(f'Error during {label}')
                errors.append(f'{label}: {str(e)}')

        sync_end = datetime.now()
        duration = (sync_end - sync_start).total_seconds()

        logger.info('-' * 70)
        if errors:
            logger.error('SYNC COMPLETED WITH ERRORS')
            for err in errors:
                logger.error(f'  - {err}')
            raise SystemExit(1)
        else:
            logger.info('SYNC ALL COMPLETED SUCCESSFULLY')
        logger.info(f'Total duration: {duration:.2f}s')
        logger.info('=' * 70)

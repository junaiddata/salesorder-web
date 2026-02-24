"""
VPS management command: Sync sales orders from SAP API and save to local DB.
Runs on VPS when SSH tunnel exposes SAP API at localhost:8443.

Usage:
    python manage.py sync_salesorders_vps
    python manage.py sync_salesorders_vps --days-back 7
    python manage.py sync_salesorders_vps --specific-date 2026-01-21
    python manage.py sync_salesorders_vps --docnum 12345
"""
import sys
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings
import logging
from logging.handlers import RotatingFileHandler

from so.sync_services import sync_salesorders_core

BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'sync_salesorders.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('sync_salesorders_vps')
logger.setLevel(logging.INFO)
logger.handlers = []

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(console_handler)


class Command(BaseCommand):
    help = 'Sync sales orders from SAP API to local DB (runs on VPS via tunnel)'

    def add_arguments(self, parser):
        parser.add_argument('--days-back', type=int, default=getattr(settings, 'SAP_SYNC_DAYS_BACK', 3), help='Number of days to fetch (default: 3)')
        parser.add_argument('--specific-date', type=str, default=None, help='Single date to fetch (YYYY-MM-DD)')
        parser.add_argument('--docnum', type=int, default=None, help='Single DocNum to fetch')

    def handle(self, *args, **options):
        days_back = options['days_back']
        specific_date = options.get('specific_date')
        docnum = options.get('docnum')
        sync_start = datetime.now()

        logger.info('=' * 70)
        logger.info('SAP Sales Order Sync (VPS)')
        logger.info('=' * 70)
        logger.info(f'Started at: {sync_start.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'API: {getattr(settings, "SAP_API_BASE_URL", "")}')
        logger.info(f'Log file: {LOG_FILE}')
        if specific_date:
            logger.info(f'Date filter: {specific_date}')
        elif docnum:
            logger.info(f'DocNum filter: {docnum}')
        else:
            logger.info(f'Days back: {days_back}')
        logger.info('-' * 70)

        try:
            stats = sync_salesorders_core(days_back=days_back, specific_date=specific_date, docnum=docnum)
        except Exception as e:
            logger.exception('Error during sync')
            raise SystemExit(1)

        sync_end = datetime.now()
        duration = (sync_end - sync_start).total_seconds()

        if stats['errors']:
            logger.error(f"Errors: {stats['errors']}")
            raise SystemExit(1)

        logger.info('SYNC SUMMARY')
        logger.info(f'Created: {stats["created"]} | Updated: {stats["updated"]} | Closed: {stats["closed"]} | Total items: {stats["total_items"]}')
        logger.info(f'Duration: {duration:.2f}s')
        logger.info('=' * 70)

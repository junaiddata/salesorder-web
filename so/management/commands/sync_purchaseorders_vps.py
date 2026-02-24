"""
VPS management command: Sync open purchase orders from SAP API and save to local DB.
Runs on VPS when SSH tunnel exposes SAP API at localhost:8443.
Fetches DocumentStatus=bost_Open only (full replace of local PO data).

Usage:
    python manage.py sync_purchaseorders_vps
"""
import sys
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings
import logging
from logging.handlers import RotatingFileHandler

from so.sync_services import sync_purchaseorders_core

BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'sync_purchaseorders.log'
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('sync_purchaseorders_vps')
logger.setLevel(logging.INFO)
logger.handlers = []

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(console_handler)


class Command(BaseCommand):
    help = 'Sync open purchase orders from SAP API to local DB (runs on VPS via tunnel)'

    def handle(self, *args, **options):
        sync_start = datetime.now()

        logger.info('=' * 70)
        logger.info('SAP Purchase Order Sync (VPS)')
        logger.info('=' * 70)
        logger.info(f'Started at: {sync_start.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'API: {getattr(settings, "SAP_PURCHASE_ORDER_API_URL", "")}')
        logger.info(f'Log file: {LOG_FILE}')
        logger.info('Fetching: DocumentStatus=bost_Open (all pages)')
        logger.info('-' * 70)

        try:
            stats = sync_purchaseorders_core()
        except Exception as e:
            logger.exception('Error during sync')
            raise SystemExit(1)

        sync_end = datetime.now()
        duration = (sync_end - sync_start).total_seconds()

        if stats['errors']:
            logger.error(f"Errors: {stats['errors']}")
            raise SystemExit(1)

        logger.info('SYNC SUMMARY')
        logger.info(f'Replaced: {stats["replaced"]} | Total items: {stats["total_items"]}')
        logger.info(f'Duration: {duration:.2f}s')
        logger.info('=' * 70)

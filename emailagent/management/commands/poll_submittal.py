"""
Periodic management command: poll the fourth, separate SUBMITTAL_IMAP_*
mailbox, classify new emails, and store them -- SUBMITTAL-ONLY, meaning
quotation drafting and LPO processing never run for anything from this
mailbox regardless of how it classifies (see
emailagent/services.py:poll_submittal_mailbox / process_new_message's
allow_quotation/allow_lpo params). Otherwise mirrors poll_project.py exactly -- same
IMAP mechanics (emailagent/outlook_client.py), just a separate mailbox and
its own UID watermark.

Usage:
    python manage.py poll_submittal
    python manage.py poll_submittal --dry-run
    python manage.py poll_submittal --max-results 20
"""
import sys
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
import logging
from logging.handlers import RotatingFileHandler

from emailagent.services import poll_submittal_mailbox

BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'poll_submittal.log'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5

LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('poll_submittal')
logger.setLevel(logging.INFO)
logger.handlers = []

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(console_handler)


class Command(BaseCommand):
    help = 'Poll the fourth SUBMITTAL_IMAP_* mailbox for new emails (submittal-only -- no quotation/LPO processing)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                             help='Classify but do not write to the DB or send Telegram notifications')
        parser.add_argument('--max-results', type=int, default=None,
                             help='Cap the number of new messages processed this run -- also lets a '
                                  'never-before-polled mailbox process its most recent N messages '
                                  'instead of none (see outlook_client.list_new_message_uids)')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        max_results = options.get('max_results')
        run_start = datetime.now()

        logger.info('=' * 70)
        logger.info('Submittal Mailbox Poll (Email Tracking Agent -- submittal-only)')
        logger.info('=' * 70)
        logger.info(f'Started at: {run_start.strftime("%Y-%m-%d %H:%M:%S")}')
        logger.info(f'Dry run: {dry_run}')
        logger.info('-' * 70)

        try:
            stats = poll_submittal_mailbox(dry_run=dry_run, max_results=max_results)
        except Exception:
            logger.exception('Error during submittal mailbox poll')
            raise SystemExit(1)

        duration = (datetime.now() - run_start).total_seconds()

        stored = sum(1 for r in stats['results'] if r.get('stored'))
        classified_only = stats['processed'] - stored

        for r in stats['results']:
            note = '' if dry_run else (' [stored]' if r.get('stored') else ' [classified only, not stored]')
            logger.info(f"{r['subject'] or '(no subject)'} | {r['category']} ({r['confidence']:.2f}) | {r['status']}{note} | {r['reasoning']}")

        if stats['errors']:
            logger.error(f"Errors: {stats['errors']}")

        logger.info('POLL SUMMARY')
        logger.info(f"Total new: {stats['total']} | Stored (client enquiries): {stored} | Classified only (not relevant, not stored): {classified_only} | Skipped (already tracked): {stats['skipped']} | Errors: {stats['errors']}")
        logger.info(f'Duration: {duration:.2f}s')
        logger.info('=' * 70)

        if stats['errors']:
            raise SystemExit(1)

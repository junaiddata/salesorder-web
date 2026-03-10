"""
Management command: check_salesorder_margins

Checks all Open + Pending sales orders against the brand-margins API.
Any SO with an item whose margin is below the required manufacturer margin
is set to 'MD Approval Required' and a Telegram notification is sent to
the 'MD Approvals' group.

Run manually:
    python manage.py check_salesorder_margins

Run as cron (every 30 minutes):
    */30 * * * * /path/to/venv/bin/python /path/to/manage.py check_salesorder_margins \
        >> /path/to/logs/check_margins.log 2>&1# Check Sales Order Margins - every 4 minutes
*/4 * * * * cd /var/www/salesorder-web2/salesorder && /var/www/salesorder-web2/salesorder/venv/bin/python manage.py check_salesorder_margins >> /var/log/sync_check_margins.log 2>&1

Options:
    --all    Re-check all Open SOs regardless of current approval_status
             (useful for backfill; will not re-notify already-flagged SOs
              because check_salesorder_margin guards on status == 'Pending')
"""
import sys
import logging
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings

from so.models import SAPSalesorder
from so.brand_margins_service import fetch_brand_margins, check_salesorder_margin

BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'check_margins.log'
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('check_salesorder_margins')
logger.setLevel(logging.INFO)
logger.handlers = []

from logging.handlers import RotatingFileHandler
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(_console_handler)


def _send_md_approval_notification(so):
    """Send a Telegram message to the MD Approvals group for a newly flagged SO."""
    chat_id = getattr(settings, 'TELEGRAM_MD_APPROVAL_CHAT_ID', '') or ''
    chat_id = str(chat_id).strip()
    if not chat_id:
        logger.warning("TELEGRAM_MD_APPROVAL_CHAT_ID not set; skipping Telegram notification.")
        return False

    base_url = getattr(settings, 'VPS_BASE_URL', 'https://salesorder.junaidworld.com').rstrip('/')
    so_url = f"{base_url}/sapsalesorders/{so.so_number}/"
    date_str = so.posting_date.strftime('%Y-%m-%d') if so.posting_date else '—'

    # Use HTML to avoid Markdown issues with special chars in customer/salesman names
    def _esc(s):
        return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    msg = (
        f"🔴 <b>MD Approval Required</b>\n"
        f"SO: <code>{_esc(so.so_number)}</code>\n"
        f"Customer: {_esc(so.customer_name or '—')}\n"
        f"Salesman: {_esc(so.salesman_name or '—')}\n"
        f"Date: {date_str}\n"
        f"{so_url}"
    )

    from so.utils import send_md_approval_telegram

    # Use MD-specific bot token (TELEGRAM_MD_APPROVAL_BOT_TOKEN) if set
    ok, err = send_md_approval_telegram(chat_id, msg)
    if ok:
        logger.info(f"Telegram notification sent for SO {so.so_number}.")
        return True

    # If "chat not found" or "supergroup" related, try -100 prefix (Telegram supergroup format)
    if err and ("not found" in str(err).lower() or "supergroup" in str(err).lower()):
        alt_id = chat_id.lstrip('-')
        if not alt_id.startswith('100'):
            alt_id = f"-100{alt_id}"
            ok2, err2 = send_md_approval_telegram(alt_id, msg)
            if ok2:
                logger.info(f"Telegram notification sent for SO {so.so_number} (supergroup format).")
                return True
            err = err2

    logger.warning(f"Telegram failed for SO {so.so_number}: {err}")
    return False


class Command(BaseCommand):
    help = 'Check sales order margins and set MD Approval Required; send Telegram notifications.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--all',
            action='store_true',
            default=False,
            help='Re-check all Open SOs (not just Pending ones). Useful for backfill.',
        )
        parser.add_argument(
            '--test-telegram',
            action='store_true',
            default=False,
            help='Send a test message to TELEGRAM_MD_APPROVAL_CHAT_ID and exit.',
        )

    def handle(self, *args, **options):
        if options.get('test_telegram'):
            chat_id = getattr(settings, 'TELEGRAM_MD_APPROVAL_CHAT_ID', '') or ''
            chat_id = str(chat_id).strip()
            if not chat_id:
                logger.warning("TELEGRAM_MD_APPROVAL_CHAT_ID not set.")
                return
            token = getattr(settings, 'TELEGRAM_MD_APPROVAL_BOT_TOKEN', '') or ''
            if token:
                logger.info("Using TELEGRAM_MD_APPROVAL_BOT_TOKEN (separate MD Approvals bot).")
            else:
                logger.info("TELEGRAM_MD_APPROVAL_BOT_TOKEN not set; using main TELEGRAM_BOT_TOKEN.")
            from so.utils import send_md_approval_telegram
            msg = "🔴 <b>Test</b> – MD Approval Telegram is working."
            ok, err = send_md_approval_telegram(chat_id, msg)
            if ok:
                logger.info("Test message sent successfully. Check your MD Approvals group.")
            else:
                logger.warning(f"Test failed: {err}")
                if err and ("not found" in str(err).lower() or "supergroup" in str(err).lower()):
                    alt = f"-100{chat_id.lstrip('-')}"
                    ok2, err2 = send_md_approval_telegram(alt, msg)
                    if ok2:
                        logger.info(f"Worked with supergroup format: {alt}. Update your .env to use this chat_id.")
                    else:
                        logger.warning(f"Supergroup format also failed: {err2}")
            return

        check_all = options['all']

        logger.info('=' * 60)
        logger.info('Sales Order Margin Check')
        logger.info('=' * 60)

        # 1. Fetch brand margins
        brand_margins = fetch_brand_margins()
        if not brand_margins:
            logger.warning('Brand margins API returned empty or failed. Aborting.')
            return

        logger.info(f'Brand margins loaded for {len(brand_margins)} manufacturers.')

        # 2. Build queryset
        if check_all:
            qs = SAPSalesorder.objects.filter(status__in=['O', 'OPEN'])
            logger.info('Mode: --all (checking all Open SOs regardless of approval_status)')
        else:
            qs = SAPSalesorder.objects.filter(status__in=['O', 'OPEN'], approval_status='Pending')
            logger.info('Mode: default (checking Open + Pending SOs only)')

        total = qs.count()
        logger.info(f'SOs to check: {total}')

        if total == 0:
            logger.info('Nothing to check. Done.')
            return

        # 3. Run margin check and notify
        flagged = 0
        notified = 0

        for so in qs.iterator():
            changed = check_salesorder_margin(so, brand_margins)
            if changed:
                flagged += 1
                _send_md_approval_notification(so)
                notified += 1

        logger.info('-' * 60)
        logger.info(f'Checked: {total} | Newly flagged: {flagged} | Notifications sent: {notified}')
        logger.info('=' * 60)

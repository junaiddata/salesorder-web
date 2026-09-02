"""
Long-running process: holds an IMAP IDLE connection open on the Outlook/
hosted mailbox (sales@junaid.ae) and runs poll_outlook() the moment new mail
arrives -- real-time tracking instead of waiting for a scheduled poll. Also
re-polls periodically (every IDLE_REFRESH_SECS) as a safety net regardless
of any IDLE notification, since IMAP IDLE sessions must be refreshed
periodically anyway (most servers close an idle session after ~29 minutes
of inactivity per RFC 2177's recommended limit).

The IDLE/watch connection (via the `imapclient` library, which has robust
native IDLE support) is entirely separate from the connection
outlook_client.build_connection() opens to actually fetch/parse messages --
this command only ever *notices* new mail; emailagent.services.poll_outlook
does the real fetching, exactly as a scheduled poll_outlook run would.

Meant to run continuously and restart automatically on failure -- see
watch_outlook.bat + the Windows Task Scheduler entry set up alongside it.

Usage:
    python manage.py watch_outlook
"""
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from imapclient import IMAPClient

from emailagent.services import poll_outlook

BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'watch_outlook.log'
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('watch_outlook')
logger.setLevel(logging.INFO)
logger.handlers = []

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(console_handler)

IDLE_REFRESH_SECS = 25 * 60  # stay under the ~29-minute timeout most IMAP servers enforce
RECONNECT_BACKOFF_SECS = 30


def _run_poll(reason):
    try:
        stats = poll_outlook()
        logger.info(f"Poll ({reason}): total={stats['total']} processed={stats['processed']} "
                    f"skipped={stats['skipped']} errors={stats['errors']}")
    except Exception:
        logger.exception(f"poll_outlook failed (triggered by: {reason})")


class Command(BaseCommand):
    help = 'Watch the Outlook/IMAP mailbox in real time via IMAP IDLE and poll immediately on new mail'

    def handle(self, *args, **options):
        if not (settings.OUTLOOK_IMAP_HOST and settings.OUTLOOK_IMAP_USER and settings.OUTLOOK_IMAP_PASSWORD):
            raise CommandError(
                'OUTLOOK_IMAP_HOST / OUTLOOK_IMAP_USER / OUTLOOK_IMAP_PASSWORD are not configured in .env.'
            )

        logger.info('=' * 70)
        logger.info('Outlook Watcher starting (IMAP IDLE)')
        logger.info('=' * 70)

        # Catch up on anything that arrived while this watcher wasn't running.
        _run_poll('startup catch-up')

        while True:
            try:
                self._watch_loop()
            except KeyboardInterrupt:
                logger.info('Outlook Watcher stopped (Ctrl+C).')
                return
            except Exception:
                logger.exception(f"Watcher connection dropped -- reconnecting in {RECONNECT_BACKOFF_SECS}s")
                time.sleep(RECONNECT_BACKOFF_SECS)

    def _watch_loop(self):
        with IMAPClient(settings.OUTLOOK_IMAP_HOST, port=settings.OUTLOOK_IMAP_PORT, ssl=True) as client:
            client.login(settings.OUTLOOK_IMAP_USER, settings.OUTLOOK_IMAP_PASSWORD)
            client.select_folder(settings.OUTLOOK_IMAP_FOLDER, readonly=True)
            logger.info(f"Connected -- watching {settings.OUTLOOK_IMAP_USER}/{settings.OUTLOOK_IMAP_FOLDER} for new mail")

            while True:
                client.idle()
                try:
                    responses = client.idle_check(timeout=IDLE_REFRESH_SECS)
                finally:
                    client.idle_done()

                new_mail = any(item[1] in (b'EXISTS', b'RECENT') for item in responses)
                _run_poll('new mail detected' if new_mail else 'periodic idle refresh')

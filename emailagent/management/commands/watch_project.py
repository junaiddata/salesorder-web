"""
Long-running process: holds an IMAP IDLE connection open on the separate
project@junaid.ae mailbox and runs poll_project_mailbox() the moment new
mail arrives -- real-time tracking instead of waiting for a scheduled poll.
Exactly mirrors watch_outlook.py's mechanics, just pointed at the
PROJECT_IMAP_* credentials/mailbox and its own watermark.

Meant to run continuously and restart automatically on failure -- see
watch_project.bat + a Windows Task Scheduler entry set up alongside it,
same pattern as watch_outlook.bat.

Usage:
    python manage.py watch_project
"""
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from imapclient import IMAPClient

from emailagent.services import poll_project_mailbox

BASE_DIR = Path(__file__).parent.parent.parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'watch_project.log'
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('watch_project')
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
        stats = poll_project_mailbox()
        logger.info(f"Poll ({reason}): total={stats['total']} processed={stats['processed']} "
                    f"skipped={stats['skipped']} errors={stats['errors']}")
    except Exception:
        logger.exception(f"poll_project_mailbox failed (triggered by: {reason})")


class Command(BaseCommand):
    help = 'Watch the project@junaid.ae IMAP mailbox in real time via IMAP IDLE and poll immediately on new mail'

    def handle(self, *args, **options):
        if not (settings.PROJECT_IMAP_HOST and settings.PROJECT_IMAP_USER and settings.PROJECT_IMAP_PASSWORD):
            raise CommandError(
                'PROJECT_IMAP_HOST / PROJECT_IMAP_USER / PROJECT_IMAP_PASSWORD are not configured in .env.'
            )

        logger.info('=' * 70)
        logger.info('Project Mailbox Watcher starting (IMAP IDLE)')
        logger.info('=' * 70)

        # Catch up on anything that arrived while this watcher wasn't running.
        _run_poll('startup catch-up')

        while True:
            try:
                self._watch_loop()
            except KeyboardInterrupt:
                logger.info('Project Mailbox Watcher stopped (Ctrl+C).')
                return
            except Exception:
                logger.exception(f"Watcher connection dropped -- reconnecting in {RECONNECT_BACKOFF_SECS}s")
                time.sleep(RECONNECT_BACKOFF_SECS)

    def _watch_loop(self):
        with IMAPClient(settings.PROJECT_IMAP_HOST, port=settings.PROJECT_IMAP_PORT, ssl=True) as client:
            client.login(settings.PROJECT_IMAP_USER, settings.PROJECT_IMAP_PASSWORD)
            client.select_folder(settings.PROJECT_IMAP_FOLDER, readonly=True)
            logger.info(f"Connected -- watching {settings.PROJECT_IMAP_USER}/{settings.PROJECT_IMAP_FOLDER} for new mail")

            while True:
                client.idle()
                try:
                    responses = client.idle_check(timeout=IDLE_REFRESH_SECS)
                finally:
                    client.idle_done()

                new_mail = any(item[1] in (b'EXISTS', b'RECENT') for item in responses)
                _run_poll('new mail detected' if new_mail else 'periodic idle refresh')

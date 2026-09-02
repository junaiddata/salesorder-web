"""
Starts or renews the Gmail push notification subscription (Pub/Sub watch) so
new mail triggers emailagent/views.py:gmail_push_webhook immediately instead
of waiting for the next scheduled poll_gmail run.

Gmail watch() subscriptions expire after ~7 days -- re-run this on a
recurring schedule (e.g. daily, via Windows Task Scheduler) to keep it alive.
Requires GMAIL_PUBSUB_TOPIC to be set in .env (see settings.py).

Usage:
    python manage.py watch_gmail
    python manage.py watch_gmail --stop
"""
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from emailagent import gmail_client


class Command(BaseCommand):
    help = 'Start or renew the Gmail push notification subscription (Pub/Sub watch)'

    def add_arguments(self, parser):
        parser.add_argument('--stop', action='store_true',
                             help='Cancel the current watch instead of renewing it')

    def handle(self, *args, **options):
        service = gmail_client.build_service()

        if options['stop']:
            gmail_client.stop_watch(service)
            self.stdout.write(self.style.SUCCESS('Gmail push notifications stopped.'))
            return

        if not settings.GMAIL_PUBSUB_TOPIC:
            raise CommandError(
                'GMAIL_PUBSUB_TOPIC is not set in .env '
                '(e.g. projects/<gcp-project-id>/topics/<topic-name>).'
            )

        result = gmail_client.watch_mailbox(service, settings.GMAIL_PUBSUB_TOPIC)
        expires_at = datetime.fromtimestamp(int(result['expiration']) / 1000)
        self.stdout.write(self.style.SUCCESS(
            f"Watching inbox for push notifications (historyId={result['historyId']}, "
            f"expires {expires_at:%Y-%m-%d %H:%M:%S} -- re-run this command before then)."
        ))

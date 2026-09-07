"""Plain-IMAP auth + fetch + MIME parsing for the Outlook/hosted mailbox
(e.g. sales@junaid.ae) -- no DB access here. Mirrors gmail_client.py's
contract (build a connection, list new message ids since a watermark, fetch
one, parse it into the same plain-dict shape) so emailagent/services.py can
drive either provider through the same downstream pipeline.

Unlike the Gmail API, a full IMAP FETCH already returns the whole message
(including attachment bytes) in one call -- there's no separate lazy
"fetch this one attachment" step to mirror.
"""
import email
import imaplib
import logging
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.utils import timezone

from .gmail_client import _html_to_text, clean_body_text

logger = logging.getLogger(__name__)


def build_connection(host=None, port=None, user=None, password=None, folder=None):
    """Defaults to the primary Outlook/IMAP mailbox (OUTLOOK_IMAP_*) when no
    args are given, same as before -- pass the PROJECT_IMAP_* settings
    instead to connect to the separate project@junaid.ae mailbox (see
    emailagent.services.poll_project_mailbox). Both mailboxes share this one
    connect/fetch/parse implementation; only the credentials differ."""
    host = host if host is not None else settings.OUTLOOK_IMAP_HOST
    port = port if port is not None else settings.OUTLOOK_IMAP_PORT
    user = user if user is not None else settings.OUTLOOK_IMAP_USER
    password = password if password is not None else settings.OUTLOOK_IMAP_PASSWORD
    folder = folder if folder is not None else settings.OUTLOOK_IMAP_FOLDER

    if not (host and user and password):
        raise RuntimeError(
            "IMAP credentials are not configured. Set the *_IMAP_HOST, *_IMAP_PORT, "
            "*_IMAP_USER and *_IMAP_PASSWORD settings in .env for this mailbox."
        )
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        raise RuntimeError(f"IMAP login failed for {user} -- check its IMAP password: {e}")
    status, _ = conn.select(folder, readonly=True)
    if status != 'OK':
        raise RuntimeError(f"Could not select IMAP folder '{folder}' for {user}")
    return conn


def close_connection(conn):
    try:
        conn.close()
    except Exception:
        pass
    try:
        conn.logout()
    except Exception:
        pass


def get_current_uid_watermark(conn) -> int:
    """Highest UID currently in the mailbox. Used both as the "nothing to
    do" result of a poll and as the safe starting point for a mailbox that
    has never been polled before -- this account already holds 40k+
    historical messages, so a first run must NOT try to classify all of
    them; it only starts tracking mail that arrives from here on."""
    status, data = conn.uid('search', None, 'ALL')
    if status != 'OK':
        raise RuntimeError(f"IMAP UID SEARCH failed: {status}")
    uids = data[0].split()
    return int(uids[-1]) if uids else 0


def list_new_message_uids(conn, last_uid: str, max_results=None):
    """Returns (uids, new_last_uid, is_first_run).

    IMAP UIDs are monotonically increasing within a mailbox's UIDVALIDITY,
    so "UID last_uid+1:*" plays the same incremental-sync role as Gmail's
    historyId diff. On a genuine first run (no watermark yet) this does NOT
    walk the whole mailbox -- it jumps the watermark to the current max UID
    and returns no messages, unless max_results is given, in which case it
    returns just the most recent max_results messages (useful for an
    initial small-scale test) while still moving the watermark to "now".
    """
    if not last_uid:
        current_max = get_current_uid_watermark(conn)
        if not max_results:
            return [], str(current_max), True
        status, data = conn.uid('search', None, 'ALL')
        if status != 'OK':
            raise RuntimeError(f"IMAP UID SEARCH failed: {status}")
        all_uids = [int(u) for u in data[0].split()]
        return all_uids[-max_results:], str(current_max), True

    status, data = conn.uid('search', None, f'UID {int(last_uid) + 1}:*')
    if status != 'OK':
        raise RuntimeError(f"IMAP UID SEARCH failed: {status}")
    # RFC 3501: "x:*" with nothing >= x still returns x itself -- drop it.
    uids = [u for u in (int(u) for u in data[0].split()) if u > int(last_uid)]
    if max_results:
        uids = uids[:max_results]
    new_last_uid = str(uids[-1]) if uids else last_uid
    return uids, new_last_uid, False


def fetch_message(conn, uid):
    status, data = conn.uid('fetch', str(uid), '(RFC822)')
    if status != 'OK' or not data or data[0] is None:
        raise RuntimeError(f"IMAP FETCH failed for UID {uid}: {status}")
    msg = email.message_from_bytes(data[0][1])
    msg['X-Local-Imap-Uid'] = str(uid)
    return msg


def _decode_mime_words(value: str) -> str:
    if not value:
        return ''
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse_addresses(header_value: str):
    return [{'name': name, 'email': addr} for name, addr in email.utils.getaddresses([header_value or '']) if addr]


def _handle_part(part, body_text_parts, body_html_parts, attachments):
    if part.is_multipart():
        return
    content_type = part.get_content_type()
    disposition = (part.get_content_disposition() or '').lower()
    filename = part.get_filename()
    if filename:
        filename = _decode_mime_words(filename)

    is_attachment = bool(filename) and (disposition == 'attachment' or content_type not in ('text/plain', 'text/html'))
    if is_attachment:
        payload = part.get_payload(decode=True) or b''
        content_id = (part.get('Content-ID') or '').strip('<>')
        attachments.append({
            'filename': filename,
            'content_type': content_type,
            'size_bytes': len(payload),
            'content_id': content_id,
            'data': payload,
        })
        return

    if disposition == 'attachment':
        return

    payload = part.get_payload(decode=True)
    if payload is None:
        return
    charset = part.get_content_charset() or 'utf-8'
    try:
        text = payload.decode(charset, errors='replace')
    except LookupError:
        text = payload.decode('utf-8', errors='replace')

    if content_type == 'text/plain':
        body_text_parts.append(text)
    elif content_type == 'text/html':
        body_html_parts.append(text)


def parse_message(msg, mailbox_user=None) -> dict:
    body_text_parts, body_html_parts, attachments = [], [], []
    if msg.is_multipart():
        for part in msg.walk():
            _handle_part(part, body_text_parts, body_html_parts, attachments)
    else:
        _handle_part(msg, body_text_parts, body_html_parts, attachments)

    body_text = '\n'.join(body_text_parts).strip()
    body_html = '\n'.join(body_html_parts).strip()
    if not body_text and body_html:
        body_text = _html_to_text(body_html)
    body_text = clean_body_text(body_text)

    from_addresses = _parse_addresses(_decode_mime_words(msg.get('From', '')))
    sender_name = from_addresses[0]['name'] if from_addresses else ''
    sender = from_addresses[0]['email'] if from_addresses else ''

    uid = msg.get('X-Local-Imap-Uid', '')
    message_id = (msg.get('Message-ID') or '').strip()
    if not message_id:
        # Extremely rare for real mail, but guards the unique constraint --
        # ties the fallback id to this mailbox + UID so it's still stable.
        message_id = f'<no-message-id-uid-{uid}@{mailbox_user or settings.OUTLOOK_IMAP_USER}>'

    date_header = msg.get('Date')
    received_at = None
    if date_header:
        try:
            received_at = parsedate_to_datetime(date_header)
            if received_at and timezone.is_naive(received_at):
                received_at = timezone.make_aware(received_at, timezone.get_current_timezone())
        except (TypeError, ValueError):
            received_at = None
    if not received_at:
        received_at = timezone.now()

    raw_headers = [{'name': k, 'value': _decode_mime_words(v)} for k, v in msg.items()]

    return {
        'gmail_message_id': message_id[:255],
        # No native conversation/thread id over IMAP -- left blank. Reply
        # linking for this source relies entirely on the In-Reply-To/
        # References header match already done in
        # services._find_quotation_by_reply_headers, which works off
        # standard email headers regardless of provider.
        'thread_id': '',
        # Zimbra's IMAP UID is also its internal item id -- stored so
        # views.open_webmail can deep-link straight to this exact message
        # via Zimbra's REST content servlet instead of just opening the
        # inbox (see that view's docstring).
        'imap_uid': uid,
        'sender': sender,
        'sender_name': sender_name,
        'to': _parse_addresses(_decode_mime_words(msg.get('To', ''))),
        'cc': _parse_addresses(_decode_mime_words(msg.get('Cc', ''))),
        'bcc': _parse_addresses(_decode_mime_words(msg.get('Bcc', ''))),
        'subject': _decode_mime_words(msg.get('Subject', '')),
        'body_text': body_text,
        'body_html': body_html,
        'received_at': received_at,
        'raw_headers': raw_headers,
        'attachments': attachments,
    }

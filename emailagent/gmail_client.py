"""Gmail API auth + fetch + MIME parsing only -- no DB access here.

Mirrors so/api_client.py's convention: this module fetches and maps raw API
responses into plain dicts; persistence lives in emailagent/services.py.
"""
import base64
import logging
import re
from email.utils import getaddresses

from django.conf import settings
from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'[ \t]+')


def get_credentials():
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GMAIL_REFRESH_TOKEN):
        raise RuntimeError(
            "Gmail credentials are not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET "
            "and GMAIL_REFRESH_TOKEN in .env (run get_gmail_refresh_token.py to obtain the refresh token)."
        )
    return Credentials(
        token=None,
        refresh_token=settings.GMAIL_REFRESH_TOKEN,
        token_uri=settings.GMAIL_TOKEN_URI,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=settings.GMAIL_SCOPES,
    )


def build_service():
    try:
        service = build('gmail', 'v1', credentials=get_credentials(), cache_discovery=False)
        # build() never touches the network -- credentials refresh lazily on
        # the first real API call. Make that call here so an invalid/expired
        # refresh token surfaces as the clear RuntimeError below instead of a
        # raw RefreshError from whatever call happens to run first.
        service.users().getProfile(userId='me').execute()
        return service
    except RefreshError as e:
        raise RuntimeError(f"Gmail refresh token is invalid/expired -- re-run get_gmail_refresh_token.py: {e}")


def get_current_history_id(service) -> str:
    profile = service.users().getProfile(userId='me').execute()
    return str(profile['historyId'])


def watch_mailbox(service, topic_name: str) -> dict:
    """Subscribes the mailbox to Gmail push notifications on the given
    Pub/Sub topic -- Gmail then publishes to that topic (which our webhook's
    push subscription forwards to us) whenever INBOX changes, instead of us
    having to wait for the next scheduled poll. Expires after ~7 days; must
    be renewed before then (see management/commands/watch_gmail.py)."""
    return service.users().watch(userId='me', body={
        'topicName': topic_name,
        'labelIds': ['INBOX'],
        'labelFilterAction': 'include',
    }).execute()


def stop_watch(service) -> None:
    service.users().stop(userId='me').execute()


def list_new_message_ids(service, last_history_id: str, fallback_query: str = None):
    """Returns (message_ids, new_history_id, used_fallback).

    Primary path: incremental diff via users.history.list(startHistoryId=...).
    Falls back to users.messages.list(q=...) if there's no watermark yet, or
    if Gmail has expired the given historyId (typically after ~7 days of
    mailbox inactivity relative to that id), which surfaces as HTTP 404.
    """
    if last_history_id:
        try:
            return _list_via_history(service, last_history_id)
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning("Gmail historyId %s expired, falling back to messages.list", last_history_id)
            else:
                raise

    message_ids = _list_via_query(service, fallback_query or 'in:inbox')
    new_history_id = get_current_history_id(service)
    return message_ids, new_history_id, True


def _list_via_history(service, start_history_id: str):
    message_ids = []
    page_token = None
    new_history_id = start_history_id
    while True:
        resp = service.users().history().list(
            userId='me',
            startHistoryId=start_history_id,
            historyTypes=['messageAdded'],
            pageToken=page_token,
        ).execute()
        for record in resp.get('history', []):
            for added in record.get('messagesAdded', []):
                msg = added.get('message', {})
                if 'INBOX' in (msg.get('labelIds') or []):
                    message_ids.append(msg['id'])
        if 'historyId' in resp:
            new_history_id = str(resp['historyId'])
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    # de-dup while preserving order (the same message can appear in multiple history records)
    seen = set()
    deduped = [m for m in message_ids if not (m in seen or seen.add(m))]
    return deduped, new_history_id, False


def _list_via_query(service, query: str):
    message_ids = []
    page_token = None
    while True:
        resp = service.users().messages().list(userId='me', q=query, pageToken=page_token).execute()
        message_ids.extend(m['id'] for m in resp.get('messages', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return message_ids


def fetch_message(service, message_id: str) -> dict:
    return service.users().messages().get(userId='me', id=message_id, format='full').execute()


def fetch_attachment_bytes(service, message_id: str, attachment_id: str) -> bytes:
    resp = service.users().messages().attachments().get(
        userId='me', messageId=message_id, id=attachment_id,
    ).execute()
    return decode_base64url(resp['data'])


def decode_base64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode('ascii') + b'=' * (-len(data) % 4))


def _header_value(headers, name):
    for h in headers:
        if h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''


def _parse_addresses(header_value):
    return [{'name': name, 'email': email} for name, email in getaddresses([header_value]) if email]


def _html_to_text(html: str) -> str:
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', html)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</p>', '\n\n', text)
    text = _TAG_RE.sub('', text)
    text = _WS_RE.sub(' ', text)
    return text.strip()


_ANGLE_LINK_RE = re.compile(r'<(?:mailto:|https?://)[^>]*>')
_DUP_ANGLE_RE = re.compile(r'(\S+)[ \t]+<\1[ \t]*>')


def clean_body_text(text: str) -> str:
    """Outlook's auto-generated plain-text export duplicates every hyperlink
    as a bracketed URL right after the visible text/icon (mailto: links,
    social-icon links with no visible text, 'name@x.com <name@x.com>' address
    echoes) -- strip those, then collapse the blank-line runs they leave
    behind so the body reads cleanly."""
    text = _ANGLE_LINK_RE.sub('', text)
    text = _DUP_ANGLE_RE.sub(r'\1', text)
    lines = [line.rstrip() for line in text.split('\n')]
    cleaned_lines = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                cleaned_lines.append('')
        else:
            blank_run = 0
            cleaned_lines.append(line)
    return '\n'.join(cleaned_lines).strip()


_CHARSET_RE = re.compile(r'charset=["\']?([\w-]+)', re.IGNORECASE)


def _part_charset(headers) -> str:
    content_type = _header_value(headers, 'Content-Type')
    match = _CHARSET_RE.search(content_type)
    return match.group(1) if match else 'utf-8'


def _decode_part_text(data: str, headers) -> str:
    charset = _part_charset(headers)
    raw = decode_base64url(data)
    try:
        return raw.decode(charset, errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


def _walk_parts(payload, body_text_parts, body_html_parts, attachments):
    mime_type = payload.get('mimeType', '')
    filename = payload.get('filename', '')
    body = payload.get('body', {})
    headers = payload.get('headers', [])

    if filename:
        content_id = _header_value(headers, 'Content-ID').strip('<>')
        attachments.append({
            'filename': filename,
            'content_type': mime_type,
            'size_bytes': body.get('size', 0),
            'attachment_id': body.get('attachmentId', ''),
            'inline_data': body.get('data'),
            'content_id': content_id,
        })
    elif mime_type == 'text/plain' and body.get('data'):
        body_text_parts.append(_decode_part_text(body['data'], headers))
    elif mime_type == 'text/html' and body.get('data'):
        body_html_parts.append(_decode_part_text(body['data'], headers))

    for part in payload.get('parts', []) or []:
        _walk_parts(part, body_text_parts, body_html_parts, attachments)


def parse_message(raw_message: dict) -> dict:
    payload = raw_message.get('payload', {})
    headers = payload.get('headers', [])

    body_text_parts, body_html_parts, attachments = [], [], []
    _walk_parts(payload, body_text_parts, body_html_parts, attachments)

    body_text = '\n'.join(body_text_parts).strip()
    body_html = '\n'.join(body_html_parts).strip()
    if not body_text and body_html:
        body_text = _html_to_text(body_html)
    body_text = clean_body_text(body_text)

    from_addresses = _parse_addresses(_header_value(headers, 'From'))
    sender_name = from_addresses[0]['name'] if from_addresses else ''
    sender = from_addresses[0]['email'] if from_addresses else ''

    internal_date_ms = int(raw_message.get('internalDate', 0))
    received_at = timezone.datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.get_current_timezone()) \
        if internal_date_ms else timezone.now()

    return {
        'gmail_message_id': raw_message['id'],
        'thread_id': raw_message.get('threadId', ''),
        'imap_uid': '',  # Gmail has its own deep link (see email_detail.html) -- unused here.
        'sender': sender,
        'sender_name': sender_name,
        'to': _parse_addresses(_header_value(headers, 'To')),
        'cc': _parse_addresses(_header_value(headers, 'Cc')),
        'bcc': _parse_addresses(_header_value(headers, 'Bcc')),  # near-always empty for received mail
        'subject': _header_value(headers, 'Subject'),
        'body_text': body_text,
        'body_html': body_html,
        'received_at': received_at,
        'raw_headers': headers,
        'attachments': attachments,
    }

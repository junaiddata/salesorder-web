"""Saves a copy of an outgoing email into the sending account's IMAP Sent
folder.

Sending is SMTP; the Sent folder is IMAP. They are different protocols against
different services, and SMTP puts nothing in your own mailbox -- a message only
appears in Sent because a mail client explicitly APPENDs a copy there right
after sending. Django's EmailMessage.send() does the SMTP half only, so before
this module every quotation and submittal we emailed was invisible in
sales@junaid.ae's Sent folder even though the client received it. (Gmail's SMTP
is the exception that makes this surprising -- Google copies to "Sent Mail"
server-side, so anyone whose mental model comes from Gmail expects it to be
automatic. smtp.emailapps.net does not.)

Deliberately NOT done by BCC-ing ourselves, the other common trick: a BCC lands
in INBOX, which is exactly the folder emailagent polls, so the classifier would
pick up our own outgoing quotation and try to read it as a customer enquiry.
An APPEND to Sent is invisible to the poller.

Every function here is best-effort and never raises: the message has already
been delivered to the client by the time this runs, so a failure to file a copy
must never surface as a send failure -- a caller that treated it as one would
prompt a re-send and the client would receive the quotation twice.
"""
import imaplib
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# Resolved once per process -- the folder name cannot change under us, and
# LIST on every send would be a pointless round trip. None = not yet looked up.
_sent_folder_cache = None

# Checked in order when the server advertises no \Sent special-use folder.
# Plain 'Sent' is Zimbra's default; the INBOX-prefixed spellings cover servers
# that put every folder under an INBOX namespace.
_SENT_FOLDER_FALLBACKS = ('Sent', 'INBOX.Sent', 'INBOX/Sent', 'Sent Items', 'Sent Messages')


def _config():
    """(host, port, user, password, folder_override) for the mailbox that
    copies are filed into. Defaults to the OUTLOOK_IMAP_* mailbox because that
    is the account we send as (EMAIL_HOST_USER) -- the SENT_IMAP_* settings
    exist only for a deployment where those two differ."""
    return (
        getattr(settings, 'SENT_IMAP_HOST', '') or settings.OUTLOOK_IMAP_HOST,
        getattr(settings, 'SENT_IMAP_PORT', 0) or settings.OUTLOOK_IMAP_PORT,
        getattr(settings, 'SENT_IMAP_USER', '') or settings.OUTLOOK_IMAP_USER,
        getattr(settings, 'SENT_IMAP_PASSWORD', '') or settings.OUTLOOK_IMAP_PASSWORD,
        getattr(settings, 'SENT_IMAP_FOLDER', ''),
    )


def _quote_folder(name):
    r"""IMAP-quote a mailbox name. Folder names reach APPEND as a quoted
    string, so a name containing a space ("Sent Items") or a quote/backslash
    has to be escaped or the command is misparsed as extra arguments."""
    escaped = name.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _discover_sent_folder(conn):
    """The server's own Sent folder, from its LIST response.

    Asks rather than assumes: APPEND to a name the server doesn't have will,
    on many servers, silently CREATE that folder, leaving a second empty
    "Sent" sitting beside the real one and copies filed where nobody looks.
    RFC 6154's \\Sent special-use attribute is the authoritative answer (this
    server does advertise it); the name fallbacks below are only for servers
    that don't."""
    try:
        status, rows = conn.list()
    except Exception:
        logger.exception("IMAP LIST failed while looking for the Sent folder")
        return _SENT_FOLDER_FALLBACKS[0]

    if status != 'OK' or not rows:
        return _SENT_FOLDER_FALLBACKS[0]

    parsed = []
    for row in rows:
        if not row:
            continue
        line = row.decode('utf-8', 'replace') if isinstance(row, bytes) else str(row)
        # (\HasNoChildren \Sent) "/" "Sent"  ->  attributes, then the name last
        attrs = line[line.find('(') + 1:line.find(')')].lower() if '(' in line else ''
        name = line.rsplit('"', 2)[-2] if line.count('"') >= 2 else line.split()[-1]
        parsed.append((attrs, name))
        if '\\sent' in attrs:
            return name

    by_name = {name.lower(): name for _, name in parsed}
    for candidate in _SENT_FOLDER_FALLBACKS:
        if candidate.lower() in by_name:
            return by_name[candidate.lower()]
    return _SENT_FOLDER_FALLBACKS[0]


def save_to_sent(message, sent_at=None):
    """APPENDs `message` (a Django EmailMessage, or anything with .message()
    or .as_bytes()) into the sending account's Sent folder.

    Returns True when the copy was filed, False otherwise -- callers should
    record the result but must NOT treat False as a send failure: by the time
    this runs the recipient already has the mail (see the module docstring).

    `sent_at` is the message's INTERNALDATE, i.e. the timestamp the folder
    sorts and displays by. Pass the moment the mail actually went out; it
    defaults to now, which is a second or two later.
    """
    global _sent_folder_cache

    host, port, user, password, folder_override = _config()
    if not (host and user and password):
        logger.warning(
            "Not saving a copy to Sent: IMAP credentials are not configured "
            "(set OUTLOOK_IMAP_* or SENT_IMAP_* in .env)."
        )
        return False

    try:
        raw = message.message().as_bytes() if hasattr(message, 'message') else message.as_bytes()
    except Exception:
        logger.exception("Could not serialise the outgoing email for the Sent folder")
        return False

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)

        folder = folder_override or _sent_folder_cache or _discover_sent_folder(conn)
        if not folder_override:
            _sent_folder_cache = folder

        # No SELECT first: APPEND names its target mailbox directly, which is
        # also why the pollers' readonly INBOX connection is irrelevant here.
        # \Seen because this is our own outgoing mail -- without it every
        # quotation we send shows up as unread in Sent.
        status, response = conn.append(
            _quote_folder(folder), r'(\Seen)',
            imaplib.Time2Internaldate(sent_at or time.time()),
            raw,
        )
        if status != 'OK':
            logger.error("IMAP APPEND to '%s' for %s returned %s: %r", folder, user, status, response)
            return False
        return True
    except Exception:
        logger.exception("Could not save a copy of the outgoing email to '%s' for %s",
                         folder_override or _sent_folder_cache or 'Sent', user)
        return False
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass

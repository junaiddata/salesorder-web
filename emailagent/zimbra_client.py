"""webmail.emailapps.net (Zimbra) deep-link helper -- used only by
views.open_webmail to jump straight to one already-fetched message's
original content instead of just opening the inbox. Zimbra's classic web
client never puts the open message's identity in the browser URL during
normal use (clicking through the inbox always shows the same #1 fragment),
so there is no link a human could ever copy out of it either -- this
authenticates fresh via Zimbra's own SOAP AuthRequest API instead, using
the same mailbox credentials outlook_client.py already uses over IMAP.
"""
import logging
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

WEBMAIL_BASE = 'https://webmail.emailapps.net'
_SOAP_URL = f'{WEBMAIL_BASE}/service/soap'
_REQUEST_TIMEOUT_SECS = 10


def get_auth_token(username: str, password: str) -> str:
    """Zimbra SOAP AuthRequest -- returns a fresh authToken for this
    mailbox (Zimbra auth tokens expire after some hours, so this is called
    fresh on every "Open in Webmail" click rather than cached). Raises
    RuntimeError on any failure (bad/rotated credentials, Zimbra
    unreachable, unexpected response shape) so the caller can fall back to
    the plain login page instead of a broken redirect."""
    payload = {
        'Body': {
            'AuthRequest': {
                '_jsns': 'urn:zimbraAccount',
                'account': {'by': 'name', '_content': username},
                'password': {'_content': password},
            }
        }
    }
    try:
        resp = requests.post(_SOAP_URL, json=payload, timeout=_REQUEST_TIMEOUT_SECS)
        resp.raise_for_status()
        token = resp.json()['Body']['AuthResponse']['authToken'][0]['_content']
    except Exception as exc:
        raise RuntimeError(f"Zimbra AuthRequest failed: {exc}") from exc
    if not token:
        raise RuntimeError("Zimbra AuthRequest returned no authToken")
    return token


def message_url(auth_token: str, imap_uid: str) -> str:
    """Direct link to one message's original content. `id` is Zimbra's own
    internal item id, which on this host is the same value as the IMAP UID
    (see outlook_client.parse_message's `imap_uid`). `auth=qp` tells Zimbra
    to read the auth token from the query string rather than requiring a
    pre-existing session cookie, so this one URL is self-authenticating --
    no separate login step happens in the browser at all.

    Deliberately the /service/home/~/ REST content servlet, NOT
    /h/printmessage (Zimbra's "Print" view) -- confirmed by hand that
    /h/printmessage 500s with auth=qp (it's a different, JSP-based app
    that doesn't accept the query-string token the way the REST servlet
    does), so it can only ever be opened by someone already holding a
    Zimbra session cookie, which defeats the point here. This REST
    servlet is the one endpoint confirmed to accept auth=qp -- the
    tradeoff is it always returns the message's raw RFC822 source (every
    MIME header/boundary included) rather than a rendered view; `view=`
    does not control that despite the name (both `text` and `html` were
    tried and returned byte-identical output). The rendered body and any
    attachments are shown on our own email_detail.html page instead
    (body_html/body_text and the Attachments section), which is why this
    link exists mainly as a "verify against the source mailbox" option
    rather than the primary way to read the email."""
    return (
        f'{WEBMAIL_BASE}/service/home/~/'
        f'?auth=qp&zauthtoken={quote(auth_token, safe="")}'
        f'&id={quote(str(imap_uid), safe="")}'
    )

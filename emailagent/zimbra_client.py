"""webmail.emailapps.net (Zimbra) deep-link helper -- used only by
views.open_webmail to land straight on the mailbox's own Inbox, already
logged in, instead of the plain login page. Authenticates fresh via
Zimbra's own SOAP AuthRequest API, using the same mailbox credentials
outlook_client.py already uses over IMAP.
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


def inbox_url(auth_token: str) -> str:
    """Auto-authenticated link straight into the real Modern web client,
    landing on this mailbox's own Inbox already logged in -- confirmed by
    hand (via a live request using this app's own configured mailbox
    credentials): the plain root URL with `auth=qp&zauthtoken=` set on it
    returns Zimbra's actual app shell (not the login form -- no
    login_csrf/zLoginForm present), WITH real ZM_AUTH_TOKEN/JSESSIONID/
    cookiesession1 cookies in the response, and the account's own address
    and a "Sign Out" link visible in the page. `auth=qp` tells Zimbra to
    read the auth token from the query string rather than requiring a
    pre-existing session cookie, so this one URL is self-authenticating --
    no separate login step happens in the browser at all.

    Two things were tried and confirmed NOT to work before landing on
    this, both against this exact server:
      - The /service/home/~/ REST content servlet (auth=qp DOES work
        there) pointed at the Inbox *folder* instead of one message id --
        returns an empty 200 response by default, and "No HTML formatter
        available for item" with `&fmt=html` added. That servlet is only
        good for one exact message's raw RFC822 source (see git history),
        never a rendered listing.
      - /h/search (Zimbra's classic-client search/mail-list URL on other
        deployments) -- 404s outright; this server has no classic client
        installed, Modern-only.
    """
    return f'{WEBMAIL_BASE}/?auth=qp&zauthtoken={quote(auth_token, safe="")}'

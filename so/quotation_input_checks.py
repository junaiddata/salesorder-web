"""
Checks for the details typed by hand on a NEW quotation: customer contact number, customer email, customer display name.

Each ``validate_*`` function returns an error message (str) when the value is not acceptable, or '' when it is fine.
An empty value is NOT judged here -- "is it required" stays the caller's business (the views). The same rules are
mirrored in the browser by ``so/templates/so/_input_checks_js.html``; ``so/tests_input_checks.py`` keeps the two in step.

The rules are deliberately tolerant of how real customers write things (mobile and landline numbers, "+971", dashes,
two numbers in one box, extensions) and strict only about content that is plainly not real.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

PHONE_MESSAGE = 'Enter a valid phone number, for example 0501234567 or 04-3362775 (digits only, no letters).'
PHONE_FAKE_MESSAGE = "That phone number doesn't look real. Enter the customer's actual mobile or landline number."
EMAIL_MESSAGE = 'Enter a valid email address, for example name@company.com.'
EMAIL_FAKE_MESSAGE = "That email address doesn't look real. Enter the customer's actual email address."
NAME_MESSAGE = 'Enter the real customer name (at least 3 letters), for example "Mr Ahmed" or "Al Noor Trading".'
NAME_FAKE_MESSAGE = "That name doesn't look real. Enter who the customer actually is."

# ── contact number ─────────────────────────────────────────────────────────
PHONE_MIN_DIGITS = 7            # shortest real number (older UAE landline without area code)
PHONE_MAX_DIGITS = 20           # one box may hold two numbers joined by a dash (04-3403911-3406733); one real number is at most 15
PHONE_MAX_RUN = 9               # 9+ digits in a straight ascending / descending run (1234567890, 0123456789) is not a real number
_EXT_RE = re.compile(r'\b(?:ext|extn|extension|ex|x)\.?\s*\d{1,6}\b', re.I)
_PHONE_CHARS_RE = re.compile(r'^[0-9+\-/,;().\s]+$')
_SEPARATOR_RE = re.compile(r'[/,;]')


def _looks_fake_digits(d):
    n = len(d)
    if n < PHONE_MIN_DIGITS:
        return False
    if max(d.count(c) for c in set(d)) >= max(6, n - 2):          # 0000000, 0555555555, 0500000000
        return True
    for k in range(2, min(5, n // 2) + 1):                         # 123123123, 0505050505
        if n % k == 0 and d == d[:k] * (n // k):
            return True
    for step in (1, -1):                                           # 1234567890, 9876543210
        run = 1
        for a, b in zip(d, d[1:]):
            run = run + 1 if int(b) - int(a) == step else 1
            if run >= PHONE_MAX_RUN:
                return True
    return False


def validate_contact_number(value):
    value = (value or '').strip()
    if not value:
        return ''
    value = _EXT_RE.sub(' ', value)
    if not _PHONE_CHARS_RE.match(value):
        return PHONE_MESSAGE
    segments = [s.strip() for s in _SEPARATOR_RE.split(value) if s.strip()]
    if not segments:
        return PHONE_MESSAGE
    for i, seg in enumerate(segments):
        if '+' in seg[1:]:
            return PHONE_MESSAGE
        digits = re.sub(r'\D', '', seg)
        if i == 0:
            if not PHONE_MIN_DIGITS <= len(digits) <= PHONE_MAX_DIGITS:
                return PHONE_MESSAGE
        elif not 2 <= len(digits) <= PHONE_MAX_DIGITS:             # "04-2611164/67": the second part may be short
            return PHONE_MESSAGE
        if _looks_fake_digits(digits):
            return PHONE_FAKE_MESSAGE
    return ''


# ── email ──────────────────────────────────────────────────────────────────
EMAIL_MAX_LENGTH = 254
JUNK_EMAIL_DOMAINS = frozenset({
    'test.com', 'abc.com', 'xyz.com', 'xxx.com', 'aaa.com', 'asd.com', 'asdf.com', 'a.com', 'no.com', 'na.com',
    'none.com', 'nomail.com', 'noemail.com', 'email.com', 'domain.com', 'yourdomain.com', 'company.com',
    'sample.com', 'demo.com', 'example.com', 'example.org', 'example.net',
})
JUNK_EMAIL_TLDS = frozenset({'local', 'test', 'invalid', 'example', 'localhost'})
JUNK_EMAIL_LOCALS = frozenset({
    'test', 'testing', 'abc', 'abcd', 'xyz', 'xxx', 'na', 'none', 'no', 'noemail', 'nomail', 'asd', 'asdf', 'aaa',
    'demo', 'sample', 'name', 'customer', 'nil', 'null', 'dummy',
})


def _email_candidates(value):
    """Addresses found in the box. Customers write several, joined by , ; / | or a dash ("a@x.ae-b@y.ae"), and a few add
    a name after them, so: split on , ; / | and spaces, drop quotes / dots / invisible characters at the ends, ignore
    pieces with no @, and cut a piece that holds several addresses at its dashes."""
    value = re.sub('[​-‏‪-‮﻿]', '', value)
    found = []
    for token in re.split(r'[,;/|\s]+', value):
        token = token.strip("'\"<>()[]:.")
        if token.count('@') > 1:
            pieces = [p for p in token.split('-') if p]
            i = 0
            while i < len(pieces):
                piece = pieces[i]
                while ('@' in piece and '.' not in piece.rpartition('@')[2] and i + 1 < len(pieces)
                       and '@' not in pieces[i + 1]):          # "a@mac-mep.com": the dash belongs to the domain
                    i += 1
                    piece += '-' + pieces[i]
                if '@' in piece:
                    found.append(piece.strip(':.'))
                i += 1
        elif '@' in token:
            found.append(token)
    return found


def validate_customer_email(value):
    """Several addresses may be given (see _email_candidates) -- each one is checked."""
    value = (value or '').strip()
    if not value:
        return ''
    addresses = _email_candidates(value)
    if not addresses:
        return EMAIL_MESSAGE
    for address in addresses:
        if len(address) > EMAIL_MAX_LENGTH:
            return EMAIL_MESSAGE
        try:
            validate_email(address)
        except ValidationError:
            return EMAIL_MESSAGE
        local, _, domain = address.rpartition('@')
        domain = domain.lower()
        tld = domain.rsplit('.', 1)[-1]
        if '.' not in domain or len(tld) < 2 or not tld.isalpha():
            return EMAIL_MESSAGE
        local_key = local.lower()
        if (domain in JUNK_EMAIL_DOMAINS or tld in JUNK_EMAIL_TLDS or local_key in JUNK_EMAIL_LOCALS
                or (len(local_key) >= 3 and len(set(local_key)) == 1)):
            return EMAIL_FAKE_MESSAGE
    return ''


# ── display name ───────────────────────────────────────────────────────────
NAME_MIN_LETTERS = 3
NAME_MAX_LENGTH = 100
JUNK_NAMES = frozenset({
    'test', 'testing', 'na', 'n a', 'none', 'nil', 'null', 'customer', 'name', 'cash', 'unknown', 'no name', 'noname',
    'dummy', 'sample', 'demo', 'abc', 'abcd', 'xyz', 'xxx', 'asdf', 'asdfg', 'qwe', 'qwerty', 'walk in', 'walkin',
    'walk in customer', 'walkin customer', 'cash customer', 'cash sales', 'debit customer', 'retail customer',
})
KEYBOARD_MASH = ('asdf', 'qwer', 'zxcv', 'sdfg', 'dfgh')


def validate_display_name(value):
    value = ' '.join((value or '').split())
    if not value:
        return ''
    letters = [c for c in value if c.isalpha()]
    if len(value) > NAME_MAX_LENGTH or len(letters) < NAME_MIN_LETTERS:
        return NAME_MESSAGE
    key = ' '.join(re.sub(r'[^\w\s]|_|\d', ' ', value.lower()).split())
    squashed = key.replace(' ', '')
    if (key in JUNK_NAMES or len(set(squashed)) == 1 or any(m in squashed for m in KEYBOARD_MASH)):
        return NAME_FAKE_MESSAGE
    return ''

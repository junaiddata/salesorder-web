"""Who may upload / print the company stamp on quotations."""
from django.conf import settings


def can_use_stamp(user):
    """Everyone who is logged in, unless QUOTATION_STAMP_USERS (a list of usernames) is set in settings.py."""
    if not (user and user.is_authenticated):
        return False
    allowed = {n.strip().lower() for n in (getattr(settings, 'QUOTATION_STAMP_USERS', None) or ())}
    return not allowed or (user.username or '').strip().lower() in allowed

"""Who may manage / use the company signature and stamp."""
from django.conf import settings

# Case-insensitive usernames. Override with BANGLADESH_PI_SEAL_USERS in settings.py.
DEFAULT_SEAL_USERS = ('md', 'manager')


def seal_usernames():
    names = getattr(settings, 'BANGLADESH_PI_SEAL_USERS', DEFAULT_SEAL_USERS)
    return {n.strip().lower() for n in names}


def can_use_seal(user):
    return bool(user and user.is_authenticated and (user.username or '').strip().lower() in seal_usernames())

"""Finance approval gate helpers.

Non-cash customers need finance approval before manager/MD can approve/reject.
Cash customers keep the existing flow. The whole gate is off unless
settings.FINANCE_APPROVAL_REQUIRED is True.
"""
import re

from django.conf import settings

# Statuses manager/MD set that must wait for finance
GATED_STATUSES = ('Approved', 'Rejected', 'MD Approval Required')


def is_cash_customer(salesorder):
    """True if customer name contains "(CASH)" (spacing/case tolerant), e.g. 'X LLC (CASH)', 'DEBIT CUSTOMER ( CASH )'."""
    name = (getattr(salesorder, 'customer_name', None) or '').upper()
    return re.search(r'\(\s*CASH\s*\)', name) is not None


def finance_gate_applies(salesorder):
    """Whether finance approval is required for this order."""
    return bool(getattr(settings, 'FINANCE_APPROVAL_REQUIRED', False)) and not is_cash_customer(salesorder)


def is_finance_approver(user):
    """Admin-role user whose username is in FINANCE_APPROVER_USERNAMES."""
    role = getattr(user, 'role', None)
    if not role or getattr(role, 'role', None) != 'Admin':
        return False
    return (user.username or '').strip().lower() in getattr(settings, 'FINANCE_APPROVER_USERNAMES', [])


def finance_block_message(salesorder, new_status):
    """Return an error message if setting new_status must be blocked, else None."""
    if not finance_gate_applies(salesorder):
        return None
    if new_status not in GATED_STATUSES or new_status == salesorder.approval_status:
        return None
    if salesorder.finance_status == 'Approved':
        return None
    if salesorder.finance_status == 'Rejected':
        return "Finance has rejected this order. Only Finance can approve it before it can be processed."
    return "Finance approval is required before this status can be set."

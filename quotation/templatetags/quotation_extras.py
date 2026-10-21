from django import template

from ..permissions import can_use_stamp as _can

register = template.Library()


@register.filter
def can_use_stamp(user):
    return _can(user)

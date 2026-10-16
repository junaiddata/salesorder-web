from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    return mapping.get(key, '') if mapping else ''


@register.filter
def can_use_seal(user):
    from ..permissions import can_use_seal as _can
    return _can(user)

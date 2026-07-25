from django import template

register = template.Library()


@register.filter
def dict_get(d, key):
    """Look up a dict value by a variable key (Django templates only support
    literal dict keys via dot notation) -- used for the item table's
    per-letter dynamic columns."""
    return (d or {}).get(key, '')

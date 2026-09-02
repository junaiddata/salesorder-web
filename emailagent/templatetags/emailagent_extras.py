"""Small template helper for building filter links on the Agent Activity
dashboard that change one query param while preserving every other active
filter (agent/status/days/q/view) -- without it, each filter pill would
have to hardcode every other filter's current value, and clicking one would
silently drop the rest."""
from django import template
from django.http import QueryDict

register = template.Library()


@register.simple_tag(takes_context=True)
def qs_replace(context, **kwargs):
    """Usage: {% qs_replace status='flagged' %} -- returns a querystring
    with `status` set to 'flagged', every other current GET param kept as
    is, and `page` dropped (changing a filter always resets to page 1).
    Pass a falsy value (e.g. status='') to remove that param entirely."""
    request = context.get('request')
    params = request.GET.copy() if request else QueryDict(mutable=True)
    for key, value in kwargs.items():
        if value in (None, ''):
            params.pop(key, None)
        else:
            params[key] = value
    params.pop('page', None)
    return params.urlencode()

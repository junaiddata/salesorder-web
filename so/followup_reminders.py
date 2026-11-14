"""In-app reminders for quotation follow-ups.

A follow-up can carry a ``remind_at`` date/time. Once that time has passed, the person who wrote the follow-up (and only
them) sees a reminder in the app until they dismiss or snooze it. There is no background job: the browser asks
``due`` every ~45 seconds and the server simply compares ``remind_at`` with the current time, so it works the same
under any number of web workers and nothing is lost if the server restarts.
"""
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import QuotationFollowUp, SAPQuotationFollowUp

MAX_DUE = 20                       # never flood the screen
SNOOZE_CHOICES = {15, 60, 240, 1440}   # minutes: 15 min, 1 hour, 4 hours, 1 day
KINDS = {'q': QuotationFollowUp, 'sap': SAPQuotationFollowUp}


def parse_remind_at(raw):
    """Parse the <input type="datetime-local"> value ('YYYY-MM-DDTHH:MM', Dubai time).

    Returns (aware datetime or None, error message or ''). Blank means "no reminder".
    """
    raw = (raw or '').strip()
    if not raw:
        return None, ''
    value = None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            value = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if value is None:
        return None, 'Invalid reminder date/time.'
    value = timezone.make_aware(value, timezone.get_current_timezone())
    if value <= timezone.now():
        return None, 'The reminder time must be in the future.'
    return value, ''


def remind_at_display(dt):
    """'20 Nov 2026, 10:00 AM' in local time, or '' when there is no reminder."""
    return timezone.localtime(dt).strftime('%d %b %Y, %I:%M %p') if dt else ''


def _due_rows(user):
    now = timezone.now()
    rows = []
    for kind, model in KINDS.items():
        qs = (model.objects
              .filter(created_by=user, remind_at__isnull=False, remind_at__lte=now, reminder_dismissed_at__isnull=True)
              .select_related('quotation').order_by('remind_at')[:MAX_DUE])
        for fu in qs:
            q = fu.quotation
            if kind == 'sap':
                number, customer = q.q_number, q.customer_name
                url = reverse('quotation_detail', args=[q.q_number])
            else:
                number = q.quotation_number
                customer = getattr(q, 'customer_display_name', None) or getattr(q.customer, 'customer_name', '') or ''
                url = reverse('view_quotation_details', args=[q.pk])
            rows.append({
                'kind': kind, 'id': fu.id, 'number': number, 'customer': customer or '',
                'note': (fu.note or '')[:200], 'url': url,
                'remind_at': remind_at_display(fu.remind_at), '_sort': fu.remind_at,
            })
    rows.sort(key=lambda r: r['_sort'])
    for r in rows:
        del r['_sort']
    return rows[:MAX_DUE]


@login_required
@require_GET
def due(request):
    resp = JsonResponse({'reminders': _due_rows(request.user)})
    resp['Cache-Control'] = 'no-store'
    return resp


@login_required
@require_POST
def act(request):
    """Dismiss or snooze one of the current user's own reminders."""
    model = KINDS.get(request.POST.get('kind'))
    action = request.POST.get('action')
    try:
        fu_id = int(request.POST.get('id', ''))
    except ValueError:
        fu_id = None
    if model is None or fu_id is None or action not in ('dismiss', 'snooze'):
        return JsonResponse({'success': False, 'error': 'Invalid request.'}, status=400)
    fu = model.objects.filter(pk=fu_id, created_by=request.user).first()   # only your own reminders
    if fu is None:
        return JsonResponse({'success': False, 'error': 'Reminder not found.'}, status=404)
    if action == 'dismiss':
        fu.reminder_dismissed_at = timezone.now()
        fu.save(update_fields=['reminder_dismissed_at'])
    else:
        try:
            minutes = int(request.POST.get('minutes', ''))
        except ValueError:
            minutes = 0
        if minutes not in SNOOZE_CHOICES:
            return JsonResponse({'success': False, 'error': 'Invalid snooze time.'}, status=400)
        fu.remind_at = timezone.now() + timedelta(minutes=minutes)
        fu.reminder_dismissed_at = None
        fu.save(update_fields=['remind_at', 'reminder_dismissed_at'])
    return JsonResponse({'success': True})


WIDGET_JS = r"""
(function () {
  if (window.__fuReminderLoaded) return;
  window.__fuReminderLoaded = true;
  var DUE_URL = "__DUE__", ACT_URL = "__ACT__", POLL_MS = 45000;
  var shown = {}, baseTitle = document.title, box = null;

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var i = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return i ? i.value : '';
  }
  function el(tag, css, text) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text) e.textContent = text;
    return e;
  }
  function ensureBox() {
    if (box && document.body.contains(box)) return box;
    box = el('div', 'position:fixed;right:16px;bottom:16px;z-index:99999;display:flex;flex-direction:column;gap:10px;' +
      'max-width:360px;width:calc(100vw - 32px);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;');
    box.id = 'fu-reminder-box';
    document.body.appendChild(box);
    return box;
  }
  function btn(label, bg, color, onClick) {
    var b = el('button', 'border:0;border-radius:6px;padding:6px 10px;font-size:12px;font-weight:600;cursor:pointer;' +
      'background:' + bg + ';color:' + color + ';', label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }
  function post(data) {
    var body = new URLSearchParams(data);
    return fetch(ACT_URL, {
      method: 'POST', credentials: 'same-origin', body: body,
      headers: {'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest'}
    });
  }
  function remove(key, card) {
    delete shown[key];
    if (card && card.parentNode) card.parentNode.removeChild(card);
    updateTitle();
  }
  function updateTitle() {
    var n = Object.keys(shown).length;
    document.title = n ? '(' + n + ') Follow-up due - ' + baseTitle : baseTitle;
  }
  function render(r) {
    var key = r.kind + ':' + r.id;
    if (shown[key]) return;
    var card = el('div', 'background:#fff;border:1px solid #f59e0b;border-left:5px solid #f59e0b;border-radius:10px;' +
      'box-shadow:0 8px 24px rgba(15,23,42,.25);padding:12px 14px;color:#1f2937;');
    card.setAttribute('role', 'alert');
    card.appendChild(el('div', 'font-size:12px;font-weight:700;color:#b45309;margin-bottom:4px;', '⏰ Follow-up reminder'));
    card.appendChild(el('div', 'font-size:14px;font-weight:700;', r.number + (r.customer ? ' – ' + r.customer : '')));
    if (r.note) card.appendChild(el('div', 'font-size:13px;color:#4b5563;margin:4px 0 8px;word-break:break-word;', r.note));
    card.appendChild(el('div', 'font-size:11px;color:#6b7280;margin-bottom:8px;', 'Set for ' + r.remind_at));
    var row = el('div', 'display:flex;gap:6px;flex-wrap:wrap;');
    var open = btn('Open', '#1e3a5f', '#fff', function () { window.location.href = r.url; });
    var snooze = btn('Snooze 1 hour', '#e5e7eb', '#111827', function () {
      post({kind: r.kind, id: r.id, action: 'snooze', minutes: 60}).then(function () { remove(key, card); });
    });
    var done = btn('Dismiss', '#fff', '#b91c1c', function () {
      post({kind: r.kind, id: r.id, action: 'dismiss'}).then(function () { remove(key, card); });
    });
    done.style.border = '1px solid #fca5a5';
    row.appendChild(open); row.appendChild(snooze); row.appendChild(done);
    card.appendChild(row);
    shown[key] = true;
    ensureBox().appendChild(card);
    updateTitle();
  }
  function poll() {
    if (document.hidden && Object.keys(shown).length) return;
    fetch(DUE_URL, {credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d && d.reminders) d.reminders.forEach(render); })
      .catch(function () {});
  }
  function start() {
    poll();
    setInterval(poll, POLL_MS);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
"""


def widget_js(request):
    """The reminder script. Served by a view (not a static file) so it works without a collectstatic on the server."""
    if not request.user.is_authenticated:
        return HttpResponse('', content_type='application/javascript')   # logged-out pages: do nothing
    js = WIDGET_JS.replace('__DUE__', reverse('followup_reminders_due')).replace('__ACT__', reverse('followup_reminders_act'))
    resp = HttpResponse(js, content_type='application/javascript; charset=utf-8')
    resp['Cache-Control'] = 'no-cache'
    return resp

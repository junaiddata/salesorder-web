"""Item catalog: every stock item (so.Items, kept up to date by the stock sync) with its photo and datasheet.

Photos / datasheets live in ItemMedia (a separate table + private folder), so the sync that rewrites Items never touches
them. Junaid users see every brand; Alabama users only the brands in the admin list (AlabamaCatalogBrand), else settings.ALABAMA_CATALOG_BRANDS (empty = all).
Only admins upload or remove files.
"""
import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django import forms
from django.db import transaction
from django.conf import settings
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from alabama import media_match
from alabama.item_sync import quotable_items
from alabama.models import AlabamaItemMedia
from quotation import pricing

from .item_media import MediaError, datasheet_to_jpeg, photo_to_jpeg
from .item_status import active_items, alabama_brand_limit, latest_sync_run
from .catalog_ref import own_catalog_enabled
from .models import AlabamaCatalogBrand, ItemMedia, Items, ItemSyncStatus

log = logging.getLogger('so')

PAGE_SIZE = 50


def _is_admin(user):
    return pricing.is_price_admin(user)


def _alabama_brands(user):
    """Lower-cased brand allowlist when this user is restricted to Alabama's brands, else None (= every brand)."""
    role = getattr(user, 'role', None)
    is_alabama = (role is not None and role.company == 'Alabama') or 'alabama' in (user.username or '').lower()
    return alabama_brand_limit(user)


def _visible_items(user):
    """Items this user may see; every list / file view starts from this so a brand rule cannot be bypassed by URL."""
    qs = Items.objects.all()
    allowed = _alabama_brands(user)
    if allowed is not None:
        cond = Q()
        for b in allowed:
            cond |= Q(item_firm__iexact=b)
        qs = qs.filter(cond)
    return qs


def _is_alabama_user(user):
    role = getattr(user, 'role', None)
    return bool((role is not None and role.company == 'Alabama') or 'alabama' in (getattr(user, 'username', '') or '').lower())


def _is_company_chooser(user):
    """so / manager admins work for both companies (same people as on the quotation pages)."""
    role = getattr(user, 'role', None)
    return bool(role is not None and role.role == 'Admin' and (user.username or '').strip().lower() in ('so', 'manager'))


def _alabama_mode(request):
    """True when this request shows Alabama's own catalog (AlabamaItem): the own-catalog switch is on and the user is an
    Alabama user, or a so / manager admin who asked for it with ?division=ALABAMA. Everyone else keeps the Junaid catalog."""
    if not own_catalog_enabled():
        return False
    if _is_alabama_user(request.user):
        return True
    return _is_company_chooser(request.user) and (request.GET.get('division') or '').strip().upper() == 'ALABAMA'


def _alabama_files_allowed(user):
    """Who may open Alabama catalog files / upload for Alabama items: the same people who can reach the Alabama catalog."""
    return own_catalog_enabled() and (_is_alabama_user(user) or _is_company_chooser(user))


def _back_url(request):
    """Back to the catalog as the user left it (filters, page); only an address inside the catalog is accepted."""
    plain = reverse('item_catalog')
    nxt = (request.POST.get('next') or '').strip()
    if nxt.startswith(plain) and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return plain


def _filtered(request):
    """The items matching the search / brand / stock / missing-file / status filters on the page (shared by the list and
    its Excel / PDF exports). Always starts from _visible_items, so the Alabama brand rule applies everywhere."""
    if _alabama_mode(request):
        return _filtered_alabama(request)
    base = _visible_items(request.user)
    q = (request.GET.get('q') or '').strip()
    chosen = [b.strip() for b in request.GET.getlist('brand') if b.strip()]
    hide_zero = request.GET.get('instock') == '1'
    missing = request.GET.get('missing') if request.GET.get('missing') in ('photo', 'datasheet') else ''
    # The catalog lists ACTIVE items only: those the stock app listed in its latest sync run. Inactive items are not
    # deleted (old documents keep them) but they are not shown here, and no URL option brings them back.
    # Until the sync has recorded a run nothing is known, so nothing is hidden yet.
    latest_run = latest_sync_run()
    has_status = latest_run is not None
    status = 'active' if has_status else ''

    items = base.select_related('media', 'sync_status')
    if q:
        items = items.filter(Q(item_code__icontains=q) | Q(item_description__icontains=q)
                             | Q(item_upvc__icontains=q) | Q(item_firm__icontains=q))
    if chosen:
        items = items.filter(item_firm__in=chosen)
    if hide_zero:
        items = items.filter(dip_warehouse_stock__gt=0)
    if status == 'active':
        items = items.filter(sync_status__last_seen_at__gte=latest_run)
    if missing == 'photo':
        items = items.filter(Q(media__isnull=True) | Q(media__photo__isnull=True) | Q(media__photo=''))
    elif missing == 'datasheet':
        items = items.filter(Q(media__isnull=True) | Q(media__datasheet__isnull=True) | Q(media__datasheet=''))
    items = items.order_by('item_firm', 'item_code')
    filter_qs = urlencode([('q', q)] + [('brand', b) for b in chosen]
                          + ([('instock', '1')] if hide_zero else []) + ([('missing', missing)] if missing else [])
                          )
    return {'base': base, 'items': items, 'q': q, 'chosen': chosen, 'hide_zero': hide_zero, 'missing': missing,
            'status': status, 'has_status': has_status, 'latest_run': latest_run, 'filter_qs': filter_qs}


def _filtered_alabama(request):
    """Same filters as `_filtered`, over Alabama's own items: the active items of the Alabama brand list (the same ones a
    new Alabama quotation offers). Read-only: the Alabama stock API owns these rows."""
    base = quotable_items()
    q = (request.GET.get('q') or '').strip()
    chosen = [b.strip() for b in request.GET.getlist('brand') if b.strip()]
    hide_zero = request.GET.get('instock') == '1'
    missing = request.GET.get('missing') if request.GET.get('missing') in ('photo', 'datasheet') else ''
    items = base
    if q:
        items = items.filter(Q(item_code__icontains=q) | Q(item_description__icontains=q)
                             | Q(item_upvc__icontains=q) | Q(item_firm__icontains=q))
    if chosen:
        items = items.filter(item_firm__in=chosen)
    if hide_zero:
        items = items.filter(total_available_stock__gt=0)       # Alabama's feed has one stock figure (StockQuantity), no DIP stock
    if missing:
        photo_ids, sheet_ids = media_match.ids_with_files()
        items = items.exclude(pk__in=photo_ids if missing == 'photo' else sheet_ids)
    items = items.order_by('item_firm', 'item_code')
    division = 'ALABAMA' if _is_company_chooser(request.user) else ''
    filter_qs = urlencode([('q', q)] + [('brand', b) for b in chosen]
                          + ([('instock', '1')] if hide_zero else []) + ([('missing', missing)] if missing else [])
                          + ([('division', division)] if division else []))
    return {'base': base, 'items': items, 'q': q, 'chosen': chosen, 'hide_zero': hide_zero, 'missing': missing,
            'status': '', 'has_status': False, 'latest_run': None, 'filter_qs': filter_qs, 'alabama': True,
            'division_param': division}


def _describe_alabama(items):
    """Display values for a list of AlabamaItem rows (photo / datasheet resolved in bulk, see alabama/media_match.py)."""
    items = list(items)
    found = media_match.resolve(items)
    for it in items:
        r = found[it.pk]
        it.dip_stock_text = pricing.format_stock(it.dip_warehouse_stock)
        it.total_stock_text = pricing.format_stock(it.total_available_stock)
        it.has_photo = bool(r.photo)
        it.has_datasheet = bool(r.sheet)
        it.thumb_file = r.photo
        it.media_version = r.version
        it.last_seen = None
        it.is_active = True
        it.can_modify = False                       # the Alabama stock API owns these rows: no edit / delete here
        it.can_remove_photo = r.photo_src == 'own'  # a reused Junaid file is not Alabama's to remove
        it.can_remove_datasheet = r.sheet_src == 'own'
        it.photo_url = reverse('item_catalog_a_photo', args=[it.pk])
        it.datasheet_url = reverse('item_catalog_a_datasheet', args=[it.pk])
        it.upload_url = reverse('item_catalog_a_upload', args=[it.pk])
        it.remove_url = reverse('item_catalog_a_remove', args=[it.pk])
    return items


def describe_items(items, f):
    """`_describe` for every item of a filtered result `f` (Junaid or Alabama)."""
    if f.get('alabama'):
        return _describe_alabama(items)
    return [_describe(it, f['has_status'], f['latest_run']) for it in items]


def _describe(it, has_status, latest_run):
    """Attach the display values the page and the exports need to an item (loaded with select_related media / sync_status)."""
    it.dip_stock_text = pricing.format_stock(it.dip_warehouse_stock)
    it.total_stock_text = pricing.format_stock(it.total_available_stock)
    media = getattr(it, 'media', None)
    it.has_photo = bool(media and media.photo)
    it.has_datasheet = bool(media and media.datasheet)
    it.media_version = int(media.updated_at.timestamp()) if media else 0
    seen = getattr(it, 'sync_status', None)
    it.last_seen = seen.last_seen_at if seen else None
    it.is_active = bool(has_status and seen and seen.last_seen_at >= latest_run)
    # Items the stock app lists are rewritten by the daily sync, so only the others can be edited or deleted here.
    it.can_modify = bool(has_status and not it.is_active)
    it.thumb_file = media.photo if it.has_photo else None
    it.can_remove_photo, it.can_remove_datasheet = it.has_photo, it.has_datasheet
    it.photo_url = reverse('item_catalog_photo', args=[it.pk])
    it.datasheet_url = reverse('item_catalog_datasheet', args=[it.pk])
    it.upload_url = reverse('item_catalog_upload', args=[it.pk])
    it.remove_url = reverse('item_catalog_remove', args=[it.pk])
    return it


@login_required
def item_catalog(request):
    f = _filtered(request)
    base, is_admin = f['base'], _is_admin(request.user)
    # only brands that have at least one active item (a brand whose items are all inactive is not offered)
    if f.get('alabama'):
        brands = list(base.exclude(item_firm='').order_by('item_firm').values_list('item_firm', flat=True).distinct())
        total = base.count()
    else:
        brands = list(active_items(base).exclude(item_firm='').order_by('item_firm').values_list('item_firm', flat=True).distinct())
        total = active_items(base).count()   # inactive items are not counted
    page = Paginator(f['items'], PAGE_SIZE).get_page(request.GET.get('page'))
    page.object_list = describe_items(list(page.object_list), f)
    chosen = f['chosen']
    return render(request, 'so/items/catalog.html', {
        'page': page, 'q': f['q'], 'shown': page.paginator.count, 'total': total,
        'brand_options': [(b, b in chosen) for b in brands], 'brand_count': len(chosen),
        'brand_label': (chosen[0] if len(chosen) == 1 else f'{len(chosen)} brands') if chosen else 'All brands',
        'hide_zero': f['hide_zero'], 'missing': f['missing'], 'status': f['status'], 'has_status': f['has_status'],
        'latest_run': f['latest_run'], 'filter_qs': f['filter_qs'], 'is_admin': is_admin,
        'can_see_cost': _can_edit_cost(request.user), 'here': request.get_full_path(),
        'division_param': f.get('division_param', ''), 'is_alabama_catalog': bool(f.get('alabama')),
    })


def _serve(request, pk, field, content_type):
    item = get_object_or_404(_visible_items(request.user), pk=pk)
    media = ItemMedia.objects.filter(item=item).first()
    f = getattr(media, field, None) if media else None
    if not f:
        raise Http404
    try:
        resp = FileResponse(f.open('rb'), content_type=content_type)
    except FileNotFoundError:
        raise Http404
    resp['Cache-Control'] = 'private, max-age=300' if field == 'photo' else 'private, no-store'
    resp['X-Robots-Tag'] = 'noindex, nofollow'
    return resp


@login_required
@require_GET
def item_catalog_photo(request, pk):
    return _serve(request, pk, 'photo', 'image/jpeg')


@login_required
@require_GET
def item_catalog_datasheet(request, pk):
    resp = _serve(request, pk, 'datasheet', 'image/jpeg')
    code = get_object_or_404(Items, pk=pk).item_code
    safe = ''.join(c for c in code if c.isalnum() or c in '-_.') or 'datasheet'
    resp['Content-Disposition'] = f'{"attachment" if request.GET.get("download") == "1" else "inline"}; filename="{safe}.jpg"'
    return resp


@login_required
@require_POST
def item_catalog_upload(request, pk):
    """Admin only: add or replace one item's photo and / or datasheet. Nothing is changed unless every file given is valid."""
    back = _back_url(request)
    if not _is_admin(request.user):
        messages.error(request, 'Only an admin can upload photos and datasheets.')
        return redirect(back)
    item = get_object_or_404(_visible_items(request.user), pk=pk)

    photo_file, sheet_file = request.FILES.get('photo'), request.FILES.get('datasheet')
    if not photo_file and not sheet_file:
        messages.error(request, 'Choose a photo or a datasheet to upload.')
        return redirect(back)
    try:
        photo_data = photo_to_jpeg(photo_file) if photo_file else None
    except MediaError as exc:
        messages.error(request, f'{item.item_code}: {exc}')
        return redirect(back)
    try:
        sheet_data = datasheet_to_jpeg(sheet_file) if sheet_file else None
    except MediaError as exc:
        messages.error(request, f'{item.item_code}: {exc}')
        return redirect(back)

    old_files = []   # replaced files are deleted only after the new ones are saved
    with transaction.atomic():
        media, _ = ItemMedia.objects.select_for_update().get_or_create(item=item)
        if photo_data is not None:
            if media.photo:
                old_files.append(media.photo.name)
            media.photo.save('photo.jpg', ContentFile(photo_data), save=False)
        if sheet_data is not None:
            if media.datasheet:
                old_files.append(media.datasheet.name)
            media.datasheet.save('datasheet.jpg', ContentFile(sheet_data), save=False)
        media.uploaded_by = request.user
        media.save()
    storage = media.photo.storage
    for name in old_files:
        try:
            storage.delete(name)
        except Exception:
            log.exception('Could not delete replaced item file %s', name)
    messages.success(request, f'{item.item_code}: saved.')
    return redirect(back)


@login_required
@require_POST
def item_catalog_remove(request, pk):
    """Admin only: remove an item's photo, datasheet or both (the files are deleted from disk too)."""
    back = _back_url(request)
    if not _is_admin(request.user):
        messages.error(request, 'Only an admin can remove photos and datasheets.')
        return redirect(back)
    item = get_object_or_404(_visible_items(request.user), pk=pk)
    kind = request.POST.get('kind')
    media = ItemMedia.objects.filter(item=item).first()
    if kind not in ('photo', 'datasheet', 'all') or not media:
        return redirect(back)
    if kind in ('photo', 'all') and media.photo:
        media.photo.delete(save=False)
    if kind in ('datasheet', 'all') and media.datasheet:
        media.datasheet.delete(save=False)
    if media.photo or media.datasheet:
        media.save()
    else:
        media.delete()
    messages.success(request, f'{item.item_code}: removed.')
    return redirect(back)


def _can_edit_cost(user):
    """Same people who may see / change an item's cost on the Items screen."""
    from .views import ITEM_COST_VISIBLE_USERNAMES
    return _is_admin(user) and (user.username or '').strip().lower() in {u.lower() for u in ITEM_COST_VISIBLE_USERNAMES}


class CatalogItemForm(forms.ModelForm):
    """The details of an item the stock app does not list. The item code is never changed here: documents and the sync
    both identify the item by it."""

    class Meta:
        model = Items
        fields = ['item_description', 'item_upvc', 'item_firm', 'item_price', 'item_cost']

    def clean_item_description(self):
        value = (self.cleaned_data.get('item_description') or '').strip()
        if not value:
            raise forms.ValidationError('Enter a description.')
        return value

    def clean_item_firm(self):
        return (self.cleaned_data.get('item_firm') or '').strip().upper() or self.instance.item_firm

    def clean_item_price(self):
        value = self.cleaned_data.get('item_price')
        if value is not None and value < 0:
            raise forms.ValidationError('Price cannot be negative.')
        return value

    def clean_item_cost(self):
        value = self.cleaned_data.get('item_cost')
        if value is not None and value < 0:
            raise forms.ValidationError('Cost cannot be negative.')
        return value


def _is_listed_by_stock_app(item):
    """True when the stock app lists the item in its latest sync -- and also while no sync has been recorded yet, because
    until then nothing is known and the item could be overwritten by the first sync."""
    run = latest_sync_run()
    if run is None:
        return True
    seen = ItemSyncStatus.objects.filter(item=item).values_list('last_seen_at', flat=True).first()
    return bool(seen and seen >= run)


@login_required
@require_POST
def item_catalog_edit(request, pk):
    """Admin only: change the details of an item the stock app does not list (the daily sync would overwrite the others)."""
    back = _back_url(request)
    if not _is_admin(request.user):
        messages.error(request, 'Only an admin can edit items.')
        return redirect(back)
    item = get_object_or_404(_visible_items(request.user), pk=pk)
    if _is_listed_by_stock_app(item):
        messages.error(request, f'{item.item_code} is listed by the stock app, so its details are kept up to date there. '
                                'Change it in the stock app instead.')
        return redirect(back)
    form = CatalogItemForm(request.POST, instance=item)
    if not _can_edit_cost(request.user):
        del form.fields['item_cost']       # cost stays as it is for anyone who may not see it
    if not form.is_valid():
        messages.error(request, f'{item.item_code}: ' + ' '.join(e for errs in form.errors.values() for e in errs))
        return redirect(back)
    form.save()
    messages.success(request, f'{item.item_code}: details saved.')
    return redirect(back)


def _used_by(item):
    """{label: count} of the records that point at this item (quotations, orders, sales history, prices ...), apart from
    its own catalog files and sync mark."""
    used = {}
    for rel in Items._meta.related_objects:
        model = rel.related_model
        if model in (ItemMedia, ItemSyncStatus):
            continue
        n = model._base_manager.filter(**{rel.field.name: item}).count()
        if n:
            used[model._meta.verbose_name_plural] = n
    return used


@login_required
@require_POST
def item_catalog_delete(request, pk):
    """Admin only: delete an item for good -- allowed only when the stock app does not list it AND no document, price or
    history record uses it. Anything else is refused (an item with history stays, shown as Inactive)."""
    back = _back_url(request)
    if not _is_admin(request.user):
        messages.error(request, 'Only an admin can delete items.')
        return redirect(back)
    item = get_object_or_404(_visible_items(request.user), pk=pk)
    if _is_listed_by_stock_app(item):
        messages.error(request, f'{item.item_code} is listed by the stock app and cannot be deleted here.')
        return redirect(back)
    used = _used_by(item)
    if used:
        what = ', '.join(f'{n} {label}' for label, n in list(used.items())[:4])
        messages.error(request, f'{item.item_code} cannot be deleted: it is used by {what}. '
                                'It stays in the system and is shown as Inactive.')
        return redirect(back)
    code = item.item_code
    with transaction.atomic():
        media = ItemMedia.objects.filter(item=item).first()
        files = [f for f in (media.photo, media.datasheet) if f] if media else []
        item.delete()
    for f in files:                      # remove the uploaded files from disk only after the delete went through
        try:
            f.storage.delete(f.name)
        except Exception:
            log.exception('Could not delete item file %s', f.name)
    messages.success(request, f'{code}: deleted.')
    return redirect(back)


# ---------------------------------------------------------------------------------------------------------------------
# Alabama catalog files. Own URLs ('a/<pk>') so an Alabama item id can never be mistaken for a Junaid one. The file is the
# one alabama/media_match.py picks for the item (its own upload, an approved link, or the matching Junaid item's file).
# ---------------------------------------------------------------------------------------------------------------------
def _alabama_item(request, pk):
    if not _alabama_files_allowed(request.user):
        raise Http404
    return get_object_or_404(quotable_items(), pk=pk)


def _stream(f, content_type, private_cache):
    if not f:
        raise Http404
    try:
        resp = FileResponse(f.open('rb'), content_type=content_type)
    except FileNotFoundError:
        raise Http404
    resp['Cache-Control'] = 'private, max-age=300' if private_cache else 'private, no-store'
    resp['X-Robots-Tag'] = 'noindex, nofollow'
    return resp


@login_required
@require_GET
def item_catalog_a_photo(request, pk):
    item = _alabama_item(request, pk)
    return _stream(media_match.resolve([item])[item.pk].photo, 'image/jpeg', True)


@login_required
@require_GET
def item_catalog_a_datasheet(request, pk):
    item = _alabama_item(request, pk)
    resp = _stream(media_match.resolve([item])[item.pk].sheet, 'image/jpeg', False)
    safe = ''.join(c for c in item.item_code if c.isalnum() or c in '-_.') or 'datasheet'
    resp['Content-Disposition'] = f'{"attachment" if request.GET.get("download") == "1" else "inline"}; filename="{safe}.jpg"'
    return resp


@login_required
@require_POST
def item_catalog_a_upload(request, pk):
    """Admin only: add or replace the photo / datasheet of one Alabama item (stored for Alabama only; a Junaid file is never touched)."""
    back = _back_url(request)
    if not _is_admin(request.user):
        messages.error(request, 'Only an admin can upload photos and datasheets.')
        return redirect(back)
    item = _alabama_item(request, pk)
    photo_file, sheet_file = request.FILES.get('photo'), request.FILES.get('datasheet')
    if not photo_file and not sheet_file:
        messages.error(request, 'Choose a photo or a datasheet to upload.')
        return redirect(back)
    try:
        photo_data = photo_to_jpeg(photo_file) if photo_file else None
    except MediaError as exc:
        messages.error(request, f'{item.item_code}: {exc}')
        return redirect(back)
    try:
        sheet_data = datasheet_to_jpeg(sheet_file) if sheet_file else None
    except MediaError as exc:
        messages.error(request, f'{item.item_code}: {exc}')
        return redirect(back)
    old_files = []
    with transaction.atomic():
        media, _ = AlabamaItemMedia.objects.select_for_update().get_or_create(alabama_item=item)
        if photo_data is not None:
            if media.photo:
                old_files.append(media.photo.name)
            media.photo.save('photo.jpg', ContentFile(photo_data), save=False)
        if sheet_data is not None:
            if media.datasheet:
                old_files.append(media.datasheet.name)
            media.datasheet.save('datasheet.jpg', ContentFile(sheet_data), save=False)
        media.uploaded_by = request.user
        media.save()
    storage = media.photo.storage
    for name in old_files:
        try:
            storage.delete(name)
        except Exception:
            log.exception('Could not delete replaced item file %s', name)
    messages.success(request, f'{item.item_code}: saved.')
    return redirect(back)


@login_required
@require_POST
def item_catalog_a_remove(request, pk):
    """Admin only: remove the photo / datasheet that was uploaded for this Alabama item. A file reused from the Junaid
    item is not Alabama's to remove (remove it on the Junaid catalog instead)."""
    back = _back_url(request)
    if not _is_admin(request.user):
        messages.error(request, 'Only an admin can remove photos and datasheets.')
        return redirect(back)
    item = _alabama_item(request, pk)
    kind = request.POST.get('kind')
    media = AlabamaItemMedia.objects.filter(alabama_item=item).first()
    if kind not in ('photo', 'datasheet', 'all') or not media:
        messages.error(request, f'{item.item_code}: nothing uploaded for Alabama to remove (a reused Junaid file is managed on the Junaid catalog).')
        return redirect(back)
    if kind in ('photo', 'all') and media.photo:
        media.photo.delete(save=False)
    if kind in ('datasheet', 'all') and media.datasheet:
        media.datasheet.delete(save=False)
    if media.photo or media.datasheet:
        media.save()
    else:
        media.delete()
    messages.success(request, f'{item.item_code}: removed.')
    return redirect(back)

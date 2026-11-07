"""Which photo / datasheet an Alabama item shows in the Item Catalog.

Nothing is copied: the files stay where they are and are looked up when a page is built. For each of the two files
(photo, datasheet) separately, the first source that has one wins:

  1. the Alabama item's own upload                 (AlabamaItemMedia)
  2. a Junaid item an admin linked to it           (AlabamaMediaLink)
  3. the Junaid item with the SAME item code AND the SAME description (case / punctuation ignored)

Same code with a different description is a different product (e.g. Hepworth vs GF pipe) and is never reused unless
an admin links it in (2). Junaid's data is only read.
"""
import re
from collections import namedtuple

from so.models import ItemMedia

from .models import AlabamaItem, AlabamaItemMedia, AlabamaMediaLink

CHUNK = 500

# photo / sheet: the stored file (FieldFile) or None; *_src: 'own' | 'link' | 'junaid' | ''; version: for cache busting
Resolved = namedtuple('Resolved', 'photo sheet photo_src sheet_src version')
NOTHING = Resolved(None, None, '', '', 0)


def norm(text):
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())


def _file(field):
    return field if field else None            # an empty FieldFile is falsy


def _version(media):
    return int(media.updated_at.timestamp()) if media else 0


def _chunks(seq):
    seq = list(seq)
    for i in range(0, len(seq), CHUNK):
        yield seq[i:i + CHUNK]


def resolve(alabama_items):
    """{alabama item pk: Resolved} for the given AlabamaItem rows (a small, fixed number of queries)."""
    items = list(alabama_items)
    out = {it.pk: NOTHING for it in items}
    if not items:
        return out
    pks = [it.pk for it in items]
    own, linked, auto = {}, {}, {}
    for part in _chunks(pks):
        for m in AlabamaItemMedia.objects.filter(alabama_item_id__in=part):
            own[m.alabama_item_id] = m
        for ln in AlabamaMediaLink.objects.filter(alabama_item_id__in=part).select_related('junaid_item__media'):
            linked[ln.alabama_item_id] = getattr(ln.junaid_item, 'media', None)
    codes = {it.item_code: it for it in items}
    for part in _chunks(codes):
        for jm in ItemMedia.objects.filter(item__item_code__in=part).select_related('item'):
            a = codes.get(jm.item.item_code)
            if a is not None and norm(a.item_description) == norm(jm.item.item_description) and norm(a.item_description):
                auto[a.pk] = jm
    for it in items:
        picked = {}
        for field in ('photo', 'datasheet'):
            for src, media in (('own', own.get(it.pk)), ('link', linked.get(it.pk)), ('junaid', auto.get(it.pk))):
                f = _file(getattr(media, field, None)) if media else None
                if f:
                    picked[field] = (f, src, _version(media))
                    break
        photo, sheet = picked.get('photo'), picked.get('datasheet')
        out[it.pk] = Resolved(photo[0] if photo else None, sheet[0] if sheet else None,
                              photo[1] if photo else '', sheet[1] if sheet else '',
                              max([p[2] for p in (photo, sheet) if p] or [0]))
    return out


def ids_with_files():
    """({pks that have a photo}, {pks that have a datasheet}) over every Alabama item -- for the 'missing' filters."""
    cand = set(AlabamaItemMedia.objects.values_list('alabama_item_id', flat=True))
    cand |= set(AlabamaMediaLink.objects.values_list('alabama_item_id', flat=True))
    junaid_codes = list(ItemMedia.objects.values_list('item__item_code', flat=True))
    for part in _chunks(junaid_codes):
        cand |= set(AlabamaItem.objects.filter(item_code__in=part).values_list('pk', flat=True))
    resolved = resolve(AlabamaItem.objects.filter(pk__in=list(cand))) if cand else {}
    return ({pk for pk, r in resolved.items() if r.photo}, {pk for pk, r in resolved.items() if r.sheet})


def review_rows():
    """Alabama items whose code matches a Junaid item that HAS a photo / datasheet but whose description differs (and that
    are not linked yet): the ones an admin may want to link by hand. [(alabama item, junaid item)]"""
    out = []
    linked = set(AlabamaMediaLink.objects.values_list('alabama_item_id', flat=True))
    for jm in ItemMedia.objects.select_related('item'):
        if not (jm.photo or jm.datasheet):
            continue
        a = AlabamaItem.objects.filter(item_code=jm.item.item_code).first()
        if a and a.pk not in linked and norm(a.item_description) != norm(jm.item.item_description):
            out.append((a, jm.item))
    return out

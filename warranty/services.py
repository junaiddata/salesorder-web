import re
from html import escape
from html.parser import HTMLParser

from django.utils import timezone

from .models import WarrantyBrandTerms, WarrantyLetter, WarrantyLetterSettings

# Tags the Warranty Terms rich-text editor's toolbar can actually produce
# (bold, heading, paragraph, bullet list). Anything else submitted -- e.g.
# a hand-crafted POST body -- is stripped rather than trusted, since this
# HTML is later rendered unescaped on the detail page and generated PDF.
_RICH_TEXT_ALLOWED_TAGS = {'p', 'div', 'br', 'b', 'strong', 'i', 'em', 'u', 'h1', 'h2', 'h3', 'ul', 'ol', 'li'}
_RICH_TEXT_VOID_TAGS = {'br'}


class _RichTextSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in _RICH_TEXT_ALLOWED_TAGS:
            self.out.append(f'<{tag}>')

    def handle_startendtag(self, tag, attrs):
        if tag in _RICH_TEXT_ALLOWED_TAGS:
            self.out.append('<br>' if tag in _RICH_TEXT_VOID_TAGS else f'<{tag}></{tag}>')

    def handle_endtag(self, tag):
        if tag in _RICH_TEXT_ALLOWED_TAGS and tag not in _RICH_TEXT_VOID_TAGS:
            self.out.append(f'</{tag}>')

    def handle_data(self, data):
        self.out.append(escape(data))


def sanitize_rich_text(html_text):
    """Strip everything except the small formatting-tag allowlist above out
    of user-submitted Warranty Terms HTML."""
    if not html_text:
        return ''
    parser = _RichTextSanitizer()
    parser.feed(html_text)
    parser.close()
    return ''.join(parser.out).strip()


def get_brand_terms(brand):
    """This brand's remembered Warranty Terms, or '' if it's never been used
    before. Matched case-insensitively so "Pegler" and "pegler" hit the same
    saved row rather than silently missing each other."""
    brand = (brand or '').strip()
    if not brand:
        return ''
    row = WarrantyBrandTerms.objects.filter(brand__iexact=brand).first()
    return row.terms_text if row else ''


def save_brand_terms(brand, terms_text):
    """Remember `terms_text` as this brand's Warranty Terms for next time."""
    brand = (brand or '').strip()
    if not brand or not terms_text:
        return
    row = WarrantyBrandTerms.objects.filter(brand__iexact=brand).first()
    if row:
        if row.terms_text != terms_text:
            row.terms_text = terms_text
            row.save(update_fields=['terms_text'])
    else:
        WarrantyBrandTerms.objects.create(brand=brand, terms_text=terms_text)


def suggest_ref_no(company):
    """Suggest the next '<prefix>/<year>/<seq>' Ref No for a company.

    Scans existing ref_no values for the current year rather than keeping a
    stored counter, since ref_no is not uniqueness-enforced (the user can
    freely edit/reuse/backdate it).
    """
    settings_row = WarrantyLetterSettings.get_instance(company)
    prefix = settings_row.ref_no_prefix or ('JN/WC' if company == 'junaid' else 'AL/WC')
    year = timezone.now().year

    pattern = re.compile(rf'^{re.escape(prefix)}/{year}/(\d+)$')
    max_seq = 0
    qs = WarrantyLetter.objects.filter(
        company=company, ref_no__startswith=f'{prefix}/{year}/'
    ).values_list('ref_no', flat=True)
    for ref in qs:
        m = pattern.match(ref)
        if m:
            max_seq = max(max_seq, int(m.group(1)))

    return f'{prefix}/{year}/{max_seq + 1:04d}'

import os
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from so.models import Items

from .models import BangladeshPI


class BangladeshPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('tester', password='x')
        self.client.force_login(self.user)

    def _payload(self, suffix='07A', bank=('17.00', '23.00')):
        return {
            'ref_suffix': suffix, 'date': '2026-10-15',
            'port_of_origin': 'ANY ITALIAN PORT', 'country_of_origin': 'ITALY',
            'transport': '1 x 40\' FCL, BY SEA', 'insurance': 'COVERED BY BUYER',
            'destination': 'CFR, CHATTOGRAM PORT', 'final_destination': 'CFR, CHATTOGRAM, BANGLADESH',
            'shipment': 'OCT-2023', 'validity': '31st OCT 2023',
            'payment': 'IRREVOCABLE & CONFIRMED L/C AT SIGHT & BALANCE ADVANCE T/T',
            'freight_charges': '1800', 'freight_valid_till': 'Dec -2023',
            'bank_terms_text': 'THE LATEST SHIPPING DATE MUST BE AT LEAST 2 MONTHS FROM THE DATE OF ISSUE.\nPARTIAL AND TRANSHIPMENT TO BE ALLOWED',
            'bank_payment': 'IRREVOCABLE & CONFIRMED L/C AT SIGHT', 'bank_insurance': 'BY THE BUYER',
            'item_kind': ['heading', 'item', 'item'],
            'item_code': ['', '3100635', '3201435'],
            'item_description': ['ARISTON BRAND ELECTRIC STORAGE WATER HEATERS (HS CODE NO.: 8516.10.90)',
                                 'ANDRIS R 30/3', 'PRO 1 R 50 V/S'],
            'item_qty': ['', '450', '100'],
            'item_price': ['', '82.15', '91.19'],
            'item_bank_price': ['', *bank],
        }

    def _post(self, suffix='07A', bank=('17.00', '23.00')):
        return self.client.post(reverse('bangladeshpi:new'), self._payload(suffix, bank))

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('bangladeshpi:list')).status_code, 302)

    def test_create_and_pdf(self):
        r = self._post()
        self.assertEqual(r.status_code, 302)
        pi = BangladeshPI.objects.get()
        self.assertEqual(pi.ref_no, 'BD/IMAA/2026/07A')
        self.assertEqual(pi.items_total, Decimal('450') * Decimal('82.15') + Decimal('100') * Decimal('91.19'))
        self.assertEqual(pi.grand_total, pi.items_total + Decimal('1800'))
        for url in (reverse('bangladeshpi:list'), reverse('bangladeshpi:edit', args=[pi.pk])):
            self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.get(reverse('bangladeshpi:pdf', args=[pi.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'%PDF'))
        out = os.environ.get('BDPI_SAMPLE_OUT')
        if out:
            with open(out, 'wb') as f:
                f.write(r.content)

    def test_duplicate_ref_rejected(self):
        self._post()
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(BangladeshPI.objects.count(), 1)

    def test_items_search_by_upc_code_and_description(self):
        Items.objects.create(item_code='A100', item_description='ANDRIS R 30/3 HEATER', item_upvc='3100635', item_firm='ARISTON')
        Items.objects.create(item_code='B200', item_description='PRO 1 R 50 V/S', item_upvc='', item_firm='ARISTON')
        url = reverse('bangladeshpi:items_search')

        def hit(q, **kw):
            return self.client.get(url, {'q': q, **kw}).json()

        self.assertEqual([r['upc'] for r in hit('3100635')['results']], ['3100635'])      # by UPC
        self.assertEqual(len(hit('B200')['results']), 1)                                   # by item code
        self.assertEqual(len(hit('heater andris')['results']), 1)                          # words, any order
        self.assertEqual(hit('PRO 1')['results'][0]['upc'], '')                            # blank UPC returned as ''
        self.assertEqual(hit('ariston', brand='OTHER')['total'], 0)                        # brand filter
        self.assertEqual(hit('a')['results'], [])                                          # too short

    def test_bank_pdf(self):
        self._post()
        pi = BangladeshPI.objects.get()
        self.assertTrue(pi.has_bank_prices)
        self.assertEqual(pi.bank_items_total, Decimal('450') * 17 + Decimal('100') * 23)
        r = self.client.get(reverse('bangladeshpi:pdf', args=[pi.pk]), {'copy': 'bank'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('_Bank.pdf', r['Content-Disposition'])
        out = os.environ.get('BDPI_SAMPLE_OUT')
        if out:
            with open(out.replace('.pdf', '_bank.pdf'), 'wb') as f:
                f.write(r.content)

    def test_bank_pdf_blocked_without_prices_and_partial_rejected(self):
        self._post(bank=('', ''))
        pi = BangladeshPI.objects.get()
        self.assertFalse(pi.has_bank_prices)
        r = self.client.get(reverse('bangladeshpi:pdf', args=[pi.pk]), {'copy': 'bank'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse('bangladeshpi:pdf', args=[pi.pk])).status_code, 200)  # customer still fine
        r = self._post(suffix='08', bank=('17.00', ''))   # partial bank prices
        self.assertEqual(r.status_code, 200)
        self.assertEqual(BangladeshPI.objects.count(), 1)

    def test_new_form_prefills_defaults(self):
        html = self.client.get(reverse('bangladeshpi:new')).content.decode()
        self.assertIn('IRREVOCABLE &amp; CONFIRMED L/C AT SIGHT &amp; BALANCE ADVANCE T/T', html)
        self.assertIn('BY THE BUYER', html)


import io
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image, ImageDraw


def _png(color, size=(300, 120), text=None):
    img = Image.new('RGBA', size, (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((5, 5, size[0] - 5, size[1] - 5), outline=color, width=6)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return SimpleUploadedFile('x.png', buf.getvalue(), content_type='image/png')


_PRIVATE = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BANGLADESH_PI_PRIVATE_ROOT=_PRIVATE,
                   BANGLADESH_PI_SEAL_USERS=['tester', 'md'])
class SealTests(BangladeshPITests):
    def test_upload_and_print_signature_stamp(self):
        from .models import BangladeshPISeal
        r = self.client.post(reverse('bangladeshpi:seal_settings'),
                             {'signature': _png('blue'), 'stamp': _png('purple', (200, 200))})
        self.assertEqual(r.status_code, 302)
        seal = BangladeshPISeal.get_instance()
        self.assertTrue(seal.signature and seal.stamp)

        r = self.client.post(reverse('bangladeshpi:seal_settings'), {'signature': SimpleUploadedFile('bad.png', b'not an image')})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(BangladeshPISeal.get_instance().signature)       # bad upload didn't replace it

        self.client.post(reverse('bangladeshpi:new'), {**self._payload(), 'add_signature': 'on', 'add_stamp': 'on'})
        pi = BangladeshPI.objects.get()
        self.assertTrue(pi.add_signature and pi.add_stamp)
        plain = self.client.get(reverse('bangladeshpi:pdf', args=[pi.pk])).content
        self.assertTrue(plain.startswith(b'%PDF'))
        out = os.environ.get('BDPI_SAMPLE_OUT')
        if out:
            with open(out.replace('.pdf', '_seal.pdf'), 'wb') as f:
                f.write(plain)
        # remove one
        self.client.post(reverse('bangladeshpi:seal_settings'), {'remove_stamp': '1'})
        self.assertFalse(BangladeshPISeal.get_instance().stamp)
        self.assertEqual(self.client.get(reverse('bangladeshpi:pdf', args=[pi.pk])).status_code, 200)  # missing image never breaks PDF


class StripBackgroundTests(TestCase):
    def test_white_becomes_transparent_ink_stays(self):
        from .pdf_builder import _strip_white_background
        img = Image.new('RGB', (4, 1), 'white')
        img.putpixel((1, 0), (0, 0, 200))      # blue ink
        img.putpixel((2, 0), (250, 250, 250))  # near-white paper noise
        out = _strip_white_background(img)
        self.assertEqual(out.getpixel((0, 0))[3], 0)
        self.assertEqual(out.getpixel((1, 0))[3], 255)
        self.assertEqual(out.getpixel((2, 0))[3], 0)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BANGLADESH_PI_PRIVATE_ROOT=tempfile.mkdtemp(),
                   BANGLADESH_PI_SEAL_USERS=['tester', 'md'])
class SealSecurityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.boss = User.objects.create_user('MD', password='x')       # case-insensitive match
        self.staff = User.objects.create_user('staff1', password='x')
        self.client.force_login(self.boss)
        self.client.post(reverse('bangladeshpi:seal_settings'),
                         {'signature': _png('blue'), 'stamp': _png('purple', (200, 200))})
        base = BangladeshPITests()
        base.client = self.client
        self.client.post(reverse('bangladeshpi:new'), {**BangladeshPITests._payload(base), 'add_signature': 'on', 'add_stamp': 'on'})
        self.pi = BangladeshPI.objects.get()

    def _images(self, user):
        import fitz
        self.client.force_login(user)
        r = self.client.get(reverse('bangladeshpi:pdf', args=[self.pi.pk]))
        self.assertEqual(r.status_code, 200)
        return len(fitz.open(stream=r.content, filetype='pdf')[0].get_images())

    def test_files_are_private_and_have_no_url(self):
        from django.conf import settings
        from .models import BangladeshPISeal
        seal = BangladeshPISeal.get_instance()
        self.assertTrue(seal.signature.storage.exists(seal.signature.name))
        self.assertTrue(os.path.abspath(seal.signature.path).startswith(os.path.abspath(settings.BANGLADESH_PI_PRIVATE_ROOT)))
        self.assertFalse(os.path.exists(os.path.join(settings.MEDIA_ROOT, seal.signature.name)))
        with self.assertRaises(ValueError):
            seal.signature.url

    def test_only_authorised_users_manage_seals(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('bangladeshpi:seal_settings')).status_code, 403)
        self.assertEqual(self.client.post(reverse('bangladeshpi:seal_settings'), {'remove_stamp': '1'}).status_code, 403)
        from .models import BangladeshPISeal
        self.assertTrue(BangladeshPISeal.get_instance().stamp)

    def test_page_shows_no_image_or_link(self):
        self.client.force_login(self.boss)
        html = self.client.get(reverse('bangladeshpi:seal_settings')).content.decode()
        self.assertNotIn('<img', html)
        self.assertNotIn('/media/', html)
        self.assertNotIn('.png', html.lower())
        self.assertIn('Signature uploaded', html)

    def test_unauthorised_user_gets_unsigned_pdf_and_cannot_tick(self):
        with_seals = self._images(self.boss)
        without = self._images(self.staff)
        self.assertGreater(with_seals, without)
        # staff cannot switch the flags off or on
        self.client.force_login(self.staff)
        base = BangladeshPITests(); base.client = self.client
        payload = BangladeshPITests._payload(base)
        self.client.post(reverse('bangladeshpi:edit', args=[self.pi.pk]), payload)   # no tick-boxes sent
        self.pi.refresh_from_db()
        self.assertTrue(self.pi.add_signature and self.pi.add_stamp)
        # and the form tells them why
        html = self.client.get(reverse('bangladeshpi:edit', args=[self.pi.pk])).content.decode()
        self.assertIn('Only authorised users', html)
        self.assertNotIn('Signature &amp; Stamp</a>', html)   # no nav link to the seal page

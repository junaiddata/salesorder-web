import io
import os
import tempfile
from decimal import Decimal

import fitz
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image, ImageDraw

from .models import (
    BangladeshCommercialInvoice, BangladeshPI, BangladeshPIItem,
)


def _png(color, size=(300, 120)):
    img = Image.new('RGBA', size, (255, 255, 255, 0))
    ImageDraw.Draw(img).ellipse((5, 5, size[0] - 5, size[1] - 5), outline=color, width=6)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return SimpleUploadedFile('x.png', buf.getvalue(), content_type='image/png')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BANGLADESH_PI_PRIVATE_ROOT=tempfile.mkdtemp(),
                   BANGLADESH_PI_SEAL_USERS=['md'])
class CommercialInvoiceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.md = User.objects.create_user('md', password='x')
        self.staff = User.objects.create_user('staff1', password='x')
        self.client.force_login(self.md)
        self.pi = BangladeshPI.objects.create(
            ref_suffix='07A', country_of_origin='Italy', bank_freight_charges=Decimal('1000'),
            freight_charges=Decimal('1800'), add_signature=True, add_stamp=True)
        BangladeshPIItem.objects.create(
            pi=self.pi, position=0, kind='heading',
            description='ARISTON BRAND ELECTRIC STORAGE WATER HEATERS (HS CODE NO. - 8516.10.90)')
        BangladeshPIItem.objects.create(pi=self.pi, position=1, code='3100635', description='ANDRIS RS 30/3',
                                        qty=540, unit_price=82.15, bank_unit_price=17)
        BangladeshPIItem.objects.create(pi=self.pi, position=2, code='3201438', description='PRO1R 50 H/5',
                                        qty=85, unit_price=91.19, bank_unit_price=23)
        BangladeshPIItem.objects.create(pi=self.pi, position=3, code='FREE1', description='FREE SAMPLE',
                                        qty=2, is_foc=True)
        self.url = reverse('bangladeshpi:ci_edit', args=[self.pi.pk])

    def _text(self, user=None):
        if user:
            self.client.force_login(user)
        r = self.client.get(reverse('bangladeshpi:ci_pdf', args=[self.pi.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'%PDF'))
        doc = fitz.open(stream=r.content, filetype='pdf')
        return doc, '\n'.join(p.get_text() for p in doc)

    def test_needs_bank_prices_and_login(self):
        self.pi.items.filter(code='3100635').update(bank_unit_price=None)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertFalse(BangladeshCommercialInvoice.objects.exists())
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_created_from_pi_as_snapshot(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        ci = BangladeshCommercialInvoice.objects.get()
        self.assertEqual(ci.invoice_no, 'BD/IMAA/2026/07A')
        self.assertEqual(ci.hs_code, '8516.10.90')
        self.assertEqual(ci.freight_charges, Decimal('1000'))
        codes = [l.code for l in ci.lines.all() if l.kind == 'item']
        self.assertEqual(codes, ['3100635', '3201438'])                     # FOC row left out
        self.assertEqual(ci.cfr_total, Decimal('540') * 17 + Decimal('85') * 23)
        self.assertEqual(ci.fob_total, ci.cfr_total - 1000)
        self.assertTrue(ci.add_signature and ci.add_stamp)                  # defaults from the PI
        # editing the PI afterwards does not change the issued invoice
        self.pi.items.filter(code='3100635').update(qty=1)
        self.client.get(self.url)
        ci.refresh_from_db()
        self.assertEqual(ci.lines.get(code='3100635').qty, 540)

    def test_save_fields_and_pdf_content(self):
        self.client.get(self.url)
        ci = BangladeshCommercialInvoice.objects.get()
        data = {
            'invoice_date': '2026-11-20', 'comm_ref_no': '25', 'to_order_of': 'NATIONAL CREDIT BANK\nAGRABAD BRANCH',
            'packages': '635', 'lc_number': '161124010026', 'lc_date': '2024-01-31',
            'container_no': 'EMCU6234419', 'gross_weight': '7962.50', 'net_weight': '7270.00', 'volume': '66.66',
            'hs_code': '8516.10.90', 'add_signature': 'on', 'add_stamp': 'on',
        }
        for l in ci.lines.filter(kind='item'):
            data.update({f'pack_{l.pk}': '540', f'gross_{l.pk}': '6210.00', f'net_{l.pk}': '5670.00'})
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        ci.refresh_from_db()
        self.assertEqual((ci.comm_ref_no, ci.packages, ci.container_no), ('25', '635', 'EMCU6234419'))
        self.assertEqual(ci.gross_weight, Decimal('7962.50'))

        doc, text = self._text()
        for needle in ('COMMERCIAL INVOICE', 'INVOICE NO', 'BD/IMAA/2026/07A', 'IS100092', '20.11.2024'[:0] + '20.11.2026',
                       'TO THE ORDER OF', 'AGRABAD BRANCH', 'NOTIFY', 'M/S.IMAA CORPORATION',
                       'CFR CHATTOGRAM SEAPORT, BANGLADESH', 'CARDBOARD BOXES', '635',
                       'LETTER OF CREDIT AT SIGHT', '161124010026', '31.01.2024', '1X40 HQ FCL', 'EMCU6234419',
                       '7,962.50', '7,270.00', '66.66', 'ANDRIS RS 30/3', '9,180.00', 'PZ',
                       'FOB CHARGES', 'FREIGHT PREPAID', 'SHIPPING MARK', 'ALSO NOTIFY', 'NASHEER AHAMMAD'):
            self.assertIn(needle, text, needle)
        self.assertEqual(text.count('AGRABAD BRANCH'), 2)       # TO THE ORDER OF and ALSO NOTIFY show the same address
        self.assertNotIn('DOMINION PLAZA', text)
        self.assertIn('000483386-0202', text)                   # default issuing bank BIN
        ci.issuing_bank_bin = '000111222-0303'
        ci.save()
        self.assertIn('000111222-0303', self._text()[1])
        self.assertNotIn('FREE SAMPLE', text)
        self.assertNotIn('ARISTON BRAND', text)        # the PI heading row is not on the invoice
        self.assertNotIn('Page 1/1', text)
        # order: CFR total, freight, FOB
        i1, i2, i3 = (text.index(k) for k in ('CFR CHATTOGRAM, BANGLADESH', 'FREIGHT CHARGES', 'FOB CHARGES'))
        self.assertTrue(i1 < i2 < i3)
        self.assertIn(f'{ci.fob_total:,.2f}', text)
        self.assertEqual(len(doc), 1)

    def test_no_freight_shows_only_cfr_total(self):
        self.pi.bank_freight_charges = 0
        self.pi.save()
        self.client.get(self.url)
        _, text = self._text()
        self.assertIn('CFR CHATTOGRAM, BANGLADESH', text)
        self.assertNotIn('FOB CHARGES', text)

    def test_refresh_keeps_weights(self):
        self.client.get(self.url)
        ci = BangladeshCommercialInvoice.objects.get()
        line = ci.lines.get(code='3100635')
        line.pack, line.gross_kg = 540, Decimal('6210')
        line.save()
        self.pi.items.filter(code='3100635').update(qty=600)
        self.client.post(self.url, {'action': 'refresh'})
        ci.refresh_from_db()
        line = ci.lines.get(code='3100635')
        self.assertEqual(line.qty, 600)
        self.assertEqual((line.pack, line.gross_kg), (540, Decimal('6210')))

    def test_signature_stamp_only_for_authorised_users(self):
        self.client.post(reverse('bangladeshpi:seal_settings'), {'signature': _png('blue'), 'stamp': _png('purple', (200, 200))})
        self.client.get(self.url)
        doc_md, _ = self._text()
        doc_staff, _ = self._text(self.staff)
        self.assertGreater(len(doc_md[-1].get_images()), len(doc_staff[-1].get_images()))
        # a non-authorised user cannot switch the choice off
        self.client.post(self.url, {'invoice_date': '2026-11-20'})
        ci = BangladeshCommercialInvoice.objects.get()
        self.assertTrue(ci.add_signature and ci.add_stamp)

    def test_list_has_button_and_pi_pdf_unaffected(self):
        html = self.client.get(reverse('bangladeshpi:list')).content.decode()
        self.assertIn('Commercial Invoice', html)
        self.assertEqual(self.client.get(reverse('bangladeshpi:pdf', args=[self.pi.pk]), {'copy': 'bank'}).status_code, 200)
        self.assertEqual(self.client.get(reverse('bangladeshpi:pdf', args=[self.pi.pk])).status_code, 200)


class FlashAutoCloseTests(TestCase):
    def test_flash_messages_have_auto_close_script(self):
        user = get_user_model().objects.create_user('someone', password='x')
        self.client.force_login(user)
        html = self.client.post(reverse('bangladeshpi:new'), {'ref_suffix': ''}, follow=True).content.decode()
        self.assertIn("document.querySelectorAll('#flashStack .flash')", html)
        self.assertIn('setTimeout(close', html)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BANGLADESH_PI_PRIVATE_ROOT=tempfile.mkdtemp(),
                   BANGLADESH_PI_SEAL_USERS=['md'])
class PackingListTests(TestCase):
    def setUp(self):
        self.md = get_user_model().objects.create_user('md', password='x')
        self.client.force_login(self.md)
        self.pi = BangladeshPI.objects.create(ref_suffix='07A', country_of_origin='Italy')
        BangladeshPIItem.objects.create(pi=self.pi, position=0, code='3100635', description='ANDRIS RS 30/3',
                                        qty=540, unit_price=82.15, bank_unit_price=17)
        BangladeshPIItem.objects.create(pi=self.pi, position=1, code='FREE1', description='FREE SAMPLE',
                                        qty=2, is_foc=True)
        self.pdf = reverse('bangladeshpi:pl_pdf', args=[self.pi.pk])
        self.edit = reverse('bangladeshpi:ci_edit', args=[self.pi.pk])

    def _text(self):
        r = self.client.get(self.pdf)
        self.assertEqual(r.status_code, 200)
        doc = fitz.open(stream=r.content, filetype='pdf')
        return doc, '\n'.join(p.get_text() for p in doc)

    def test_redirects_until_invoice_exists_then_pdf(self):
        self.assertEqual(self.client.get(self.pdf).status_code, 302)
        self.client.get(self.edit)
        line = BangladeshCommercialInvoice.objects.get().lines.get()
        self.client.post(self.edit, {
            'invoice_date': '2026-11-20', 'our_delivery': '25', 'ref_our_ord_no': '26', 'packages': '635',
            f'pack_{line.pk}': '540', f'gross_{line.pk}': '6210.00', f'net_{line.pk}': '5670.00'})
        doc, text = self._text()
        for needle in ('PACKING LIST', 'BD/IMAA/2026/PL/07A', 'ANDRIS RS 30/3', '6,210.00', '5,670.00',
                       'ELECTRIC STORAGE WATER HEATERS', 'OUR DELIVERY', 'BD/IMAA/2026/07A', 'ALSO NOTIFY'):
            self.assertIn(needle, text, needle)
        for banned in ('FREE SAMPLE', '(USD)', 'FOB CHARGES', 'CFR CHATTOGRAM, BANGLADESH', '9,180.00'):
            self.assertNotIn(banned, text, banned)
        self.assertEqual(len(doc), 1)

    def test_typed_number_and_commercial_invoice_unchanged(self):
        self.client.get(self.edit)
        self.client.post(self.edit, {'invoice_date': '2026-11-20', 'pl_no': '01/PL/2026/25'})
        self.assertIn('01/PL/2026/25', self._text()[1])
        r = self.client.get(reverse('bangladeshpi:ci_pdf', args=[self.pi.pk]))
        t2 = '\n'.join(p.get_text() for p in fitz.open(stream=r.content, filetype='pdf'))
        self.assertIn('COMMERCIAL INVOICE', t2)
        self.assertIn('9,180.00', t2)

import os
import shutil
import tempfile
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from PyPDF2 import PdfReader

from .models import Quotation, QuotationItem

TMP_ROOT = tempfile.mkdtemp(prefix='qtn_test_')


def make_pdf(text='Datasheet', pages=1):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for n in range(pages):
        c.drawString(100, 700, f'{text} page {n + 1}')
        c.showPage()
    c.save()
    return buf.getvalue()


PDF_BYTES = make_pdf()


def jpeg_size(data):
    assert data[:3] == b'\xff\xd8\xff', 'not a JPEG'
    return Image.open(BytesIO(data)).size


def png_file(name='p.png'):
    buf = BytesIO()
    Image.new('RGB', (40, 30), (200, 30, 30)).save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


@override_settings(QUOTATION_PRIVATE_ROOT=TMP_ROOT, QUOTATION_PUBLIC_BASE_URL='https://example.test')
class QuotationTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def setUp(self):
        self.user = get_user_model().objects.create_user('tester', password='x', email='t@x.com')
        self.client.force_login(self.user)

    def _payload(self, qtn_no='QTN-1', **extra):
        data = {
            'qtn_no': qtn_no, 'date': '2026-10-20', 'to_text': 'ACME LLC\nDubai',
            'remarks': 'Deposit before production\n\n2) Port of loading: Shenzhen\n3. Net packing', 'validity': '30 days', 'stock_availability': '2026-11-05', 'project_reference': 'PRJ-77 Marina Tower',
            'country_of_origin': 'Italy', 'sales_person': 'Aiswarya', 'email': 'a@x.com',
            'contact_number': '0501234567',
            'row_key': ['n1', 'n2'], 'item_id': ['', ''],
            'item_no': ['H12', ''],
            'item_model': ['AR-50', 'PG-12'],
            'item_brand': ['ARISTON', 'PEGLER'], 'item_description': ['Heater 50L', 'Valve'],
            'item_size': ['50L', '1/2"'], 'item_color': ['White', ''],
            'item_qty': ['10', '2'], 'item_price': ['100.50', '25'],
            'item_warranty': ['2 Years', ''],
            'remove_photo': ['0', '0'], 'remove_ds': ['0', '0'],
        }
        data.update(extra)
        return data

    def test_create_with_files_and_totals(self):
        resp = self.client.post(reverse('quotation:new'), self._payload(
            photo_n1=png_file(), ds_n1=SimpleUploadedFile('sheet.pdf', PDF_BYTES, 'application/pdf')))
        self.assertRedirects(resp, reverse('quotation:list'))
        q = Quotation.objects.get()
        self.assertEqual(q.total, Decimal('1055.00'))
        self.assertEqual(q.vat_amount, Decimal('52.75'))        # 5% of 1,055.00
        self.assertEqual(q.grand_total, Decimal('1107.75'))
        first, second = q.items.all()
        self.assertTrue(first.photo and first.datasheet and first.share_token)
        self.assertFalse(second.datasheet or second.share_token)

    def test_public_datasheet_needs_no_login_and_only_serves_that_file(self):
        self.client.post(reverse('quotation:new'), self._payload(
            ds_n1=SimpleUploadedFile('sheet.pdf', PDF_BYTES, 'application/pdf')))
        token = QuotationItem.objects.get(brand='ARISTON').share_token
        self.client.logout()
        resp = self.client.get(reverse('quotation:datasheet', args=[token]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'image/jpeg')
        self.assertEqual(jpeg_size(b''.join(resp.streaming_content))[0], 1240)
        self.assertTrue(resp['Content-Disposition'].startswith('attachment'))   # the PDF link downloads the file
        viewed = self.client.get(reverse('quotation:datasheet', args=[token]) + '?view=1')
        self.assertTrue(viewed['Content-Disposition'].startswith('inline'))
        self.assertEqual(self.client.get(reverse('quotation:datasheet', args=['nope'])).status_code, 404)

    def test_other_pages_require_login(self):
        self.client.logout()
        for name, args in (('list', []), ('new', []), ('pdf', [1]), ('photo', [1])):
            resp = self.client.get(reverse(f'quotation:{name}', args=args))
            self.assertEqual(resp.status_code, 302, name)

    def test_rejects_non_pdf_datasheet_and_bad_image(self):
        resp = self.client.post(reverse('quotation:new'), self._payload(
            ds_n1=SimpleUploadedFile('sheet.pdf', b'not a pdf'),
            photo_n2=SimpleUploadedFile('x.png', b'garbage', 'image/png')))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Quotation.objects.exists())
        self.assertContains(resp, 'PDF or an image')
        self.assertContains(resp, 'valid image')

    def test_duplicate_qtn_no_and_no_items(self):
        self.client.post(reverse('quotation:new'), self._payload())
        resp = self.client.post(reverse('quotation:new'), self._payload())
        self.assertContains(resp, 'already exists')
        resp = self.client.post(reverse('quotation:new'), self._payload(
            'QTN-2', item_no=['', ''], item_model=['', ''], item_brand=['', ''], item_description=['', '']))
        self.assertContains(resp, 'at least one item')

    def test_edit_keeps_files_and_token_and_replace_keeps_token(self):
        self.client.post(reverse('quotation:new'), self._payload(
            photo_n1=png_file(), ds_n1=SimpleUploadedFile('sheet.pdf', PDF_BYTES, 'application/pdf')))
        q = Quotation.objects.get()
        item = q.items.first()
        token, photo_name = item.share_token, item.photo.name
        # Edit without touching files: nothing is lost.
        edit = self._payload(row_key=[f'e{item.pk}', 'n1'], item_id=[str(item.pk), str(q.items.last().pk)])
        edit['row_key'] = [f'e{item.pk}', f'e{q.items.last().pk}']
        self.client.post(reverse('quotation:edit', args=[q.pk]), edit)
        item.refresh_from_db()
        self.assertEqual((item.share_token, item.photo.name), (token, photo_name))
        self.assertTrue(item.datasheet)
        # Replace the datasheet: same token, new file.
        new_pdf = make_pdf('Revised', pages=2)
        edit[f'ds_e{item.pk}'] = SimpleUploadedFile('new.pdf', new_pdf, 'application/pdf')
        self.client.post(reverse('quotation:edit', args=[q.pk]), edit)
        item.refresh_from_db()
        self.assertEqual(item.share_token, token)
        self.client.logout()
        resp = self.client.get(reverse('quotation:datasheet', args=[token]))
        w, h = jpeg_size(b''.join(resp.streaming_content))
        self.assertGreater(h, 1.5 * w)   # the 2-page PDF is now one tall JPEG

    def test_remove_datasheet_kills_link(self):
        self.client.post(reverse('quotation:new'), self._payload(
            ds_n1=SimpleUploadedFile('sheet.pdf', PDF_BYTES, 'application/pdf')))
        q = Quotation.objects.get()
        item, other = q.items.all()
        token = item.share_token
        edit = self._payload(row_key=[f'e{item.pk}', f'e{other.pk}'], item_id=[str(item.pk), str(other.pk)],
                             remove_ds=['1', '0'])
        self.client.post(reverse('quotation:edit', args=[q.pk]), edit)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('quotation:datasheet', args=[token])).status_code, 404)

    def test_delete_removes_files(self):
        self.client.post(reverse('quotation:new'), self._payload(
            photo_n1=png_file(), ds_n1=SimpleUploadedFile('sheet.pdf', PDF_BYTES, 'application/pdf')))
        item = QuotationItem.objects.get(brand='ARISTON')
        paths = [item.photo.path, item.datasheet.path]
        self.assertTrue(all(os.path.exists(p) for p in paths))
        self.client.post(reverse('quotation:delete', args=[item.quotation_id]))
        self.assertFalse(any(os.path.exists(p) for p in paths))
        self.assertFalse(Quotation.objects.exists())

    def test_pdf_is_on_letterhead_with_content_and_download_link(self):
        self.client.post(reverse('quotation:new'), self._payload(
            photo_n1=png_file(), ds_n1=SimpleUploadedFile('sheet.pdf', PDF_BYTES, 'application/pdf')))
        q = Quotation.objects.get()
        resp = self.client.get(reverse('quotation:pdf', args=[q.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        reader = PdfReader(BytesIO(resp.content))
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        for needle in ('QUOTATION', 'QTN-1', '20.10.2026', 'ACME LLC', 'ARISTON', 'Heater 50L',
                       'H12', 'MODEL', 'AR-50', 'TERMS AND CONDITIONS', 'TOTAL (AED)', '1,055.00', 'VAT 5% (AED)', '52.75', 'GRAND TOTAL (AED)', '1,107.75', '1.  Deposit before production', '2.  Port of loading: Shenzhen', '3.  Net packing', 'VALIDITY', '05.11.2026', 'PROJECT REF', 'PRJ-77 Marina Tower', 'BANK ACCOUNT DETAILS',
                       'A/C NO: 012001111025', 'A/C NAME: JUNAID GALLERIA TRADING LLC', 'AE88 0380 0000 1200 1111 025',
                       'SALES PERSON', 'Aiswarya', 'Download', 'Datasheet'):
            self.assertIn(needle, text, needle)
        item = q.items.first()
        uris = [a.get_object()['/A']['/URI'] for a in reader.pages[0].get('/Annots', [])]
        self.assertIn(f'https://example.test/quote-builder/ds/{item.share_token}/', uris)

    def test_image_and_multipage_pdf_uploads_become_jpeg(self):
        buf = BytesIO()
        Image.new('RGBA', (800, 600), (0, 0, 0, 0)).save(buf, 'PNG')   # transparent PNG
        self.client.post(reverse('quotation:new'), self._payload(
            ds_n1=SimpleUploadedFile('scan.png', buf.getvalue(), 'image/png'),
            ds_n2=SimpleUploadedFile('multi.pdf', make_pdf(pages=3), 'application/pdf')))
        one, two = Quotation.objects.get().items.all()
        self.client.logout()
        sizes = []
        for item in (one, two):
            self.assertTrue(item.datasheet.name.endswith('.jpg'), item.datasheet.name)
            resp = self.client.get(reverse('quotation:datasheet', args=[item.share_token]))
            self.assertEqual(resp['Content-Type'], 'image/jpeg')
            self.assertIn('.jpg', resp['Content-Disposition'])
            sizes.append(jpeg_size(b''.join(resp.streaming_content)))
        self.assertEqual(sizes[0], (800, 600))
        self.assertGreater(sizes[1][1], 3 * 1500)   # 3 stacked A4 pages

    def test_pdf_with_too_many_pages_rejected(self):
        resp = self.client.post(reverse('quotation:new'), self._payload(
            ds_n1=SimpleUploadedFile('big.pdf', make_pdf(pages=11), 'application/pdf')))
        self.assertContains(resp, 'max 10')
        self.assertFalse(Quotation.objects.exists())

    def test_rows_are_never_split_across_pages(self):
        n = 40
        descs = [f'START{i:02d} ' + 'long description words ' * 25 + f'END{i:02d}' for i in range(n)]
        payload = self._payload(
            row_key=[f'n{i}' for i in range(n)], item_id=[''] * n, item_no=[''] * n, item_model=[''] * n,
            item_brand=['ARISTON'] * n, item_description=descs,
            item_size=[''] * n, item_color=[''] * n, item_qty=['1'] * n, item_price=['10'] * n,
            item_warranty=[''] * n, remove_photo=['0'] * n, remove_ds=['0'] * n)
        self.client.post(reverse('quotation:new'), payload)
        q = Quotation.objects.get()
        reader = PdfReader(BytesIO(self.client.get(reverse('quotation:pdf', args=[q.pk])).content))
        self.assertGreater(len(reader.pages), 3)
        texts = [p.extract_text().replace('\n', ' ') for p in reader.pages]
        for i in range(n):
            page_of_start = [k for k, t in enumerate(texts) if f'START{i:02d}' in t]
            page_of_end = [k for k, t in enumerate(texts) if f'END{i:02d}' in t]
            self.assertEqual(page_of_start, page_of_end, f'item {i} was split across pages')
        # Total, VAT and Grand Total stay together on one page, with the last item above them.
        vat_pages = [k for k, t in enumerate(texts) if 'VAT 5% (AED)' in t]
        self.assertEqual(vat_pages, [k for k, t in enumerate(texts) if 'GRAND TOTAL (AED)' in t])
        self.assertEqual(len(vat_pages), 1)
        self.assertIn(f'START{n - 1:02d}', texts[vat_pages[0]])

    def test_pdf_survives_many_items_and_a_description_longer_than_a_page(self):
        long_desc = 'FIRSTWORD ' + 'Concealed flushing cistern with steel support frame and fixing set. ' * 60 + 'LASTWORD'   # ~4,000 chars
        payload = self._payload(
            row_key=[f'n{i}' for i in range(30)], item_id=[''] * 30, item_no=[''] * 30, item_model=[''] * 30,
            item_brand=['ARISTON'] * 30, item_description=[long_desc if i == 5 else f'Item {i}' for i in range(30)],
            item_size=[''] * 30, item_color=[''] * 30, item_qty=['1'] * 30, item_price=['10'] * 30,
            item_warranty=[''] * 30, remove_photo=['0'] * 30, remove_ds=['0'] * 30)
        self.client.post(reverse('quotation:new'), payload)
        q = Quotation.objects.get()
        resp = self.client.get(reverse('quotation:pdf', args=[q.pk]))
        self.assertEqual(resp.status_code, 200)
        reader = PdfReader(BytesIO(resp.content))
        self.assertGreater(len(reader.pages), 2)
        everything = '\n'.join(p.extract_text() for p in reader.pages)
        for needle in ('TOTAL (AED)', '300.00', 'FIRSTWORD', 'LASTWORD'):   # nothing of the long text is lost
            self.assertIn(needle, everything)
        flat = ' '.join(everything.split())   # the repeated table header can fall inside a phrase, so count words
        for word in ('Concealed', 'cistern', 'support', 'fixing', 'set.'):
            self.assertEqual(flat.count(word), 60, word)
        last = reader.pages[-1].extract_text()
        for needle in ('BANK ACCOUNT DETAILS', 'CONTACT'):
            self.assertIn(needle, last)

    def test_stock_availability_is_a_date_and_project_reference_is_optional(self):
        resp = self.client.post(reverse('quotation:new'), self._payload('QTN-A', stock_availability='soon'))
        self.assertContains(resp, 'valid Stock Availability date')
        self.assertFalse(Quotation.objects.exists())
        # Both optional: blank date and blank project reference save fine and print nothing for them.
        self.client.post(reverse('quotation:new'), self._payload('QTN-B', stock_availability='', project_reference=''))
        q = Quotation.objects.get(qtn_no='QTN-B')
        self.assertIsNone(q.stock_availability)
        reader = PdfReader(BytesIO(self.client.get(reverse('quotation:pdf', args=[q.pk])).content))
        self.assertNotIn('PROJECT REF', reader.pages[0].extract_text())
        # The edit form shows the saved date in the date box.
        self.client.post(reverse('quotation:edit', args=[q.pk]), self._payload(
            'QTN-B', stock_availability='2026-12-01', row_key=[f'e{i.pk}' for i in q.items.all()],
            item_id=[str(i.pk) for i in q.items.all()]))
        page = self.client.get(reverse('quotation:edit', args=[q.pk]))
        self.assertContains(page, 'type="date" name="stock_availability"')
        self.assertContains(page, 'value="2026-12-01"')

    def test_pdf_orientation_option(self):
        self.client.post(reverse('quotation:new'), self._payload(
            ds_n1=SimpleUploadedFile('sheet.pdf', PDF_BYTES, 'application/pdf')))
        q = Quotation.objects.get()
        url = reverse('quotation:pdf', args=[q.pk])
        item = q.items.first()
        for query, landscape in (('', False), ('?orientation=portrait', False), ('?orientation=landscape', True),
                                 ('?orientation=junk', False)):
            resp = self.client.get(url + query)
            self.assertEqual(resp.status_code, 200, query)
            pdf_pages = PdfReader(BytesIO(resp.content)).pages
            page = pdf_pages[0]
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            self.assertEqual(w > h, landscape, query)
            self.assertAlmostEqual(max(w, h), 841.9, delta=0.5)   # always A4
            self.assertAlmostEqual(min(w, h), 595.3, delta=0.5)
            text = '\n'.join(p.extract_text() for p in pdf_pages)   # the closing blocks may move to a 2nd page
            for needle in ('QUOTATION', 'QTN-1', 'H12', 'TERMS AND CONDITIONS', 'IBAN', 'Download'):
                self.assertIn(needle, text, (query, needle))
            uris = [a.get_object()['/A']['/URI'] for a in page.get('/Annots', [])]
            self.assertIn(f'https://example.test/quote-builder/ds/{item.share_token}/', uris, query)   # links survive
            self.assertIn('Landscape' if landscape else 'Quotation_QTN-1.pdf', resp['Content-Disposition'])

    def test_landscape_rows_still_never_split_and_survive_huge_descriptions(self):
        n = 30
        descs = [f'START{i:02d} ' + 'long description words ' * 25 + f'END{i:02d}' for i in range(n)]
        descs[7] = 'FIRSTWORD ' + 'Concealed flushing cistern with steel support frame and fixing set. ' * 60 + 'LASTWORD'
        payload = self._payload(
            row_key=[f'n{i}' for i in range(n)], item_id=[''] * n, item_no=[''] * n, item_model=[''] * n,
            item_brand=['ARISTON'] * n, item_description=descs,
            item_size=[''] * n, item_color=[''] * n, item_qty=['1'] * n, item_price=['10'] * n,
            item_warranty=[''] * n, remove_photo=['0'] * n, remove_ds=['0'] * n)
        self.client.post(reverse('quotation:new'), payload)
        q = Quotation.objects.get()
        resp = self.client.get(reverse('quotation:pdf', args=[q.pk]) + '?orientation=landscape')
        self.assertEqual(resp.status_code, 200)
        reader = PdfReader(BytesIO(resp.content))
        texts = [p.extract_text().replace('\n', ' ') for p in reader.pages]
        for i in range(n):
            if i == 7:
                continue
            self.assertEqual([k for k, t in enumerate(texts) if f'START{i:02d}' in t],
                             [k for k, t in enumerate(texts) if f'END{i:02d}' in t], f'item {i} was split')
        everything = ' '.join(' '.join(texts).split())
        self.assertIn('FIRSTWORD', everything)
        self.assertIn('LASTWORD', everything)
        self.assertIn('IBAN', texts[-1])

    def test_vat_is_five_percent_rounded_to_the_cent_and_amounts_add_up(self):
        q = Quotation.objects.create(qtn_no='V-1', date='2026-10-20')
        QuotationItem.objects.create(quotation=q, position=0, brand='A', qty=Decimal('0.50'), unit_price=Decimal('0.01'))   # 0.005
        QuotationItem.objects.create(quotation=q, position=1, brand='B', qty=Decimal('10'), unit_price=Decimal('0.01'))      # 0.10
        a, b = q.items.all()
        self.assertEqual((a.amount, b.amount), (Decimal('0.01'), Decimal('0.10')))   # a line rounds half up to the cent
        self.assertEqual(q.total, Decimal('0.11'))                                    # = the sum of the printed lines
        self.assertEqual(q.vat_amount, Decimal('0.01'))                               # 0.0055 -> 0.01
        self.assertEqual(q.grand_total, Decimal('0.12'))
        a.delete()
        self.assertEqual(q.vat_amount, Decimal('0.01'))                               # 0.005 rounds half UP, not to even
        empty = Quotation.objects.create(qtn_no='V-2', date='2026-10-20')
        self.assertEqual((empty.total, empty.vat_amount, empty.grand_total), (Decimal('0'), Decimal('0.00'), Decimal('0.00')))

    def test_totals_show_in_list_and_form(self):
        self.client.post(reverse('quotation:new'), self._payload())
        q = Quotation.objects.get()
        listing = self.client.get(reverse('quotation:list'))
        self.assertContains(listing, 'Grand Total (AED)')
        self.assertContains(listing, '1107.75')
        form = self.client.get(reverse('quotation:edit', args=[q.pk]))
        for needle in ('id="tTotal"', 'id="tVat"', 'id="tGrand"', 'VAT 5%'):
            self.assertContains(form, needle)

    def test_other_apps_untouched(self):
        # The new app must not interfere with the Bangladesh PI routes.
        self.assertEqual(self.client.get(reverse('bangladeshpi:list')).status_code, 200)

"""The details typed on a NEW quotation must look real: contact number (mobile or landline), email, walk-in display name.

* so/quotation_input_checks.py holds the rules; so/templates/so/_input_checks_js.html is the browser copy.
* Old quotations and the edit pages are untouched.
"""
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from emailagent.models import TrackedEmail
from . import quotation_input_checks as checks
from .models import Customer, Quotation
from .tests_alabama_catalog import HOSTS, Base

TMP_MEDIA = tempfile.mkdtemp(prefix='input_checks_media_')   # uploaded test files never touch the real media folder
JS_FILE = Path(__file__).parent / 'templates' / 'so' / '_input_checks_js.html'


class InputCheckRulesTests(SimpleTestCase):
    def test_contact_number_accepts_real_mobiles_and_landlines(self):
        for value in ('0501234567', '0507654321', '050 123 4567', '04-3362775', '043362775', '+971 4 5015902', '+971505961607',
                      '971-4-2672299', '(04) 336 2775', '04-3404440 /3472100', '04-2611164/67', '050-5145468,050-2554',
                      '04-3362775 ext 12', '04-3403911-3406733', '2(055)4351763-351762'):
            self.assertEqual(checks.validate_contact_number(value), '', value)

    def test_contact_number_rejects_names_text_and_fake_numbers(self):
        for value in ('nfg', 'Ahmed', 'CASH ONLY', '0501234567 Ahmed', 'abc123', '12345', '123456', '05+0123456',
                      '1234567890', '0123456789', '9876543210', '0000000000', '5555555', '0500000000', '123123123', '0505050505',
                      '1' * 21):
            self.assertNotEqual(checks.validate_contact_number(value), '', value)

    def test_empty_values_are_not_judged_here(self):
        for fn in (checks.validate_contact_number, checks.validate_customer_email, checks.validate_display_name):
            self.assertEqual(fn(''), '')
            self.assertEqual(fn('   '), '')
            self.assertEqual(fn(None), '')

    def test_email_accepts_real_addresses_including_several(self):
        for value in ('buyer@acmetrading.ae', 'Name.Surname+tag@Sub.Company.co.uk', 'mail@3demirates.ae', 'info@mac-mep.com',
                      'a@x-y.ae; b@z.ae', 'info@alramsprime.com/mohamed.atta@alramsprime.com',
                      'accounts@alshadouf.com-huzaifa@alshadouf.com', 'a@mac-mep.com-b@xyz.ae', "'payables@alsafraduae.com'"):
            self.assertEqual(checks.validate_customer_email(value), '', value)

    def test_email_rejects_malformed_and_placeholder_addresses(self):
        for value in ('hello', 'a@b', 'nfg@', '@x.ae', 'x@.com', 'x@x..com', 'user@localhost', '+971563223333',
                      'test@gmail.com', 'x@test.com', 'buyer@example.com', 'me@abc.com', 'aaa@x.ae', 'no@x.ae',
                      'manual-upload@upload.local'):
            self.assertNotEqual(checks.validate_customer_email(value), '', value)

    def test_display_name_accepts_real_names(self):
        for value in ('Mr Ahmed', 'Al Noor Trading', 'Mr. Ng', 'ABC Trading', 'Mr Ahmed (Marina site)', 'محمد علي'):
            self.assertEqual(checks.validate_display_name(value), '', value)

    def test_display_name_rejects_placeholders_and_junk(self):
        for value in ('xx', '123', '...', 'aaa', 'Test', 'n/a', 'NA', 'asdf', 'qwerty uiop', 'Cash Customer', 'Walk-in',
                      'Mr 12', 'a' * 101):
            self.assertNotEqual(checks.validate_display_name(value), '', value)

    def test_browser_copy_has_the_same_word_lists_and_messages(self):
        js = JS_FILE.read_text(encoding='utf-8')
        words = (checks.JUNK_EMAIL_DOMAINS | checks.JUNK_EMAIL_TLDS | checks.JUNK_EMAIL_LOCALS | checks.JUNK_NAMES
                 | set(checks.KEYBOARD_MASH))
        for word in words:
            self.assertIn("'%s'" % word, js, word)
        for message in (checks.PHONE_MESSAGE, checks.PHONE_FAKE_MESSAGE, checks.EMAIL_MESSAGE, checks.EMAIL_FAKE_MESSAGE,
                        checks.NAME_MESSAGE, checks.NAME_FAKE_MESSAGE):
            self.assertIn(message, js)
        self.assertIn('PHONE_MIN_DIGITS = %d' % checks.PHONE_MIN_DIGITS, js)
        self.assertIn('PHONE_MAX_DIGITS = %d' % checks.PHONE_MAX_DIGITS, js)
        self.assertIn('PHONE_MAX_RUN = %d' % checks.PHONE_MAX_RUN, js)


@override_settings(ALLOWED_HOSTS=HOSTS, MEDIA_ROOT=TMP_MEDIA)
class InputChecksOnTheFormsTests(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.walkin = Customer.objects.create(customer_code='W1', customer_name='CASH CUSTOMER - DEIRA', salesman=self.salesman)
        self.login(self.junaid)

    # ── in-app create form ────────────────────────────────────────────────
    def _create(self, customer, **extra):
        data = {'customer': customer.id, 'salesman': self.salesman.id, 'remarks': '',
                'customer_contact_number': '0501234567', 'customer_email': 'buyer@acmetrading.ae',
                'item': ['1'], 'quantity': ['1'], 'price': ['10'], 'unit': ['pcs']}
        data.update(extra)
        return self.client.post(reverse('create_quotation'), data)

    def test_create_form_rejects_junk_details(self):
        from django.contrib.messages import get_messages
        for field, value in (('customer_contact_number', 'nfg'), ('customer_contact_number', '0000000000'),
                             ('customer_email', 'nobody'), ('customer_email', 'test@gmail.com')):
            resp = self._create(self.cust, **{field: value})
            self.assertEqual(Quotation.objects.count(), 0, value)
            self.assertTrue(list(get_messages(resp.wsgi_request)), value)
        resp = self._create(self.walkin, new_customer_name='asdf')
        self.assertEqual(Quotation.objects.count(), 0)
        resp = self._create(self.cust, customer_email='', customer_email_later='on', customer_contact_number='Ahmed')
        self.assertEqual(Quotation.objects.count(), 0)                       # the tick never excuses a fake number

    def test_create_form_accepts_real_mobile_and_landline_and_walkin_name(self):
        self._create(self.cust, customer_contact_number='04-3362775')
        self.assertEqual(Quotation.objects.get().customer_contact_number, '04-3362775')
        Quotation.objects.all().delete()
        self._create(self.walkin, new_customer_name='Mr Ahmed (Marina site)', customer_contact_number='+971 50 123 4567')
        self.assertEqual(Quotation.objects.get().customer_display_name, 'Mr Ahmed (Marina site)')

    def test_pages_carry_the_browser_checks(self):
        self.assertIn('window.QInputChecks', self.client.get(reverse('create_quotation')).content.decode())
        self.assertIn('window.QInputChecks', self.client.get(reverse('emailagent:upload_enquiry')).content.decode())
        self.assertIn('window.QInputChecks', self.client.get(reverse('quotation:new')).content.decode())

    # ── manual enquiry upload ─────────────────────────────────────────────
    def _upload(self, customer=None, **extra):
        data = {'files': SimpleUploadedFile('boq.pdf', b'%PDF-1.4 x', content_type='application/pdf'),
                'customer_contact_number': '0501234567', 'customer_email': 'buyer@acmetrading.ae',
                'customer_display_name': 'Mr Ahmed'}
        if customer is not None:
            data['customer_id'] = customer.id
        data.update(extra)
        with mock.patch('emailagent.upload_enquiry.start_processing') as start:
            resp = self.client.post(reverse('emailagent:upload_enquiry'), data)
        return resp, start

    def test_upload_rejects_junk_details(self):
        for field, value in (('customer_contact_number', 'nfg'), ('customer_email', 'upload@upload.local'),
                             ('customer_display_name', 'xx')):
            resp, start = self._upload(None, **{field: value})
            self.assertEqual(resp.status_code, 400, value)
            self.assertFalse(resp.json()['ok'])
            start.assert_not_called()
        self.assertEqual(TrackedEmail.objects.count(), 0)

    def test_upload_accepts_real_details_and_a_landline(self):
        resp, start = self._upload(None, customer_contact_number='06-5734646')
        self.assertEqual(resp.status_code, 200, resp.content)
        start.assert_called_once()
        # a real customer is picked: the display name is optional, so it is not judged
        resp, _ = self._upload(self.cust, customer_display_name='xx')
        self.assertEqual(resp.status_code, 200, resp.content)

    # ── standalone quotation system ───────────────────────────────────────
    def test_standalone_new_quotation_rejects_junk_but_old_ones_still_edit(self):
        from quotation.models import Quotation as StandaloneQuotation
        from quotation.tests import QuotationTests
        payload = QuotationTests._payload
        url = reverse('quotation:new')
        for field, value in (('customer_contact', 'nfg'), ('customer_email', 'test@gmail.com')):
            resp = self.client.post(url, payload(None, **{field: value}))
            self.assertEqual(resp.status_code, 200, value)
            self.assertTrue(resp.context['errors'], value)
            self.assertEqual(StandaloneQuotation.objects.count(), 0, value)
        resp = self.client.post(url, payload(None, customer_contact='04-3362775'))
        self.assertEqual(resp.status_code, 302, getattr(resp, 'context', None) and resp.context['errors'])
        self.assertEqual(StandaloneQuotation.objects.get().customer_contact, '04-3362775')
        # an existing quotation keeps saving whatever it already holds (edit page unchanged)
        q = StandaloneQuotation.objects.get()
        resp = self.client.post(reverse('quotation:edit', args=[q.pk]), payload(None, customer_contact='nfg', customer_email=''))
        self.assertEqual(resp.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.customer_contact, 'nfg')

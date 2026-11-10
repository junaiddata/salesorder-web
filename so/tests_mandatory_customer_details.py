"""Mandatory customer details on NEW quotations created by hand.

* Customer contact number and email are required on the in-app create form and on the manual enquiry upload page.
* A generic CASH / walk-in customer (see quotation_customer_details.WALKIN_NAME_PHRASES) also needs a display name.
* Real companies that merely pay cash ("... (CASH)") are NOT walk-ins.
* The edit page and old quotations are untouched."""
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from emailagent import upload_enquiry as upload_service
from emailagent.models import QuotationDraft, TrackedEmail
from .models import Customer, Items, ItemSyncStatus, Quotation
from .quotation_customer_details import is_walkin_customer
from .tests_alabama_catalog import HOSTS, Base


TMP_MEDIA = tempfile.mkdtemp(prefix='mandatory_details_media_')   # uploaded test files never touch the real media folder


@override_settings(ALLOWED_HOSTS=HOSTS, MEDIA_ROOT=TMP_MEDIA)
class MandatoryCustomerDetailsTests(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.walkin = Customer.objects.create(customer_code='W1', customer_name='CASH CUSTOMER - DEIRA', salesman=self.salesman)
        self.cash_company = Customer.objects.create(customer_code='W2', customer_name='AL HATIMI TRADING L.L.C (CASH)',
                                                    salesman=self.salesman)
        self.login(self.junaid)

    def _create(self, customer, **extra):
        data = {'customer': customer.id, 'salesman': self.salesman.id, 'remarks': '',
                'customer_contact_number': '0501234567', 'customer_email': 'buyer@example.com',
                'item': ['1'], 'quantity': ['1'], 'price': ['10'], 'unit': ['pcs']}
        data.update(extra)
        return self.client.post(reverse('create_quotation'), data)

    # ── which customers count as walk-in ──────────────────────────────────
    def test_walkin_detection_matches_generic_customers_only(self):
        for name in ('CASH CUSTOMER', 'Cash Customer - Junaid', 'DEBIT CUSTOMER ( CASH )', 'CASH SALES - SCRAP',
                     'RETAIL CUSTOMERS ( CASH )', 'MR DAOUD N.A.H ( CASH CUSTOMER )', '  cash   customer  deira '):
            self.assertTrue(is_walkin_customer(name), name)
        for name in ('AL HATIMI TRADING L.L.C (CASH)', 'AL BASHIR FOR RETAIL OF SANITARY (CASH)', 'Cust One', '', None):
            self.assertFalse(is_walkin_customer(name), name)
        self.assertTrue(is_walkin_customer(self.walkin))
        self.assertFalse(is_walkin_customer(self.cash_company))
        self.assertFalse(is_walkin_customer(None))

    def test_customer_details_json_says_whether_it_is_a_walkin(self):
        url = reverse('quotation_customer_details')
        self.assertTrue(self.client.get(url, {'customer_id': self.walkin.id}).json()['is_walkin'])
        self.assertFalse(self.client.get(url, {'customer_id': self.cash_company.id}).json()['is_walkin'])
        self.assertEqual(self.client.get(url, {'customer_id': 0}).json(), {})

    # ── in-app create form ────────────────────────────────────────────────
    def test_walkin_customer_needs_a_display_name_on_create(self):
        self._create(self.walkin)
        self.assertEqual(Quotation.objects.count(), 0)
        self._create(self.walkin, new_customer_name='   ')
        self.assertEqual(Quotation.objects.count(), 0)
        self._create(self.walkin, new_customer_name='Mr Ahmed (Marina site)')
        self.assertEqual(Quotation.objects.get().customer_display_name, 'Mr Ahmed (Marina site)')

    def test_real_company_paying_cash_needs_no_display_name(self):
        self._create(self.cash_company)
        self.assertEqual(Quotation.objects.get().customer, self.cash_company)

    def test_create_form_still_needs_contact_and_email_for_walkin(self):
        self._create(self.walkin, new_customer_name='Mr Ahmed', customer_email='')
        self.assertEqual(Quotation.objects.count(), 0)

    def test_create_page_has_the_display_name_toggle(self):
        page = self.client.get(reverse('create_quotation')).content.decode()
        self.assertIn('setDisplayNameRequired', page)
        self.assertIn('id="new_customer_name"', page)

    # ── manual enquiry upload ─────────────────────────────────────────────
    def _upload(self, customer=None, **extra):
        data = {'files': SimpleUploadedFile('boq.pdf', b'%PDF-1.4 x', content_type='application/pdf'),
                'customer_contact_number': '0501234567', 'customer_email': 'buyer@example.com',
                'customer_display_name': 'Mr Ahmed'}
        if customer is not None:
            data['customer_id'] = customer.id
        data.update(extra)
        with mock.patch('emailagent.upload_enquiry.start_processing') as start:
            resp = self.client.post(reverse('emailagent:upload_enquiry'), data)
        return resp, start

    def test_upload_needs_contact_number_and_email_always(self):
        for field in ('customer_contact_number', 'customer_email'):
            resp, start = self._upload(self.cust, **{field: '  '})
            self.assertEqual(resp.status_code, 400, field)
            self.assertFalse(resp.json()['ok'])
            start.assert_not_called()
        self.assertEqual(TrackedEmail.objects.count(), 0)

    def test_upload_needs_display_name_for_walkin_or_no_customer_only(self):
        for customer in (self.walkin, None):
            resp, _ = self._upload(customer, customer_display_name='')
            self.assertEqual(resp.status_code, 400)
            self.assertIn('display name', resp.json()['error'])
        resp, start = self._upload(self.cust, customer_display_name='')      # a real customer is picked: fine
        self.assertEqual(resp.status_code, 200, resp.content)
        start.assert_called_once()
        resp, _ = self._upload(self.walkin, customer_display_name='Mr Ahmed')
        self.assertEqual(resp.status_code, 200, resp.content)
        resp, _ = self._upload(None, customer_display_name='Mr Ahmed')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_upload_page_has_the_two_new_boxes(self):
        page = self.client.get(reverse('emailagent:upload_enquiry')).content.decode()
        self.assertIn('name="customer_contact_number"', page)
        self.assertIn('name="customer_email"', page)

    def test_upload_stores_contact_and_email_and_copies_them_onto_the_quotation(self):
        resp, _ = self._upload(self.cust, customer_display_name='')
        self.assertEqual(resp.status_code, 200, resp.content)
        te = TrackedEmail.objects.get()
        self.assertEqual((te.upload_customer_contact_number, te.upload_customer_email), ('0501234567', 'buyer@example.com'))
        # what the background step does once the agent has drafted the quotation
        q = Quotation.objects.create(customer=self.cust, salesman=self.salesman)
        QuotationDraft.objects.create(tracked_email=te, status=QuotationDraft.STATUS_CONFIRMED, quotation=q)
        upload_service._apply_typed_names(te)
        q.refresh_from_db()
        self.assertEqual((q.customer_contact_number, q.customer_email), ('0501234567', 'buyer@example.com'))

    def test_upload_without_the_new_values_still_works_for_existing_callers(self):
        te = upload_service.create_upload(self.junaid, [SimpleUploadedFile('a.pdf', b'%PDF-1.4 x', content_type='application/pdf')])
        self.assertEqual((te.upload_customer_contact_number, te.upload_customer_email), ('', ''))

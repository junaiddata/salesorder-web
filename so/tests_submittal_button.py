"""'Generate Submittal' is offered on Junaid quotations only (the submittal agent is Junaid-only). Alabama quotations
do not show it; an existing submittal is still reachable through 'View Submittal'."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from submittal.models import Submittal

from .models import Customer, Items, Quotation, QuotationItem, Role, Salesman


@override_settings(ALLOWED_HOSTS=['testserver'])
class GenerateSubmittalButtonTests(TestCase):
    def setUp(self):
        sm = Salesman.objects.create(salesman_name='KADER')
        cust = Customer.objects.create(customer_code='C1', customer_name='Cust', salesman=sm)
        item = Items.objects.create(item_code='A1', item_description='Thing', item_firm='PEGLER', item_cost=1, item_price=2)
        self.junaid_q = Quotation.objects.create(customer=cust, salesman=sm, division='JUNAID')
        self.alabama_q = Quotation.objects.create(customer=cust, salesman=sm, division='ALABAMA')
        for q in (self.junaid_q, self.alabama_q):
            QuotationItem.objects.create(quotation=q, item=item, quantity=1, price=2, line_total=2)
        admin = User.objects.create_user('jadmin', password='x')
        Role.objects.create(user=admin, role='Admin', company='Junaid')
        self.client.force_login(admin)

    def page(self, q):
        resp = self.client.get(reverse('view_quotation_details', args=[q.id]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_junaid_quotation_still_offers_generate_submittal(self):
        html = self.page(self.junaid_q)
        self.assertIn('data-bs-target="#generateSubmittalModal"', html)
        self.assertIn('id="generateSubmittalModal"', html)
        self.assertIn('id="generateSubmittalForm"', html)

    def test_alabama_quotation_does_not(self):
        html = self.page(self.alabama_q)
        self.assertNotIn('id="generateSubmittalModal"', html)
        self.assertNotIn('data-bs-target="#generateSubmittalModal"', html)
        self.assertNotIn('id="generateSubmittalForm"', html)
        self.assertNotIn('fa-file-circle-plus', html)           # the button / modal icon

    def test_an_existing_submittal_is_still_linked_on_an_alabama_quotation(self):
        sub = Submittal.objects.create(project='P', company='alabama', source_quotation=self.alabama_q)
        html = self.page(self.alabama_q)
        self.assertIn('View Submittal', html)
        self.assertIn(reverse('submittal:detail', args=[sub.pk]), html)
        self.assertNotIn('id="generateSubmittalModal"', html)

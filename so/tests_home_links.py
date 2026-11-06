"""The Home / Back-to-Home links on the quotation and sales-order pages go to the user's OWN company home
(same rule as the login redirect): an Alabama user is not sent to the Junaid dashboards."""
import re

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Customer, Items, OrderItem, Quotation, QuotationItem, Role, SalesOrder, Salesman

HOME = reverse('home')                                  # Junaid admin dashboard
SALES_HOME = reverse('sales_home')                      # Junaid salesman dashboard
AL_HOME = reverse('alabama:home')                       # Alabama admin home
AL_SALES_HOME = reverse('alabama:alabama_sales_home')   # Alabama salesman home


@override_settings(ALLOWED_HOSTS=['testserver'])
class HomeLinkTests(TestCase):
    def setUp(self):
        self.salesman = Salesman.objects.create(salesman_name='KADER')
        self.cust = Customer.objects.create(customer_code='C1', customer_name='Cust', salesman=self.salesman)
        self.item = Items.objects.create(item_code='A1', item_description='Thing', item_firm='X', item_cost=1, item_price=2)
        self.users = {
            'al_sales': self._user('alabamakadhar', 'Salesman', 'Alabama'),
            'al_sales_role_only': self._user('alrole_sales', 'Salesman', 'Alabama'),
            'al_admin': self._user('alrole_admin', 'Admin', 'Alabama'),
            'j_sales': self._user('jsam', 'Salesman', 'Junaid'),
            'j_admin': self._user('jadmin', 'Admin', 'Junaid'),
            'so': self._user('so', 'Admin', 'Junaid'),
            'manager': self._user('manager', 'Admin', 'Junaid'),
        }
        self.quotation = Quotation.objects.create(customer=self.cust, salesman=self.salesman, division='ALABAMA')
        QuotationItem.objects.create(quotation=self.quotation, item=self.item, quantity=1, price=2, line_total=2)
        self.order = SalesOrder.objects.create(customer=self.cust, salesman=self.salesman, division='ALABAMA')
        OrderItem.objects.create(order=self.order, item=self.item, quantity=1, price=2)

    def _user(self, name, role, company):
        u = User.objects.create_user(name, password='x', is_superuser=False)
        Role.objects.create(user=u, role=role, company=company)
        return u

    def link(self, who, url, pattern):
        self.client.force_login(self.users[who])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, (who, url))
        found = re.findall(pattern, resp.content.decode())
        self.assertTrue(found, (who, url, 'home link not found'))
        return found[0]

    LIST_BTN = r'<a href="([^"]+)" class="btn btn-light">\s*<i class="fas fa-home me-2"></i>Back to (?:Home|Alabama)'
    COMBINED_BTN = r'<a href="([^"]+)" class="btn btn-outline-light btn-sm"><i class="fas fa-home me-1"></i>Home</a>'
    CRUMB = r'<a href="([^"]+)">Home</a>'

    # expected home per user type
    EXPECTED = {
        'al_sales': AL_SALES_HOME, 'al_sales_role_only': AL_SALES_HOME, 'al_admin': AL_HOME,
        'j_sales': SALES_HOME, 'j_admin': HOME, 'so': HOME, 'manager': HOME,
    }

    def test_quotation_list_back_button(self):
        for who, expected in self.EXPECTED.items():
            self.assertEqual(self.link(who, reverse('view_quotations'), self.LIST_BTN), expected, who)

    def test_combined_quotations_home_button(self):
        for who, expected in self.EXPECTED.items():
            self.assertEqual(self.link(who, reverse('combined_quotations_list'), self.COMBINED_BTN), expected, who)

    def test_quotation_details_breadcrumb(self):
        for who, expected in self.EXPECTED.items():
            self.assertEqual(self.link(who, reverse('view_quotation_details', args=[self.quotation.id]), self.CRUMB), expected, who)

    def test_sales_order_list_back_button(self):
        for who, expected in self.EXPECTED.items():
            self.assertEqual(self.link(who, reverse('view_sales_orders'), self.LIST_BTN), expected, who)

    def test_sales_order_details_breadcrumb(self):
        for who in ('al_admin', 'j_admin', 'so', 'manager'):
            self.assertEqual(self.link(who, reverse('view_sales_order_details', args=[self.order.id]), self.CRUMB),
                             self.EXPECTED[who], who)

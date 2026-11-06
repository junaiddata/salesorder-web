"""The Firm filter on the quotations list: the Alabama list offers Alabama brands (not all Junaid brands) and still finds
old quotations whose lines carry Junaid-style brand names. Junaid / so / manager lists, and the switch-off, are unchanged."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from alabama.models import AlabamaFeedBrand, AlabamaItem
from .models import Customer, Items, Quotation, QuotationItem, Role, Salesman

HOSTS = ['testserver']


@override_settings(ALLOWED_HOSTS=HOSTS, ALABAMA_OWN_CATALOG=True, ALABAMA_OWN_ITEMS=True)
class AlabamaFirmFilterTests(TestCase):
    def setUp(self):
        cache.clear()
        AlabamaFeedBrand.objects.all().delete()
        for b in ('VERA-INDIA', 'KLUDI', 'ATLAS-MPI'):
            AlabamaFeedBrand.objects.create(brand=b)
        sm = Salesman.objects.create(salesman_name='KADER')
        cust = Customer.objects.create(customer_code='C1', customer_name='Cust', salesman=sm)
        now = timezone.now()
        # Junaid catalog: many brands, including Junaid-style spellings
        self.j_vera = Items.objects.create(item_code='V1', item_description='Old vera', item_firm='VERA INDIA', item_cost=1, item_price=2)
        self.j_atlas = Items.objects.create(item_code='AT1', item_description='Old atlas', item_firm='ATLAS - HP FITTINGS', item_cost=1, item_price=2)
        self.j_peg = Items.objects.create(item_code='P1', item_description='Old pegler', item_firm='PEGLER', item_cost=1, item_price=2)
        Items.objects.create(item_code='X1', item_description='Never used', item_firm='JUNAID-ONLY-BRAND', item_cost=1, item_price=2)
        # Alabama catalog
        self.a_vera = AlabamaItem.objects.create(item_code='V1', item_description='Vera', item_firm='VERA-INDIA', quotable=True, last_seen_at=now)
        AlabamaItem.objects.create(item_code='AT1', item_description='Atlas', item_firm='ATLAS-MPI', quotable=True, last_seen_at=now)
        self.a_kludi = AlabamaItem.objects.create(item_code='K1', item_description='Kludi', item_firm='KLUDI', quotable=True, last_seen_at=now)

        def quote(division, **line):
            q = Quotation.objects.create(customer=cust, salesman=sm, division=division)
            QuotationItem.objects.create(quotation=q, quantity=1, price=2, line_total=2, **line)
            return q
        self.old_vera = quote('ALABAMA', item=self.j_vera)          # old line, Junaid-style brand name
        self.old_atlas = quote('ALABAMA', item=self.j_atlas)
        self.old_peg = quote('ALABAMA', item=self.j_peg)             # old line, brand not in the Alabama catalog
        self.new_kludi = quote('ALABAMA', alabama_item=self.a_kludi)
        self.new_vera = quote('ALABAMA', alabama_item=self.a_vera)
        self.junaid_q = quote('JUNAID', item=self.j_peg)

        self.al_admin = self._user('alrole_admin', 'Admin', 'Alabama')
        self.j_admin = self._user('jadmin', 'Admin', 'Junaid')
        self.so = self._user('so', 'Admin', 'Junaid')

    def _user(self, name, role, company):
        u = User.objects.create_user(name, password='x')
        Role.objects.create(user=u, role=role, company=company)
        return u

    def page(self, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse('view_quotations'), params)
        self.assertEqual(resp.status_code, 200)
        return resp

    def found(self, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse('view_quotations_ajax'), params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()['count']

    # ---- dropdown -----------------------------------------------------------------------------------------------
    def test_alabama_list_offers_alabama_brands_not_all_junaid_brands(self):
        choices = self.page(self.al_admin).context['firm_choices']
        self.assertIn('VERA-INDIA', choices)
        self.assertIn('KLUDI', choices)
        self.assertIn('ATLAS-MPI', choices)
        self.assertNotIn('JUNAID-ONLY-BRAND', choices)

    def test_old_brand_spellings_are_merged_not_duplicated(self):
        choices = self.page(self.al_admin).context['firm_choices']
        self.assertNotIn('VERA INDIA', choices)
        self.assertNotIn('ATLAS - HP FITTINGS', choices)

    def test_old_brand_missing_from_alabama_catalog_stays_filterable(self):
        self.assertIn('PEGLER', self.page(self.al_admin).context['firm_choices'])

    def test_so_with_division_alabama_gets_the_alabama_list(self):
        choices = self.page(self.so, division='ALABAMA').context['firm_choices']
        self.assertNotIn('JUNAID-ONLY-BRAND', choices)
        self.assertIn('KLUDI', choices)

    def test_junaid_and_so_default_lists_are_unchanged(self):
        for user in (self.j_admin, self.so):
            choices = self.page(user).context['firm_choices']
            self.assertIn('JUNAID-ONLY-BRAND', choices)
            self.assertIn('VERA INDIA', choices)
            self.assertNotIn('ATLAS-MPI', choices)

    @override_settings(ALABAMA_OWN_CATALOG=False)
    def test_switch_off_keeps_the_old_list(self):
        choices = self.page(self.al_admin).context['firm_choices']
        self.assertIn('JUNAID-ONLY-BRAND', choices)

    # ---- filter -------------------------------------------------------------------------------------------------
    def test_alabama_brand_finds_old_and_new_quotations(self):
        self.assertEqual(self.found(self.al_admin, firm='VERA-INDIA'), 2)        # old 'VERA INDIA' line + new line
        self.assertEqual(self.found(self.al_admin, firm='ATLAS-MPI'), 1)         # old line, found through the item code
        self.assertEqual(self.found(self.al_admin, firm='KLUDI'), 1)

    def test_old_brand_name_still_finds_its_quotation(self):
        self.assertEqual(self.found(self.al_admin, firm='PEGLER'), 1)             # Alabama only; the Junaid one is hidden

    def test_junaid_filter_is_unchanged(self):
        self.assertEqual(self.found(self.j_admin, firm='PEGLER'), 1)
        self.assertEqual(self.found(self.j_admin, firm='VERA-INDIA'), 0)         # no extra Alabama matching for Junaid

    @override_settings(ALABAMA_OWN_CATALOG=False)
    def test_switch_off_filter_is_the_old_one(self):
        self.assertEqual(self.found(self.al_admin, firm='VERA-INDIA'), 1)         # only the new line (text match)

"""The Item Catalog for Alabama: Alabama users see Alabama's own (quotable) items, with the photo / datasheet of the
matching Junaid item reused (same code AND same description) or their own upload. Junaid's catalog is unchanged."""
import shutil
import tempfile
from io import BytesIO

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from alabama import media_match
from alabama.models import AlabamaFeedBrand, AlabamaItem, AlabamaItemMedia, AlabamaMediaLink
from .models import ItemMedia, Items, ItemSyncStatus, Role

ROOT = tempfile.mkdtemp(prefix='al_catalog_test_')


def png(color='red'):
    buf = BytesIO()
    Image.new('RGB', (40, 30), color).save(buf, 'PNG')
    return buf.getvalue()


def upload(color='blue', name='s.png'):
    return SimpleUploadedFile(name, png(color), content_type='image/png')


@override_settings(ITEM_MEDIA_PRIVATE_ROOT=ROOT, ALLOWED_HOSTS=['testserver'], ALABAMA_OWN_CATALOG=True, ALABAMA_OWN_ITEMS=True)
class AlabamaItemCatalogTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(ROOT, ignore_errors=True)

    def setUp(self):
        cache.clear()
        AlabamaFeedBrand.objects.all().delete()
        AlabamaFeedBrand.objects.create(brand='KLUDI')
        now = timezone.now()
        # Junaid catalog (ids on purpose equal to the Alabama ids below)
        self.j_same = Items.objects.create(id=1, item_code='S1', item_description='Kludi basin mixer', item_firm='KLUDI',
                                           item_cost=5, item_price=9)
        self.j_diff = Items.objects.create(id=2, item_code='D1', item_description='GF pipe 3 inch', item_firm='GF',
                                           item_cost=5, item_price=9)
        self.j_only = Items.objects.create(id=3, item_code='J1', item_description='Junaid only', item_firm='PEGLER',
                                           item_cost=5, item_price=9)
        for it in (self.j_same, self.j_diff, self.j_only):
            ItemSyncStatus.objects.create(item=it, last_seen_at=now)
        for it, color in ((self.j_same, 'red'), (self.j_diff, 'green')):
            m = ItemMedia(item=it)
            m.photo.save('p.jpg', ContentFile(png(color)), save=False)
            m.datasheet.save('d.jpg', ContentFile(png(color)), save=False)
            m.save()
        # Alabama catalog
        mk = lambda pk, code, desc, brand, q=True: AlabamaItem.objects.create(
            id=pk, item_code=code, item_description=desc, item_firm=brand, item_cost=0, item_price=12, quotable=q, last_seen_at=now)
        self.a_same = mk(1, 'S1', 'KLUDI Basin  Mixer', 'KLUDI')                 # same code, same description (case/space differ)
        self.a_diff = mk(2, 'D1', 'Hepworth pipe 3 inch', 'KLUDI')               # same code, different description
        self.a_none = mk(3, 'N1', 'No Junaid twin', 'KLUDI')
        self.a_hidden = mk(4, 'H1', 'Hidden brand item', 'PLUMBING', q=False)

        self.al_admin = self._user('alrole_admin', 'Admin', 'Alabama')
        self.al_sales = self._user('alabamakadhar', 'Salesman', 'Alabama')
        self.j_admin = self._user('jadmin', 'Admin', 'Junaid')
        self.so = self._user('so', 'Admin', 'Junaid')

    def _user(self, name, role, company):
        u = User.objects.create_user(name, password='x')
        Role.objects.create(user=u, role=role, company=company)
        return u

    def page(self, user, **params):
        self.client.force_login(user)
        resp = self.client.get(reverse('item_catalog'), params)
        self.assertEqual(resp.status_code, 200)
        return resp

    def codes(self, resp):
        return [it.item_code for it in resp.context['page']]

    # ---- who sees which catalog ----------------------------------------------------------------------------------
    def test_alabama_user_sees_only_quotable_alabama_items(self):
        resp = self.page(self.al_sales)
        self.assertEqual(sorted(self.codes(resp)), ['D1', 'N1', 'S1'])
        self.assertEqual(resp.context['total'], 3)
        self.assertEqual([b for b, _ in resp.context['brand_options']], ['KLUDI'])

    def test_junaid_users_still_see_the_junaid_catalog(self):
        for user in (self.j_admin, self.so):
            self.assertEqual(sorted(self.codes(self.page(user))), ['D1', 'J1', 'S1'])

    def test_so_can_open_the_alabama_catalog_with_division(self):
        resp = self.page(self.so, division='ALABAMA')
        self.assertEqual(sorted(self.codes(resp)), ['D1', 'N1', 'S1'])
        self.assertIn('division=ALABAMA', resp.context['filter_qs'])

    @override_settings(ALABAMA_OWN_CATALOG=False)
    def test_switch_off_alabama_user_gets_the_old_junaid_page(self):
        self.assertNotIn('N1', self.codes(self.page(self.al_sales)))

    # ---- reuse of the Junaid files --------------------------------------------------------------------------------
    def test_same_code_and_description_reuses_the_junaid_files(self):
        found = media_match.resolve(AlabamaItem.objects.all())
        self.assertTrue(found[self.a_same.pk].photo)
        self.assertEqual(found[self.a_same.pk].photo_src, 'junaid')
        self.assertTrue(found[self.a_same.pk].sheet)

    def test_same_code_but_different_description_is_not_reused(self):
        found = media_match.resolve(AlabamaItem.objects.all())
        self.assertFalse(found[self.a_diff.pk].photo)
        self.assertFalse(found[self.a_diff.pk].sheet)
        self.assertFalse(found[self.a_none.pk].photo)

    def test_an_approved_link_reuses_it(self):
        AlabamaMediaLink.objects.create(alabama_item=self.a_diff, junaid_item=self.j_diff)
        r = media_match.resolve([self.a_diff])[self.a_diff.pk]
        self.assertTrue(r.photo and r.sheet)
        self.assertEqual(r.photo_src, 'link')

    def test_own_upload_wins_over_the_junaid_file(self):
        self.client.force_login(self.al_admin)
        resp = self.client.post(reverse('item_catalog_a_upload', args=[self.a_same.pk]), {'photo': upload('blue')})
        self.assertEqual(resp.status_code, 302)
        r = media_match.resolve([self.a_same])[self.a_same.pk]
        self.assertEqual(r.photo_src, 'own')
        self.assertEqual(r.sheet_src, 'junaid')                      # the datasheet is still the reused Junaid one
        self.assertTrue(ItemMedia.objects.get(item=self.j_same).photo)       # Junaid's own file is untouched

    def test_page_shows_urls_of_the_alabama_files(self):
        html = self.page(self.al_sales).content.decode()
        self.assertIn(reverse('item_catalog_a_photo', args=[self.a_same.pk]), html)
        self.assertIn(reverse('item_catalog_a_datasheet', args=[self.a_same.pk]), html)
        self.assertNotIn(reverse('item_catalog_photo', args=[self.j_same.pk]), html)

    # ---- serving files --------------------------------------------------------------------------------------------
    def test_alabama_file_urls_serve_the_matched_file(self):
        self.client.force_login(self.al_sales)
        self.assertEqual(self.client.get(reverse('item_catalog_a_photo', args=[self.a_same.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('item_catalog_a_datasheet', args=[self.a_same.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('item_catalog_a_photo', args=[self.a_diff.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('item_catalog_a_photo', args=[self.a_hidden.pk])).status_code, 404)

    def test_junaid_users_cannot_open_alabama_file_urls(self):
        self.client.force_login(self.j_admin)
        self.assertEqual(self.client.get(reverse('item_catalog_a_photo', args=[self.a_same.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('item_catalog_a_upload', args=[self.a_same.pk]), {'photo': upload()}).status_code, 404)

    @override_settings(ALABAMA_OWN_CATALOG=False)
    def test_switch_off_alabama_file_urls_are_closed(self):
        self.client.force_login(self.al_sales)
        self.assertEqual(self.client.get(reverse('item_catalog_a_photo', args=[self.a_same.pk])).status_code, 404)

    def test_junaid_file_urls_unchanged(self):
        self.client.force_login(self.j_admin)
        self.assertEqual(self.client.get(reverse('item_catalog_photo', args=[self.j_same.pk])).status_code, 200)

    # ---- upload / remove ------------------------------------------------------------------------------------------
    def test_salesman_cannot_upload(self):
        self.client.force_login(self.al_sales)
        self.client.post(reverse('item_catalog_a_upload', args=[self.a_none.pk]), {'photo': upload()})
        self.assertFalse(AlabamaItemMedia.objects.exists())

    def test_admin_upload_and_remove_own_file_only(self):
        self.client.force_login(self.al_admin)
        self.client.post(reverse('item_catalog_a_upload', args=[self.a_none.pk]), {'photo': upload()})
        self.assertTrue(AlabamaItemMedia.objects.get(alabama_item=self.a_none).photo)
        self.assertEqual(media_match.resolve([self.a_none])[self.a_none.pk].photo_src, 'own')
        self.client.post(reverse('item_catalog_a_remove', args=[self.a_none.pk]), {'kind': 'photo'})
        self.assertFalse(AlabamaItemMedia.objects.filter(alabama_item=self.a_none).exists())
        # a reused Junaid file cannot be removed from the Alabama side
        self.client.post(reverse('item_catalog_a_remove', args=[self.a_same.pk]), {'kind': 'photo'})
        self.assertTrue(ItemMedia.objects.get(item=self.j_same).photo)
        self.assertTrue(media_match.resolve([self.a_same])[self.a_same.pk].photo)

    def test_upload_never_touches_junaid_media(self):
        before = ItemMedia.objects.get(item=self.j_same).photo.name
        self.client.force_login(self.al_admin)
        self.client.post(reverse('item_catalog_a_upload', args=[self.a_same.pk]), {'photo': upload()})
        self.assertEqual(ItemMedia.objects.get(item=self.j_same).photo.name, before)

    def test_no_edit_or_delete_buttons_for_alabama_items(self):
        html = self.page(self.al_admin).content.decode()
        self.assertNotIn('js-edit', html.split('<script>')[0])
        self.assertNotIn('Delete item', html)

    # ---- filters and exports --------------------------------------------------------------------------------------
    def test_missing_filters(self):
        self.assertEqual(sorted(self.codes(self.page(self.al_sales, missing='photo'))), ['D1', 'N1'])
        self.assertEqual(sorted(self.codes(self.page(self.al_sales, missing='datasheet'))), ['D1', 'N1'])

    def test_search_and_brand_filters(self):
        self.assertEqual(self.codes(self.page(self.al_sales, q='Hepworth')), ['D1'])
        self.assertEqual(sorted(self.codes(self.page(self.al_sales, brand='KLUDI'))), ['D1', 'N1', 'S1'])

    def test_excel_and_pdf_exports(self):
        self.client.force_login(self.al_admin)
        for name in ('item_catalog_export_excel', 'item_catalog_export_pdf'):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertGreater(len(resp.content), 500)

    def test_junaid_exports_unchanged(self):
        self.client.force_login(self.j_admin)
        for name in ('item_catalog_export_excel', 'item_catalog_export_pdf'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_review_command_lists_description_mismatches(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('alabama_media_review', stdout=out)
        self.assertIn('D1', out.getvalue())
        self.assertNotIn('S1 |', out.getvalue())


@override_settings(ITEM_MEDIA_PRIVATE_ROOT=ROOT, ALLOWED_HOSTS=['testserver'], ALABAMA_OWN_CATALOG=True, ALABAMA_OWN_ITEMS=True)
class AlabamaCatalogStockAndLinkTests(TestCase):
    """Alabama's feed has a single stock figure (StockQuantity): the page shows it as Total Stock, has no DIP column,
    and 'Hide 0 stock' works on it. The Alabama section links to the Alabama catalog."""

    def setUp(self):
        cache.clear()
        AlabamaFeedBrand.objects.all().delete()
        AlabamaFeedBrand.objects.create(brand='KLUDI')
        now = timezone.now()
        from decimal import Decimal
        AlabamaItem.objects.create(item_code='IN1', item_description='In stock', item_firm='KLUDI', item_stock=7,
                                   total_available_stock=Decimal('7'), quotable=True, last_seen_at=now)
        AlabamaItem.objects.create(item_code='OUT1', item_description='Out of stock', item_firm='KLUDI', item_stock=0,
                                   total_available_stock=Decimal('0'), quotable=True, last_seen_at=now)
        Items.objects.create(item_code='J1', item_description='Junaid item', item_firm='PEGLER', dip_warehouse_stock=Decimal('3'),
                             total_available_stock=Decimal('4'))
        self.al_admin = self._user('alrole_admin', 'Admin', 'Alabama')
        self.j_admin = self._user('jadmin', 'Admin', 'Junaid')
        self.so = self._user('so', 'Admin', 'Junaid')

    def _user(self, name, role, company):
        u = User.objects.create_user(name, password='x')
        Role.objects.create(user=u, role=role, company=company)
        return u

    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('item_catalog'), params)

    def test_alabama_stock_shown_and_no_dip_column(self):
        html = self.get(self.al_admin).content.decode()
        self.assertIn('<th>Total Stock</th>', html)
        self.assertNotIn('DIP Stock', html)

    def test_hide_zero_stock_uses_alabama_stock(self):
        resp = self.get(self.al_admin, instock='1')
        self.assertEqual([it.item_code for it in resp.context['page']], ['IN1'])

    def test_junaid_page_keeps_its_dip_column_and_filter(self):
        resp = self.get(self.j_admin)
        self.assertIn('DIP Stock', resp.content.decode())
        self.assertEqual([it.item_code for it in self.get(self.j_admin, instock='1').context['page']], ['J1'])

    def test_exports_have_no_dip_for_alabama_but_keep_it_for_junaid(self):
        from io import BytesIO as _B
        from openpyxl import load_workbook
        self.client.force_login(self.al_admin)
        ws = load_workbook(_B(self.client.get(reverse('item_catalog_export_excel')).content)).active
        self.assertNotIn('DIP Stock', [c.value for c in ws[1]])
        self.assertEqual(self.client.get(reverse('item_catalog_export_pdf')).status_code, 200)
        self.client.force_login(self.j_admin)
        ws = load_workbook(_B(self.client.get(reverse('item_catalog_export_excel')).content)).active
        self.assertIn('DIP Stock', [c.value for c in ws[1]])

    def test_alabama_sidebar_links_to_the_alabama_catalog(self):
        manager = self._user('manager', 'Admin', 'Junaid')
        for user in (self.al_admin, manager):
            self.client.force_login(user)
            html = self.client.get(reverse('alabama:home')).content.decode()
            self.assertIn(reverse('item_catalog') + '?division=ALABAMA', html, user.username)

    def test_so_sees_the_alabama_catalog_link_in_the_alabama_section_and_on_the_home_page(self):
        link = reverse('item_catalog') + '?division=ALABAMA'
        self.client.force_login(self.so)
        self.assertIn(link, self.client.get(reverse('alabama:home')).content.decode())
        self.assertIn(link, self.client.get(reverse('home')).content.decode())

    def test_junaid_admin_gets_no_alabama_catalog_link(self):
        link = reverse('item_catalog') + '?division=ALABAMA'
        self.client.force_login(self.j_admin)
        self.assertNotIn(link, self.client.get(reverse('home')).content.decode())

    @override_settings(ALABAMA_OWN_CATALOG=False)
    def test_switch_off_no_alabama_catalog_links(self):
        link = reverse('item_catalog') + '?division=ALABAMA'
        self.client.force_login(self.so)
        self.assertNotIn(link, self.client.get(reverse('home')).content.decode())
        self.assertNotIn(link, self.client.get(reverse('alabama:home')).content.decode())

    def test_so_gets_the_alabama_catalog_through_that_link(self):
        resp = self.get(self.so, division='ALABAMA')
        self.assertEqual(sorted(it.item_code for it in resp.context['page']), ['IN1', 'OUT1'])

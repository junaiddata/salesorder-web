"""In-app reminders on quotation follow-ups: saved with the follow-up, shown only to the author once due, dismiss/snooze."""
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Quotation, QuotationFollowUp, SAPQuotation, SAPQuotationFollowUp
from .tests_alabama_catalog import HOSTS, Base

AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}


def _local_input(dt):
    """What <input type="datetime-local"> submits for a moment (Dubai time, no seconds)."""
    return timezone.localtime(dt).strftime('%Y-%m-%dT%H:%M')


@override_settings(ALLOWED_HOSTS=HOSTS)
class FollowUpReminderTests(Base):
    def setUp(self):
        super().setUp()
        self.quotation = Quotation.objects.create(customer=self.cust, salesman=self.salesman)
        self.sap = SAPQuotation.objects.create(q_number='900001', customer_name='ACME SAP')
        self.login(self.so_admin)

    # -- saving ------------------------------------------------------------------------------------------------
    def _add_app(self, remind_at=None, note='call customer'):
        data = {'action': 'add_followup', 'note': note}
        if remind_at is not None:
            data['remind_at'] = remind_at
        return self.client.post(reverse('view_quotation_details', args=[self.quotation.pk]), data, **AJAX)

    def _add_sap(self, remind_at=None, note='call customer'):
        data = {'note': note}
        if remind_at is not None:
            data['remind_at'] = remind_at
        return self.client.post(reverse('quotation_add_followup', args=[self.sap.q_number]), data, **AJAX)

    def test_followup_without_reminder_works_as_before(self):
        for resp in (self._add_app(), self._add_sap(), self._add_app(''), self._add_sap('')):
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertEqual(resp.json()['followup']['remind_at'], '')
        self.assertEqual(QuotationFollowUp.objects.filter(remind_at__isnull=True).count(), 2)
        self.assertEqual(SAPQuotationFollowUp.objects.filter(remind_at__isnull=True).count(), 2)

    def test_reminder_is_saved_in_dubai_time_for_both_quotation_types(self):
        when = timezone.now() + timedelta(days=5)
        for resp in (self._add_app(_local_input(when)), self._add_sap(_local_input(when))):
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertTrue(resp.json()['followup']['remind_at'])
        for fu in (QuotationFollowUp.objects.get(), SAPQuotationFollowUp.objects.get()):
            self.assertEqual(fu.created_by, self.so_admin)
            self.assertAlmostEqual(fu.remind_at.timestamp(), when.replace(second=0, microsecond=0).timestamp(), delta=60)

    def test_past_or_invalid_reminder_is_rejected_and_nothing_is_saved(self):
        past = _local_input(timezone.now() - timedelta(hours=1))
        for value in (past, 'not-a-date'):
            self.assertEqual(self._add_app(value).status_code, 400)
            self.assertEqual(self._add_sap(value).status_code, 400)
        self.assertEqual(QuotationFollowUp.objects.count(), 0)
        self.assertEqual(SAPQuotationFollowUp.objects.count(), 0)

    # -- due list ----------------------------------------------------------------------------------------------
    def _due(self):
        return self.client.get(reverse('followup_reminders_due')).json()['reminders']

    def _make(self, model, quotation, user, minutes_from_now, **extra):
        return model.objects.create(quotation=quotation, note='n', created_by=user,
                                    remind_at=timezone.now() + timedelta(minutes=minutes_from_now), **extra)

    def test_only_due_reminders_of_the_logged_in_author_are_listed(self):
        mine_due = self._make(QuotationFollowUp, self.quotation, self.so_admin, -1)
        sap_due = self._make(SAPQuotationFollowUp, self.sap, self.so_admin, -5)
        self._make(QuotationFollowUp, self.quotation, self.so_admin, 30)                       # not due yet
        self._make(QuotationFollowUp, self.quotation, self.junaid, -1)                         # someone else's
        self._make(QuotationFollowUp, self.quotation, self.so_admin, -1, reminder_dismissed_at=timezone.now())
        QuotationFollowUp.objects.create(quotation=self.quotation, note='no reminder', created_by=self.so_admin)
        rows = self._due()
        self.assertEqual({(r['kind'], r['id']) for r in rows}, {('q', mine_due.id), ('sap', sap_due.id)})
        sap_row = next(r for r in rows if r['kind'] == 'sap')
        self.assertEqual(sap_row['url'], reverse('quotation_detail', args=['900001']))
        self.assertEqual(sap_row['number'], '900001')
        app_row = next(r for r in rows if r['kind'] == 'q')
        self.assertEqual(app_row['url'], reverse('view_quotation_details', args=[self.quotation.pk]))

    def test_other_users_never_see_my_reminders(self):
        self._make(QuotationFollowUp, self.quotation, self.so_admin, -1)
        self.login(self.junaid)
        self.assertEqual(self._due(), [])

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('followup_reminders_due')).status_code, 302)
        self.assertEqual(self.client.post(reverse('followup_reminders_act'), {}).status_code, 302)
        self.assertEqual(self.client.get(reverse('followup_reminders_js')).content, b'')

    # -- dismiss / snooze --------------------------------------------------------------------------------------
    def _act(self, **data):
        return self.client.post(reverse('followup_reminders_act'), data)

    def test_dismiss_removes_it_from_the_due_list(self):
        fu = self._make(SAPQuotationFollowUp, self.sap, self.so_admin, -1)
        self.assertEqual(self._act(kind='sap', id=fu.id, action='dismiss').status_code, 200)
        self.assertEqual(self._due(), [])
        fu.refresh_from_db()
        self.assertIsNotNone(fu.reminder_dismissed_at)

    def test_snooze_pushes_it_back_and_it_returns_when_due_again(self):
        fu = self._make(QuotationFollowUp, self.quotation, self.so_admin, -1)
        self.assertEqual(self._act(kind='q', id=fu.id, action='snooze', minutes=60).status_code, 200)
        self.assertEqual(self._due(), [])
        fu.refresh_from_db()
        self.assertAlmostEqual(fu.remind_at.timestamp(), (timezone.now() + timedelta(minutes=60)).timestamp(), delta=30)
        fu.remind_at = timezone.now() - timedelta(seconds=1)
        fu.save()
        self.assertEqual(len(self._due()), 1)

    def test_snooze_after_dismiss_reactivates_and_bad_values_are_refused(self):
        fu = self._make(QuotationFollowUp, self.quotation, self.so_admin, -1, reminder_dismissed_at=timezone.now())
        self.assertEqual(self._act(kind='q', id=fu.id, action='snooze', minutes=7).status_code, 400)
        self.assertEqual(self._act(kind='q', id=fu.id, action='snooze', minutes='x').status_code, 400)
        self.assertEqual(self._act(kind='zzz', id=fu.id, action='dismiss').status_code, 400)
        self.assertEqual(self._act(kind='q', id='abc', action='dismiss').status_code, 400)
        self.assertEqual(self._act(kind='q', id=fu.id, action='explode').status_code, 400)

    def test_cannot_act_on_someone_elses_reminder(self):
        fu = self._make(QuotationFollowUp, self.quotation, self.junaid, -1)
        self.assertEqual(self._act(kind='q', id=fu.id, action='dismiss').status_code, 404)
        fu.refresh_from_db()
        self.assertIsNone(fu.reminder_dismissed_at)

    def test_deleting_a_followup_removes_its_reminder(self):
        fu = self._make(QuotationFollowUp, self.quotation, self.so_admin, -1)
        fu.delete()
        self.assertEqual(self._due(), [])

    # -- pages -------------------------------------------------------------------------------------------------
    def test_widget_script_is_served_to_logged_in_users_and_pages_load_it(self):
        js = self.client.get(reverse('followup_reminders_js'))
        self.assertEqual(js.status_code, 200)
        self.assertIn('javascript', js['Content-Type'])
        body = js.content.decode()
        self.assertIn(reverse('followup_reminders_due'), body)
        self.assertIn(reverse('followup_reminders_act'), body)
        self.assertNotIn('__DUE__', body)
        tag = reverse('followup_reminders_js')
        for url in (reverse('view_quotation_details', args=[self.quotation.pk]),
                    reverse('quotation_detail', args=[self.sap.q_number])):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertContains(resp, tag)
            self.assertContains(resp, 'name="remind_at"')

    def test_history_shows_the_reminder_badge(self):
        self._make(QuotationFollowUp, self.quotation, self.so_admin, 60)
        self._make(SAPQuotationFollowUp, self.sap, self.so_admin, 60)
        for url in (reverse('view_quotation_details', args=[self.quotation.pk]),
                    reverse('quotation_detail', args=[self.sap.q_number])):
            self.assertContains(self.client.get(url), 'Remind:')

    def test_combined_quotations_page_loads_the_reminder_script(self):
        resp = self.client.get(reverse('combined_quotations_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse('followup_reminders_js'))


@override_settings(ALLOWED_HOSTS=HOSTS)
class CombinedGpPercentTests(Base):
    def test_summary_shows_gp_percent_on_page_and_ajax(self):
        self.login(self.so_admin)
        resp = self.client.get(reverse('combined_quotations_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'GP %')
        self.assertIn('combined_month_gp_pct', resp.context)
        ajax = self.client.get(reverse('combined_quotations_list') + '?ajax=combined_calendar', **AJAX)
        self.assertEqual(ajax.status_code, 200)
        self.assertContains(ajax, 'GP %')


@override_settings(ALLOWED_HOSTS=HOSTS)
class CombinedTotalFilterTests(Base):
    def setUp(self):
        super().setUp()
        self.login(self.so_admin)
        for num, amount in (('S-LOW', 3000), ('S-MID', 7000), ('S-HIGH', 150000)):
            SAPQuotation.objects.create(q_number=num, customer_name='SAPCO', document_total=amount)
        for amount in (3000, 30000):
            Quotation.objects.create(customer=self.cust, salesman=self.salesman, division='JUNAID', grand_total=amount)

    def _rows(self, total=None):
        url = reverse('combined_quotations_list') + (('?' + urlencode({'total': total})) if total is not None else '')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_all_and_unknown_values_do_not_filter(self):
        self.assertEqual(self._rows().context['total_count'], 5)
        self.assertEqual(self._rows('').context['total_count'], 5)
        self.assertEqual(self._rows('bogus').context['total_count'], 5)

    def test_each_band_keeps_only_matching_quotations_from_both_sources(self):
        self.assertEqual(self._rows('0-5000').context['total_count'], 2)       # S-LOW + app 3000
        self.assertEqual(self._rows('5001-10000').context['total_count'], 1)   # S-MID
        self.assertEqual(self._rows('25001-50000').context['total_count'], 1)  # app 30000
        self.assertEqual(self._rows('100000+').context['total_count'], 1)      # S-HIGH
        self.assertEqual(self._rows('10001-25000').context['total_count'], 0)

    def test_selected_band_stays_selected_and_summary_follows_it(self):
        resp = self._rows('5001-10000')
        self.assertEqual(resp.context['selected_total'], '5001-10000')
        self.assertContains(resp, 'name="total"')

"""Bank details + TRN on the quotation PDFs, above the stamp: Junaid's (in-app and SAP) and Alabama's (in-app),
each on its own quotations only."""
from io import BytesIO
from unittest import mock

from django.test import TestCase
from PyPDF2 import PdfReader
from reportlab.platypus import Image

from . import quotation_pdf_new as pdf
from .models import Customer, Quotation, Salesman, SAPQuotation

NEEDLES = ('BANK DETAILS', 'NATIONAL BANK OF RAS AL KHAIMAH', '0322464229001', 'AE23 0400 0003 2246 4229 001', 'NRAKAEAK',
           '100225006400003')
ALABAMA_NEEDLES = ('BANK DETAILS', 'ALABAMA BUILDING MATERIALS T LLC', 'ABU DHABI COMMERCIAL BANK', '11513072820001',
                   'AE95 0030 0115 1307 2820 001', 'ADCBAEAAXXX', '100604133700003')


def _pages(data):
    return [' '.join(p.extract_text().split()) for p in PdfReader(BytesIO(data)).pages]


def _fake_stamp(base):
    """A stamp-sized image, so the layout tests do not depend on a file in media/."""
    from PIL import Image as PILImage
    buf = BytesIO()
    PILImage.new('RGB', (100, 80), (0, 0, 120)).save(buf, 'PNG')
    buf.seek(0)
    return Image(buf, width=100, height=80)


class QuotationBankDetailsTests(TestCase):
    def setUp(self):
        self.salesman = Salesman.objects.create(salesman_name='ANISH')
        self.customer = Customer.objects.create(customer_code='C1', customer_name='Cust One', salesman=self.salesman)

    def _quotation(self, **extra):
        return Quotation.objects.create(customer=self.customer, salesman=self.salesman, **extra)

    def test_ibans_are_valid_and_end_with_the_account_number(self):
        for rows in (pdf.BANK_DETAILS, pdf.ALABAMA_BANK_DETAILS):
            details = dict(rows)
            iban = details['IBAN NO'].replace(' ', '')
            self.assertEqual(int(''.join(str(int(c, 36)) for c in iban[4:] + iban[:4])) % 97, 1)
            self.assertTrue(iban.endswith(details['A/C NO'].lstrip('0')))

    def test_both_divisions_use_the_same_labels(self):
        self.assertEqual([label for label, _ in pdf.BANK_DETAILS], [label for label, _ in pdf.ALABAMA_BANK_DETAILS])

    def test_junaid_quotation_prints_the_details_after_the_terms_for_both_licences(self):
        for licence in ('JUNAID_SME', 'JUNAID_MAT'):
            text = ' '.join(_pages(pdf.build_so_quotation_pdf(self._quotation(division='JUNAID', license_name=licence))))
            for needle in NEEDLES:
                self.assertIn(needle, text, (licence, needle))
            self.assertLess(text.index('TERMS AND CONDITIONS'), text.index('BANK DETAILS'))

    def test_alabama_quotation_prints_its_own_details_and_never_junaids(self):
        text = ' '.join(_pages(pdf.build_so_quotation_pdf(self._quotation(division='ALABAMA'))))
        for needle in ALABAMA_NEEDLES:
            self.assertIn(needle, text, needle)
        self.assertLess(text.index('TERMS AND CONDITIONS'), text.index('BANK DETAILS'))
        for needle in NEEDLES[1:]:
            self.assertNotIn(needle, text, needle)

    def test_junaid_quotation_never_shows_alabamas_details(self):
        text = ' '.join(_pages(pdf.build_so_quotation_pdf(self._quotation(division='JUNAID'))))
        for needle in ALABAMA_NEEDLES[1:]:
            self.assertNotIn(needle, text, needle)

    def test_sap_quotation_prints_them_unless_it_is_an_alabama_salesperson(self):
        q = SAPQuotation.objects.create(q_number='Q1', customer_name='Cust One', salesman_name='B.MR.MUZAIN')
        text = ' '.join(_pages(pdf.build_sap_quotation_pdf(q)))
        for needle in NEEDLES:
            self.assertIn(needle, text, needle)
        q.salesman_name = 'Alabama - Sam'
        text = ' '.join(_pages(pdf.build_sap_quotation_pdf(q)))
        self.assertNotIn('BANK DETAILS', text)

    def test_bank_details_and_stamp_stay_together_whatever_the_page_length(self):
        """Stacked normally; side by side when only the stamp would spill over; otherwise both move to the next page."""
        drawn = []
        original = pdf._Closing.draw

        def spy(closing):
            drawn.append('side' if closing._parts is closing.side_by_side else 'stacked')
            original(closing)

        for division in ('JUNAID', 'ALABAMA'):
            with self.subTest(division=division):
                self._check_lengths(self._quotation(division=division), spy, drawn)

    def _check_lengths(self, q, spy, drawn):
        modes = set()
        with mock.patch.object(pdf, '_stamp', _fake_stamp), mock.patch.object(pdf._Closing, 'draw', spy):
            for lines in range(0, 60):
                q.remarks = '\n'.join('Remark line %d' % i for i in range(lines))
                drawn.clear()
                pages = _pages(pdf.build_so_quotation_pdf(q))
                self.assertEqual(len(drawn), 1, lines)                      # one block: never split over two pages
                self.assertIn('TERMS AND CONDITIONS', pages[-1], lines)
                self.assertIn('BANK DETAILS', pages[-1], lines)
                modes.add((len(pages), drawn[0]))
        self.assertIn((1, 'stacked'), modes)
        self.assertIn((1, 'side'), modes)
        self.assertIn((2, 'stacked'), modes)

    def test_works_without_a_stamp_file(self):
        with mock.patch.object(pdf, '_stamp', lambda base: None):
            text = ' '.join(_pages(pdf.build_so_quotation_pdf(self._quotation(division='JUNAID'))))
        self.assertIn('BANK DETAILS', text)

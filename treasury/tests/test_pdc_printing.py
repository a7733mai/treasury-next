"""PDC print formats and the amount-in-words helper (PLAN.md §4 printing)."""

import re

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import formatdate, nowdate

from treasury.tests.utils import DEFAULT_PDC_SETTINGS, TreasuryTestMixin, day, make_pdc_cheque
from treasury.utils.amount_in_words import number_in_words, treasury_amount_in_words

DOCTYPE = "PDC Cheque"
RECEIPT = "PDC Cheque Receipt"
CHEQUE_PRINT = "PDC Cheque Print"
ARABIC = re.compile(r"[؀-ۿ]")


class TestPDCPrinting(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		self.set_lang("en")

	def set_lang(self, lang: str):
		previous = frappe.local.lang
		frappe.local.lang = lang
		self.addCleanup(setattr, frappe.local, "lang", previous)

	def outgoing(self):
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30))
		cheque.deliver(handover_date=day(-59))
		cheque.postpone(new_due_date=day(-20), reason="Supplier agreed")
		cheque.reload()
		return cheque

	def incoming(self):
		cheque = make_pdc_cheque(
			"Incoming", amount=2500, issue_date=day(-50), due_date=day(-20), drawer_bank="Drawer Bank X"
		)
		cheque.receive(receipt_date=day(-49))
		cheque.reload()
		return cheque

	@staticmethod
	def render(cheque, print_format: str) -> str:
		return frappe.get_print(DOCTYPE, cheque.name, print_format=print_format)

	@staticmethod
	def print_date(html: str) -> str:
		match = re.search(r'class="pdc-print-date-value">([^<]*)<', html)
		return match.group(1).strip() if match else None

	# ------------------------------------------------------------------ receipt

	def test_receipt_outgoing(self):
		cheque = self.outgoing()
		html = self.render(cheque, RECEIPT)

		self.assertIn("Cheque Delivery Voucher", html)
		self.assertIn(cheque.cheque_no, html)
		self.assertIn(cheque.party_name, html)
		# the print date is today, never the posting / handover / due date
		self.assertEqual(self.print_date(html), formatdate(nowdate()))
		self.assertNotEqual(self.print_date(html), formatdate(cheque.party_posting_date))
		self.assertIn(formatdate(day(-60)), html)  # issue date
		self.assertIn(formatdate(day(-59)), html)  # handover date
		self.assertIn(formatdate(day(-20)), html)  # effective due date
		self.assertIn(formatdate(day(-30)), html)  # original due date (postponed)
		self.assertIn("One Thousand", html)
		self.assertIn('dir="ltr"', html)

	def test_receipt_incoming(self):
		cheque = self.incoming()
		html = self.render(cheque, RECEIPT)

		self.assertIn("Cheque Receipt", html)
		self.assertNotIn("Cheque Delivery Voucher", html)
		self.assertIn("Drawer Bank X", html)
		self.assertIn(cheque.cheque_no, html)
		self.assertIn("Two Thousand, Five Hundred", html)
		self.assertEqual(self.print_date(html), formatdate(nowdate()))
		self.assertIn(formatdate(day(-49)), html)  # receipt date

	def test_receipt_in_arabic(self):
		cheque = self.outgoing()
		self.set_lang("ar")
		html = self.render(cheque, RECEIPT)

		self.assertIn('dir="rtl"', html)
		words = re.search(r'class="pdc-amount-words">([^<]*)<', html).group(1)
		self.assertTrue(ARABIC.search(words), words)
		self.assertIn("ألف", words)

	# ------------------------------------------------------------------ cheque leaf

	def test_cheque_print(self):
		for cheque in (self.outgoing(), self.incoming()):
			html = self.render(cheque, CHEQUE_PRINT)
			self.assertIn(cheque.party_name, html)
			self.assertIn(formatdate(cheque.effective_due_date), html)
			self.assertIn("pdc-cheque-words", html)

	def test_cheque_print_in_arabic(self):
		cheque = self.outgoing()
		self.set_lang("ar")
		html = self.render(cheque, CHEQUE_PRINT)
		self.assertIn("ألف", html)

	def test_mapping_print_format_hint(self):
		cheque = self.outgoing()
		settings = frappe.get_doc("Treasury Company Settings", cheque.company)
		row = next(r for r in settings.pdc_accounts if r.bank_account == cheque.bank_account)
		previous = row.cheque_print_format
		frappe.db.set_value("PDC Account Mapping", row.name, "cheque_print_format", CHEQUE_PRINT)
		self.addCleanup(frappe.db.set_value, "PDC Account Mapping", row.name, "cheque_print_format", previous)

		cheque = frappe.get_doc(DOCTYPE, cheque.name)
		cheque.run_method("onload")
		self.assertEqual(cheque.get_onload().cheque_print_format, CHEQUE_PRINT)


class TestAmountInWords(IntegrationTestCase):
	def test_english(self):
		self.assertEqual(number_in_words(1250, "en"), "One Thousand, Two Hundred And Fifty")
		words = treasury_amount_in_words(1250.75, "USD", "en")
		self.assertIn("One Thousand", words)
		self.assertIn("Seventy Five", words)
		self.assertIn("US Dollar", words)

	def test_arabic(self):
		words = treasury_amount_in_words(1000, "EGP", "ar")
		self.assertIn("ألف", words)
		self.assertTrue(ARABIC.search(words))

	def test_whole_amount_has_no_fraction_part(self):
		words = treasury_amount_in_words(500, "USD", "en")
		self.assertIn("Five Hundred", words)
		self.assertNotIn(" and ", words)

	def test_unsupported_language_falls_back_to_english(self):
		self.assertEqual(number_in_words(3, "xx"), "Three")

	def test_negative_amount(self):
		self.assertEqual(treasury_amount_in_words(-1, "EGP", "en"), "")

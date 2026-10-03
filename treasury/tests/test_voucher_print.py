"""Bilingual cash / bank vouchers: Treasury Receipt Voucher, Treasury Payment Voucher (Payment Entry) and
Treasury Journal Voucher (Journal Entry). Arabic and English amounts in words, mode, cheque, signatures."""

import re

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from treasury.tests.utils import (
	COMPANY,
	TreasuryTestMixin,
	acc,
	ensure_account,
	make_purchase_invoice,
	make_sales_invoice,
)
from treasury.utils.voucher_print import (
	arabic_amount_in_words,
	english_amount_in_words,
	treasury_voucher_context,
)

RECEIPT = "Treasury Receipt Voucher"
PAYMENT = "Treasury Payment Voucher"
JOURNAL = "Treasury Journal Voucher"
ARABIC = re.compile(r"[؀-ۿ]")


class TestVoucherAmountInWords(IntegrationTestCase):
	"""QAR: Riyal with 100 Dirham."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.set_value("Currency", "QAR", {"enabled": 1, "fraction": "Dirham", "fraction_units": 100})

	def test_english_figures(self):
		words = english_amount_in_words(1250.50, "QAR")
		for part in ("One Thousand", "Two Hundred", "Fifty", "Dirham"):
			self.assertIn(part, words)
		self.assertTrue(words.endswith("only."))

	def test_english_ignores_print_language(self):
		previous = frappe.local.lang
		frappe.local.lang = "ar"
		try:
			words = english_amount_in_words(1250.50, "QAR")
			self.assertFalse(ARABIC.search(words), words)
			self.assertEqual(frappe.local.lang, "ar")
		finally:
			frappe.local.lang = previous

	def test_arabic_amounts(self):
		words = arabic_amount_in_words(1250.50, "QAR")
		self.assertTrue(ARABIC.search(words))
		for part in ("ألف", "ريال قطري", "درهم", "فقط", "لا غير"):
			self.assertIn(part, words)

	def test_arabic_fraction_only(self):
		words = arabic_amount_in_words(0.75, "QAR")
		self.assertIn("درهم", words)
		self.assertNotIn("ريال", words)
		self.assertIn("خمسة وسبعون", words)

	def test_one_million(self):
		self.assertIn("مليون", arabic_amount_in_words(1000000, "QAR"))
		self.assertIn("One Million", english_amount_in_words(1000000, "QAR"))
		self.assertNotIn("درهم", arabic_amount_in_words(1000000, "QAR"))


class TestTreasuryVouchers(TreasuryTestMixin, IntegrationTestCase):
	def receive(self, cash: bool = True, **values):
		pe = get_payment_entry("Sales Invoice", make_sales_invoice(1250.5).name, bank_account=self.data.egp.bank_gl)
		return self.finish(pe, "Cash" if cash else "Wire Transfer", cash, values)

	def pay(self, cash: bool = True, **values):
		pe = get_payment_entry("Purchase Invoice", make_purchase_invoice(800).name, bank_account=self.data.egp.bank_gl)
		return self.finish(pe, "Cash" if cash else "Wire Transfer", cash, values)

	def finish(self, pe, mode, cash, values):
		if cash:
			cash_account = ensure_account("Treasury Voucher Cash", "Cash In Hand", "Cash")
			if pe.payment_type == "Receive":
				pe.paid_to = cash_account
			else:
				pe.paid_from = cash_account
		pe.mode_of_payment = mode if frappe.db.exists("Mode of Payment", mode) else None
		pe.reference_no = values.get("reference_no", "CHQ-001")
		pe.reference_date = today()
		pe.remarks = values.get("remarks", "Narration test")
		pe.insert(ignore_permissions=True)
		return pe

	def render(self, doc, print_format):
		return frappe.get_print(doc.doctype, doc.name, print_format=print_format)

	def test_formats_are_shipped_standard_in_treasury(self):
		for name, doctype in ((RECEIPT, "Payment Entry"), (PAYMENT, "Payment Entry"), (JOURNAL, "Journal Entry")):
			values = frappe.db.get_value(
				"Print Format", name, ["doc_type", "standard", "print_format_type", "module", "disabled"], as_dict=True
			)
			self.assertEqual(values.doc_type, doctype)
			self.assertEqual((values.standard, values.print_format_type, values.disabled), ("Yes", "Jinja", 0))
			self.assertEqual(values.module, "Treasury Setup")

	def test_cash_receipt(self):
		pe = self.receive(cash=True)
		context = treasury_voucher_context(pe)
		self.assertEqual((context["kind"], context["mode"]), ("receipt", "cash"))
		html = self.render(pe, RECEIPT)
		for text in ("سند قبض نقدي", "Cash Receipt Voucher", "استلمنا من", "المبلغ كتابة", "Narration test", "CHQ-001"):
			self.assertIn(text, html)
		self.assertIn(pe.name, html)
		self.assertIn("فقط", html)
		self.assertIn("only", html)

	def test_bank_payment_has_signatures_and_prepared_by(self):
		pe = self.pay(cash=False)
		html = self.render(pe, PAYMENT)
		for text in ("سند صرف", "Payment Voucher", "صرفنا إلى", "Prepared by", "Checked by", "Approved by", "Received by"):
			self.assertIn(text, html)
		self.assertIn(frappe.utils.get_fullname(pe.owner), html)

	def test_amounts_in_both_languages(self):
		html = self.render(self.receive(), RECEIPT)
		self.assertRegex(html, r"1,250\.5")
		self.assertTrue(ARABIC.search(html))
		self.assertIn("One Thousand", html)

	def test_journal_voucher(self):
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": today(),
				"voucher_type": "Journal Entry",
				"cheque_no": "JV-CHQ-7",
				"cheque_date": today(),
				"user_remark": "Journal narration",
				"accounts": [
					{
						"account": ensure_account("Treasury Voucher Cash", "Cash In Hand", "Cash"),
						"debit_in_account_currency": 300,
					},
					{"account": acc("Sales"), "credit_in_account_currency": 300},
				],
			}
		).insert(ignore_permissions=True)
		context = treasury_voucher_context(je)
		self.assertEqual((context["kind"], context["mode"]), ("journal", "cash"))
		html = self.render(je, JOURNAL)
		for text in ("سند قيد يومية", "Journal Voucher", "JV-CHQ-7", "Journal narration", "مدين", "Debit", "Prepared by"):
			self.assertIn(text, html)
		self.assertNotIn("Received by", html)

	def test_draft_watermark(self):
		html = self.render(self.receive(), RECEIPT)
		self.assertIn("مسودة", html)


class TestArabicAmountInWordsGrammar(IntegrationTestCase):
	"""Arabic wording of QAR amounts: "فقط <riyals> ريال قطري و<dirhams> درهم لا غير"."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.set_value("Currency", "QAR", {"enabled": 1, "fraction": "Dirham", "fraction_units": 100})

	def test_1250_50_qar_structure(self):
		words = arabic_amount_in_words(1250.50, "QAR")
		self.assertTrue(words.startswith("فقط "), words)
		self.assertTrue(words.endswith(" لا غير"), words)
		# riyals first, then the dirham fraction joined with the conjunction "و"
		self.assertLess(words.index("ألف"), words.index("ريال قطري"))
		self.assertRegex(words, r"ريال قطري و\S+ درهم لا غير$")
		self.assertIn("خمسون درهم", words)  # 50 dirhams

	def test_0_75_qar_is_dirhams_only(self):
		words = arabic_amount_in_words(0.75, "QAR")
		self.assertEqual(words, "فقط خمسة وسبعون درهم لا غير")

	def test_1_000_000_qar_has_no_fraction(self):
		words = arabic_amount_in_words(1000000, "QAR")
		self.assertEqual(words, "فقط مليون ريال قطري لا غير")

	def test_whole_riyals_have_no_dirham_part(self):
		self.assertNotIn("درهم", arabic_amount_in_words(2, "QAR"))
		self.assertIn("ريال قطري", arabic_amount_in_words(2, "QAR"))

	def test_fraction_rounding_carries_into_riyals(self):
		# 0.999 rounds to 100 dirhams = one more riyal, never "مئة درهم"
		words = arabic_amount_in_words(1.999, "QAR")
		self.assertNotIn("درهم", words)
		self.assertIn("ريال قطري", words)

	def test_zero_and_negative(self):
		self.assertEqual(arabic_amount_in_words(-5, "QAR"), "")

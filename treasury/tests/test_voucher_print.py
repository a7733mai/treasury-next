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
from treasury.utils.amount_in_words import number_in_words
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
		for part in ("ألف", "ريالاً قطرياً", "درهماً", "فقط", "لا غير"):
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
		# the fraction must survive: without disable_rounded_total the invoice (and the payment) rounds to 1,250
		invoice = make_sales_invoice(1250.5, submit=False)
		invoice.disable_rounded_total = 1
		invoice.save(ignore_permissions=True)
		invoice.submit()
		pe = get_payment_entry("Sales Invoice", invoice.name, bank_account=self.data.egp.bank_gl)
		return self.finish(pe, "Cash" if cash else "Wire Transfer", cash, values)

	def pay(self, cash: bool = True, **values):
		pe = get_payment_entry(
			"Purchase Invoice", make_purchase_invoice(800).name, bank_account=self.data.egp.bank_gl
		)
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
		pe.custom_remarks = 1  # keep the narration as typed (ERPNext otherwise regenerates it)
		pe.remarks = values.get("remarks", "Narration test")
		pe.insert(ignore_permissions=True)
		return pe

	def render(self, doc, print_format):
		return frappe.get_print(doc.doctype, doc.name, print_format=print_format)

	def test_formats_are_shipped_standard_in_treasury(self):
		for name, doctype in (
			(RECEIPT, "Payment Entry"),
			(PAYMENT, "Payment Entry"),
			(JOURNAL, "Journal Entry"),
		):
			values = frappe.db.get_value(
				"Print Format",
				name,
				["doc_type", "standard", "print_format_type", "module", "disabled"],
				as_dict=True,
			)
			self.assertEqual(values.doc_type, doctype)
			self.assertEqual(
				(values.standard, values.print_format_type, values.disabled), ("Yes", "Jinja", 0)
			)
			self.assertEqual(values.module, "Treasury Setup")

	def test_cash_receipt(self):
		pe = self.receive(cash=True)
		context = treasury_voucher_context(pe)
		self.assertEqual((context["kind"], context["mode"]), ("receipt", "cash"))
		html = self.render(pe, RECEIPT)
		for text in (
			"سند قبض نقدي",
			"Cash Receipt Voucher",
			"استلمنا من",
			"المبلغ كتابة",
			"Narration test",
			"CHQ-001",
		):
			self.assertIn(text, html)
		self.assertIn(pe.name, html)
		self.assertIn("فقط", html)
		self.assertIn("only", html)

	def test_bank_payment_has_signatures_and_prepared_by(self):
		pe = self.pay(cash=False)
		html = self.render(pe, PAYMENT)
		for text in (
			"سند صرف",
			"Payment Voucher",
			"صرفنا إلى",
			"Prepared by",
			"Checked by",
			"Approved by",
			"Received by",
		):
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
		for text in (
			"سند قيد يومية",
			"Journal Voucher",
			"JV-CHQ-7",
			"Journal narration",
			"مدين",
			"Debit",
			"Prepared by",
		):
			self.assertIn(text, html)
		self.assertNotIn("Received by", html)

	def test_draft_watermark(self):
		html = self.render(self.receive(), RECEIPT)
		self.assertIn("مسودة", html)


class TestArabicAmountInWordsGrammar(IntegrationTestCase):
	"""Arabic number-noun agreement of QAR amounts: "فقط <riyals> <riyal form> و<dirhams> <dirham form> لا غير"."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.set_value("Currency", "QAR", {"enabled": 1, "fraction": "Dirham", "fraction_units": 100})

	def words(self, amount):
		return arabic_amount_in_words(amount, "QAR")

	def test_one_follows_the_noun(self):
		self.assertEqual(self.words(1), "فقط ريال قطري واحد لا غير")

	def test_two_is_the_dual_alone(self):
		self.assertEqual(self.words(2), "فقط ريالان قطريان لا غير")

	def test_three_to_ten_take_the_plural(self):
		self.assertEqual(self.words(3), f"فقط {number_in_words(3, 'ar')} ريالات قطرية لا غير")

	def test_eleven_to_ninety_nine_take_the_accusative_singular(self):
		self.assertEqual(self.words(11), f"فقط {number_in_words(11, 'ar')} ريالاً قطرياً لا غير")
		self.assertEqual(self.words(50), "فقط خمسون ريالاً قطرياً لا غير")

	def test_round_hundreds_and_millions_take_the_genitive_singular(self):
		self.assertEqual(self.words(100), f"فقط {number_in_words(100, 'ar')} ريال قطري لا غير")
		self.assertEqual(self.words(1000000), "فقط مليون ريال قطري لا غير")

	def test_1250_50_qar(self):
		words = self.words(1250.50)
		self.assertEqual(words, f"فقط {number_in_words(1250, 'ar')} ريالاً قطرياً وخمسون درهماً لا غير")
		self.assertNotIn(" و ", words)  # the conjunction is attached to the word it introduces

	def test_0_75_qar_is_dirhams_only(self):
		self.assertEqual(self.words(0.75), f"فقط {number_in_words(75, 'ar')} درهماً لا غير")

	def test_fraction_counts_agree_too(self):
		self.assertEqual(self.words(1.01), "فقط ريال قطري واحد ودرهم واحد لا غير")
		self.assertEqual(self.words(1.02), "فقط ريال قطري واحد ودرهمان لا غير")
		self.assertEqual(self.words(1.05), f"فقط ريال قطري واحد و{number_in_words(5, 'ar')} دراهم لا غير")

	def test_conjunction_attached_in_number_words(self):
		self.assertNotIn(" و ", number_in_words(75, "ar"))

	def test_amount_is_rounded_to_the_currency_decimals_first(self):
		# 1.999 is 2.00: the dual, never "one riyal and one hundred dirhams"
		self.assertEqual(self.words(1.999), "فقط ريالان قطريان لا غير")

	def test_whole_riyals_have_no_dirham_part(self):
		self.assertNotIn("درهم", self.words(2))

	def test_zero_and_negative(self):
		self.assertEqual(self.words(-5), "")

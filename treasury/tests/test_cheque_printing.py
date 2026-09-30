"""Phase 7f: cheque printing on bank Cheque Layouts, print registration and reprints (spec 7f §7.4)."""

import re
from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, fmt_money, getdate
from frappe.www.printview import get_html_and_style

from treasury.pdc.cheque_printing import (
	DEFAULT_FONT_FAMILY,
	FORMAT_BATCH,
	FORMAT_CHEQUE,
	FORMAT_TEST,
	LEGACY_FORMAT,
	ChequeAlreadyPrintedError,
	build_leaf,
	currency_decimals,
	format_cheque_date,
	get_cheque_text,
	preview_layout,
	printed_fingerprint,
	render_leaf,
	split_words,
)
from treasury.utils.amount_in_words import treasury_amount_in_words
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	TreasuryTestMixin,
	add_user_permission,
	bank_account_name,
	day,
	ensure_company,
	ensure_currency,
	ensure_other_company_bank,
	ensure_user,
	get_voucher_count,
	make_cheque_layout,
	make_pdc_cheque,
	unique_cheque_no,
)

CHEQUE = "PDC Cheque"
BATCH = "Cheque Batch"
LAYOUT = "Cheque Layout"
ARABIC = re.compile(r"[؀-ۿ]")
ARABIC_INDIC = re.compile(r"[٠-٩]")
WESTERN = re.compile(r"[0-9]")
LAYOUT_VALUES = {
	"nudge_x_mm": 1.5,
	"date_top_mm": 10,
	"date_left_mm": 130,
	"words1_max_chars": 30,
	"figures_guard": "#",
	"default_language": "Arabic",
}
TREASURY_USER = "treasury-print-user@example.com"
TREASURY_MANAGER = "treasury-print-manager@example.com"
ACCOUNTS_USER = "treasury-print-accounts@example.com"
NO_ACCESS_USER = "treasury-print-noaccess@example.com"
# restricted by a User Permission to one cheque (test_batch_all_or_nothing only)
BATCH_MANAGER = "treasury-print-batch-manager@example.com"
# restricted by User Permissions (for Cheque Layout) to another company / another bank account
OTHER_COMPANY_MANAGER = "treasury-print-othercompany@example.com"
OTHER_BANK_MANAGER = "treasury-print-otherbank@example.com"
FORGED_JOB = "0123456789abcdef0123"
BOXES = (
	"tcl-date",
	"tcl-payee",
	"tcl-figures",
	"tcl-words-1",
	"tcl-words-2",
	"tcl-crossing",
	"tcl-signature",
)


class TestChequePrinting(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(TREASURY_MANAGER, ["Treasury Manager"])
		ensure_user(ACCOUNTS_USER, ["Accounts User"])
		ensure_user(NO_ACCESS_USER, [])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS, cheque_reprint_requires_manager=1)
		self.set_lang("en")
		self.layout = make_cheque_layout(**LAYOUT_VALUES)
		self.cheque = make_pdc_cheque(amount=1250.50, issue_date=day(-20), due_date=day(-5))

	def set_lang(self, lang: str):
		previous = frappe.local.lang
		frappe.local.lang = lang
		self.addCleanup(setattr, frappe.local, "lang", previous)

	def leaf(self, lang: str, layout=None, amount=1250.50, currency="EGP"):
		return build_leaf(
			layout or self.layout,
			payee=self.cheque.party_name,
			amount=amount,
			currency=currency,
			date=self.cheque.effective_due_date,
			company=COMPANY,
			lang=lang,
		)

	@contextmanager
	def url_job(self, job: str | None):
		"""The printview URL's `print_job` argument (the registration it prints)."""
		form_dict = frappe.local.form_dict
		had_job, previous = "print_job" in form_dict, form_dict.get("print_job")
		form_dict["print_job"] = job
		try:
			yield
		finally:
			if had_job:
				form_dict["print_job"] = previous
			else:
				form_dict.pop("print_job", None)

	def render(self, doctype: str, name: str, print_format: str, job: str | None = None) -> str:
		"""The print as printview renders it for a URL with `print_job=job`."""
		with self.url_job(job):
			return frappe.get_print(doctype, name, print_format=print_format)

	def print_leaf(self, cheque, language="en", **kwargs) -> str:
		"""Register a print of `cheque` and render it as the form opens it (with the returned job)."""
		result = cheque.print_cheque(language=language, **kwargs)
		return self.render(CHEQUE, cheque.name, FORMAT_CHEQUE, result["print_job"])

	@staticmethod
	def words_line(html: str) -> str:
		match = re.search(r'tcl-words-1[^>]*>\s*([^<]*)<', html)
		return match.group(1).strip() if match else ""

	def disable_layouts(self):
		"""Every layout of the EGP bank disabled (the layouts are rolled back with the class; each test creates
		its own default in setUp)."""
		for name in frappe.get_all(LAYOUT, filters={"bank_account": bank_account_name("EGP")}, pluck="name"):
			frappe.db.set_value(LAYOUT, name, "disabled", 1)

	def state(self, cheque) -> tuple:
		cheque.reload()
		return cheque.operational_status, cheque.accounting_status, get_voucher_count(CHEQUE, cheque.name)

	# ------------------------------------------------------------------ building the leaf

	def test_build_leaf_positions(self):
		leaf = self.leaf("en")
		self.assertEqual((leaf.boxes.date.top, leaf.boxes.date.left), ("10.00mm", "131.50mm"))
		self.assertEqual((leaf.page_width, leaf.page_height), ("175.00mm", "80.00mm"))

		layout = frappe.copy_doc(self.layout)
		layout.update({"paper": "A4 Carrier", "offset_left_mm": 20, "offset_top_mm": 15})
		leaf = self.leaf("en", layout)
		self.assertEqual((leaf.boxes.date.top, leaf.boxes.date.left), ("25.00mm", "151.50mm"))
		self.assertEqual((leaf.page_width, leaf.page_height), ("210.00mm", "297.00mm"))
		self.assertEqual((leaf.leaf_top, leaf.leaf_left), ("15.00mm", "20.00mm"))

	def test_build_leaf_english(self):
		leaf = self.leaf("en")
		self.assertEqual(leaf.dir, "ltr")
		self.assertEqual(leaf.figures, f"#{fmt_money(1250.50, precision=2)}#")
		self.assertIn("One Thousand, Two Hundred And Fifty", leaf.words)
		self.assertIn("Egyptian Pound", leaf.words)
		self.assertIn("Fifty", leaf.words.split("Egyptian Pound")[1])
		self.assertTrue(leaf.words.endswith("only"), leaf.words)
		self.assertLessEqual(len(leaf.words1), 30)
		self.assertTrue(leaf.words2)
		self.assertEqual(f"{leaf.words1} {leaf.words2}", leaf.words)
		self.assertEqual(leaf.crossing, "A/C Payee Only")
		due = getdate(self.cheque.effective_due_date)
		self.assertEqual(leaf.date, f"{due.day:02d}/{due.month:02d}/{due.year}")
		self.assertEqual(leaf.payee, self.cheque.party_name)
		self.assertEqual(leaf.signature, COMPANY)

	def test_build_leaf_arabic(self):
		leaf = self.leaf("ar")
		self.assertEqual(leaf.dir, "rtl")
		self.assertTrue(leaf.words.startswith("فقط"), leaf.words)
		self.assertTrue(leaf.words.endswith("لا غير"), leaf.words)
		self.assertIn("ألف", leaf.words)
		self.assertIn("جنيه مصري", leaf.words)
		self.assertEqual(leaf.crossing, "لحساب المستفيد الأول")

	def test_arabic_indic_digits(self):
		layout = frappe.copy_doc(self.layout)
		layout.digits = "Arabic-Indic"
		arabic = self.leaf("ar", layout)
		for text in (arabic.figures, arabic.date):
			self.assertTrue(ARABIC_INDIC.search(text), text)
			self.assertFalse(WESTERN.search(text), text)
		english = self.leaf("en", layout)
		self.assertEqual((english.figures, english.date), (self.leaf("en").figures, self.leaf("en").date))

	def test_wording_options(self):
		layout = frappe.copy_doc(self.layout)
		layout.add_only_suffix = 0
		self.assertNotIn("only", self.leaf("en", layout).words)
		self.assertNotIn("فقط", self.leaf("ar", layout).words)

		layout = frappe.copy_doc(self.layout)
		layout.show_currency_name = 0
		self.assertNotIn("Egyptian Pound", self.leaf("en", layout).words)
		self.assertNotIn("جنيه مصري", self.leaf("ar", layout).words)

		layout = frappe.copy_doc(self.layout)
		layout.fraction_style = "Figures"
		self.assertIn("and 50/100", self.leaf("en", layout).words)
		self.assertIn("و50/100", self.leaf("ar", layout).words)

		layout = frappe.copy_doc(self.layout)
		layout.update({"fraction_name_ar": "فلس", "currency_name_en": "Egyptian Pounds"})
		self.assertIn("فلس", self.leaf("ar", layout).words)
		self.assertIn("Egyptian Pounds", self.leaf("en", layout).words)

	def test_qar_and_kwd(self):
		ensure_currency("QAR", "Dirham", 100)
		ensure_currency("KWD", "Fils", 1000)
		fraction = frappe.db.get_value("Currency", "QAR", "fraction")

		arabic = self.leaf("ar", amount=1000.50, currency="QAR").words
		self.assertIn("ريال قطري", arabic)
		self.assertIn("خمسون", arabic)
		english = self.leaf("en", amount=1000.50, currency="QAR").words
		self.assertIn("Qatari Riyal", english)
		self.assertTrue(english.endswith(f"Fifty {fraction} only"), english)
		if fraction == "Dirham":
			self.assertIn("درهم", arabic)
		layout = frappe.copy_doc(self.layout)
		layout.update({"fraction_name_ar": "درهم", "fraction_name_en": "Dirhams"})
		self.assertIn("درهم", self.leaf("ar", layout, amount=1000.50, currency="QAR").words)

		units = frappe.db.get_value("Currency", "KWD", "fraction_units")
		if units == 1000:
			self.assertEqual(currency_decimals("KWD"), 3)
			# every decimal of the currency prints, a trailing zero included (fmt_money alone gives "1.25")
			leaf = self.leaf("en", amount=1.250, currency="KWD")
			self.assertEqual(leaf.figures, "#1.250#")
			self.assertIn("Two Hundred And Fifty", leaf.words)
			self.assertEqual(self.leaf("en", amount=1250, currency="KWD").figures, "#1,250.000#")
		self.assertEqual(currency_decimals("EGP"), 2)

	def test_date_formats(self):
		date = getdate("2026-12-31")
		expected = {
			"dd/mm/yyyy": "31/12/2026",
			"dd-mm-yyyy": "31-12-2026",
			"dd.mm.yyyy": "31.12.2026",
			"ddmmyyyy": "31122026",
			"yyyy/mm/dd": "2026/12/31",
			"mm/dd/yyyy": "12/31/2026",
		}
		for date_format, text in expected.items():
			self.assertEqual(format_cheque_date(date, date_format), text)

		layout = frappe.copy_doc(self.layout)
		layout.update({"date_format": "ddmmyyyy", "date_char_spacing_mm": 5})
		leaf = self.leaf("en", layout)
		self.assertEqual(len(leaf.date_chars), 8)
		html = str(render_leaf(layout, leaf))
		self.assertEqual(html.count('<span style="display:inline-block;width:5.00mm'), 8)

	def test_split_words(self):
		self.assertEqual(split_words("One Thousand", 60), ("One Thousand", ""))
		self.assertEqual(split_words("abcde fghij", 11), ("abcde fghij", ""))
		self.assertEqual(split_words("abcdefghij", 4), ("abcd", "efghij"))
		self.assertEqual(split_words("one two three four", 9), ("one two", "three four"))
		self.assertEqual(split_words("one two three", 7), ("one two", "three"))

	# ------------------------------------------------------------------ registration

	def test_print_requires_registration(self):
		html = self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE)
		self.assertIn("treasury-cheque-notice", html)
		self.assertNotIn("tcl-figures", html)

	def test_print_cheque_counter(self):
		before = self.state(self.cheque)
		result = self.cheque.print_cheque(language="ar")
		job = result.pop("print_job")
		self.assertEqual(
			result,
			{"name": self.cheque.name, "print_count": 1, "print_format": FORMAT_CHEQUE, "language": "ar"},
		)
		self.assertEqual(self.state(self.cheque), before)
		self.assertEqual(self.cheque.last_print_job, job)
		self.assertEqual((self.cheque.print_count, self.cheque.last_printed_by), (1, "Administrator"))
		self.assertTrue(self.cheque.last_printed_on)
		log = self.cheque.status_log[-1]
		self.assertEqual((log.event, log.remarks, log.user), ("Printed", "Arabic", "Administrator"))
		self.assertEqual(
			(log.from_operational, log.to_operational), ("Created", "Created")
		)  # no status change

		self.set_lang("ar")
		html = self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job)
		self.assertIn("tcl-figures", html)
		self.assertIn('dir="rtl"', html)
		self.assertTrue(ARABIC.search(self.words_line(html)), self.words_line(html))
		self.set_lang("en")
		html = self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job)
		self.assertIn("One Thousand", self.words_line(html))
		self.assertEqual(frappe.db.get_value(CHEQUE, self.cheque.name, "print_count"), 1)  # a view never counts

	def test_reprint_flow(self):
		self.cheque.print_cheque(language="ar")
		self.assertRaises(ChequeAlreadyPrintedError, self.cheque.print_cheque, language="ar")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.cheque.print_cheque(language="ar", reprint=1)
		self.assertIn("A reason is required to reprint", str(ctx.exception))

		with self.set_user(TREASURY_USER):
			self.assertRaises(
				frappe.PermissionError, self.cheque.print_cheque, language="en", reprint=1, reason="Jammed"
			)
		self.use_company_settings(cheque_reprint_requires_manager=0)
		with self.set_user(TREASURY_USER):
			result = self.cheque.print_cheque(language="en", reprint=1, reason="Jammed")
		self.assertEqual((result["print_count"], result["language"]), (2, "en"))
		self.cheque.reload()
		self.assertEqual((self.cheque.print_count, self.cheque.last_printed_by), (2, TREASURY_USER))
		log = self.cheque.status_log[-1]
		self.assertEqual(log.event, "Reprinted")
		self.assertIn("Jammed", log.remarks)
		self.assertEqual(get_voucher_count(CHEQUE, self.cheque.name), 0)

	def test_manager_reprints(self):
		self.cheque.print_cheque(language="en")
		with self.set_user(TREASURY_MANAGER):
			self.cheque.print_cheque(language="English", reprint=1, reason="Spoilt in the printer")
		self.assertEqual(frappe.db.get_value(CHEQUE, self.cheque.name, "print_count"), 2)
		self.assertRaises(
			frappe.ValidationError, self.cheque.print_cheque, language="French", reprint=1, reason="x"
		)

	def test_print_not_printable(self):
		self.use_company_settings(outgoing_party_posting="Manual", outgoing_bank_posting="Manual")
		cleared = make_pdc_cheque(issue_date=day(-20), due_date=day(-5))
		cleared.deliver(handover_date=day(-19))
		cleared.clear(clearance_date=day(-4))
		with self.assertRaises(frappe.ValidationError) as ctx:
			cleared.print_cheque(language="en")
		self.assertIn("only Created or Delivered cheques can be printed", str(ctx.exception))

		cancelled = make_pdc_cheque()
		cancelled.cancel_cheque(reason="Wrong payee")
		self.assertRaises(frappe.ValidationError, cancelled.print_cheque, language="en")

		incoming = make_pdc_cheque("Incoming")
		with self.assertRaises(frappe.ValidationError) as ctx:
			incoming.print_cheque(language="en")
		self.assertIn("Only outgoing cheques can be printed", str(ctx.exception))
		incoming.run_method("onload")
		self.assertEqual(incoming.get_onload().cheque_print_format, LEGACY_FORMAT)
		self.assertIsNone(incoming.get_onload().cheque_layout)

		# a delivered cheque prints; the leaf of a cheque that became unprintable is refused by the format
		delivered = make_pdc_cheque(issue_date=day(-20), due_date=day(-5))
		delivered.deliver(handover_date=day(-19))
		job = delivered.print_cheque(language="en")["print_job"]
		self.assertIn("tcl-figures", self.render(CHEQUE, delivered.name, FORMAT_CHEQUE, job))
		delivered.clear(clearance_date=day(-4))
		html = self.render(CHEQUE, delivered.name, FORMAT_CHEQUE, job)
		self.assertNotIn("tcl-figures", html)
		self.assertIn("treasury-cheque-notice", html)

	def test_print_permissions(self):
		with self.set_user(ACCOUNTS_USER):
			self.assertRaises(frappe.PermissionError, self.cheque.print_cheque, language="en")
		with self.set_user(NO_ACCESS_USER):
			self.assertRaises(frappe.PermissionError, self.cheque.print_cheque, language="en")
		with self.set_user(TREASURY_USER):
			self.assertEqual(self.cheque.print_cheque(language="en")["print_count"], 1)

	def test_no_layout_fallback(self):
		cheque = make_pdc_cheque(currency="USD", amount=100, issue_date=day(-20), due_date=day(-5))
		with self.assertRaises(frappe.ValidationError) as ctx:
			cheque.print_cheque(language="en")
		self.assertIn("has no Cheque Layout", str(ctx.exception))
		cheque.run_method("onload")
		self.assertEqual(cheque.get_onload().cheque_print_format, LEGACY_FORMAT)
		self.assertIsNone(cheque.get_onload().cheque_layout)

		self.cheque.run_method("onload")
		self.assertEqual(self.cheque.get_onload().cheque_print_format, FORMAT_CHEQUE)
		self.assertEqual(
			self.cheque.get_onload().cheque_layout,
			{
				"name": self.layout.name,
				"default_language": "Arabic",
				"reprint_requires_manager": True,  # setUp
			},
		)

		# a disabled layout is no layout: back to the legacy format
		self.disable_layouts()
		self.cheque.run_method("onload")
		self.assertEqual(self.cheque.get_onload().cheque_print_format, LEGACY_FORMAT)

	def test_replacement_resets_counter(self):
		self.cheque.print_cheque(language="ar")
		result = self.cheque.replace(
			{"cheque_no": unique_cheque_no(), "replacement_date": day(-10), "reason": "Printed wrongly"}
		)
		new = frappe.get_doc(CHEQUE, result["replaced_by"])
		self.assertEqual((new.print_count, new.last_printed_by, new.last_printed_on), (0, None, None))
		self.assertEqual([r.event for r in new.status_log], ["Created"])

	def test_protected_counter(self):
		"""The counter changes only through Print Cheque (a form / REST save is rejected)."""
		doc = frappe.get_doc(CHEQUE, self.cheque.name)
		doc.print_count = 5
		self.assertRaises(frappe.ValidationError, doc.save)
		doc = frappe.get_doc(CHEQUE, self.cheque.name)
		doc.payee_on_cheque = "ACME Trading Co. W.L.L."
		doc.save()
		self.assertIn("ACME Trading Co. W.L.L.", self.print_leaf(self.cheque))
		for fieldname in ("last_print_job", "printed_fingerprint"):
			doc = frappe.get_doc(CHEQUE, self.cheque.name)
			doc.set(fieldname, FORGED_JOB)
			self.assertRaises(frappe.ValidationError, doc.save)

	# ------------------------------------------------------------------ fix round 2: registered prints only

	def test_leaf_renders_only_for_its_registration(self):
		"""The leaf renders for the print job of the URL, for the user who registered it; not without a job,
		with another job, or for another Treasury user."""
		job = self.cheque.print_cheque(language="en")["print_job"]
		self.assertIn("tcl-figures", self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job))
		for other_job in (None, FORGED_JOB, "<script>"):
			html = self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, other_job)
			self.assertNotIn("tcl-figures", html)
			self.assertIn("treasury-cheque-notice", html)
		with self.set_user(TREASURY_USER):
			self.assertNotIn("tcl-figures", self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job))

		# a new registration replaces the job: the previous URL no longer prints
		new_job = self.cheque.print_cheque(language="en", reprint=1, reason="Jammed")["print_job"]
		self.assertNotEqual(new_job, job)
		self.assertNotIn("tcl-figures", self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job))
		self.assertIn("tcl-figures", self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, new_job))

	def test_render_requires_print_role(self):
		"""Review Major 1: a role that may not register a print (Accounts User, with print permission) gets no
		leaf from the print format either."""
		job = self.cheque.print_cheque(language="en")["print_job"]
		with self.set_user(ACCOUNTS_USER):
			html = self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job)
		self.assertNotIn("tcl-figures", html)
		self.assertIn("treasury-cheque-notice", html)
		self.assertIn("This action requires one of the roles", html)

	def test_forged_document_not_rendered(self):
		"""Security Major 4: printview renders a client-sent document (`doc=` JSON); the leaf comes from the
		saved cheque only."""
		job = self.cheque.print_cheque(language="en")["print_job"]
		self.cheque.reload()

		def render_json(values) -> str:
			with self.url_job(job):
				return get_html_and_style(doc=frappe.as_json(values), print_format=FORMAT_CHEQUE)["html"]

		# the registered cheque with other content: refused
		forged = {**self.cheque.as_dict(), "amount": 999999, "payee_on_cheque": "Mallory Trading"}
		html = render_json(forged)
		self.assertNotIn("tcl-figures", html)
		self.assertNotIn("Mallory", html)
		self.assertIn("treasury-cheque-notice", html)
		# the same content as saved prints the saved cheque
		self.assertIn("tcl-figures", render_json(self.cheque.as_dict()))

		# an unregistered cheque with a forged counter, job and fingerprint: refused (the database says 0)
		other = make_pdc_cheque(amount=777, issue_date=day(-20), due_date=day(-5))
		forged = {
			**other.as_dict(),
			"print_count": 3,
			"last_print_job": job,
			"last_printed_by": "Administrator",
			"printed_fingerprint": printed_fingerprint(other),
		}
		html = render_json(forged)
		self.assertNotIn("tcl-figures", html)
		self.assertIn("treasury-cheque-notice", html)

	def test_printed_content_locked(self):
		"""Security Major 2: once printed, what the leaf shows cannot change by a save; a lifecycle change (a
		postponed date) stops the leaf until a reprint is registered, which counts."""
		job = self.cheque.print_cheque(language="en")["print_job"]
		for fieldname, value in (
			("payee_on_cheque", "Someone Else"),
			("amount", 999),
			("original_due_date", day(30)),
			("cheque_no", unique_cheque_no()),
		):
			doc = frappe.get_doc(CHEQUE, self.cheque.name)
			doc.set(fieldname, value)
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.save()
			self.assertIn("after the cheque was printed", str(ctx.exception))
		doc = frappe.get_doc(CHEQUE, self.cheque.name)
		doc.remarks = "Not on the leaf"
		doc.save()  # other fields stay editable
		self.assertIn("tcl-figures", self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job))

		self.cheque.postpone(new_due_date=day(30), reason="The supplier asked")
		html = self.render(CHEQUE, self.cheque.name, FORMAT_CHEQUE, job)
		self.assertNotIn("tcl-figures", html)
		self.assertIn("changed after it was printed", html)

		html = self.print_leaf(self.cheque, reprint=1, reason="New date")  # a Treasury Manager (setting on)
		due = getdate(day(30))
		self.assertIn(f"{due.day:02d}/{due.month:02d}/{due.year}", html)
		self.assertEqual(frappe.db.get_value(CHEQUE, self.cheque.name, "print_count"), 2)

	def test_reprint_reason_sanitised(self):
		"""Security Major 3: the status log row inserted in place is sanitised like a save."""
		self.cheque.print_cheque(language="en")
		self.cheque.print_cheque(language="en", reprint=1, reason='<img src=x onerror="alert(1)">Jammed')
		remarks = frappe.db.get_value(
			"PDC Status Log", {"parent": self.cheque.name, "event": "Reprinted"}, "remarks"
		)
		self.assertNotIn("onerror", remarks)
		self.assertIn("Jammed", remarks)

	def test_font_family_validated(self):
		"""Security Minor 7: the font family is written into a style attribute: font names only."""
		for bad in ("Arial;background:url(//x)", "Arial</style>", 'Arial"', "a" * 101):
			layout = frappe.get_doc(LAYOUT, self.layout.name)
			layout.font_family = bad
			self.assertRaises(frappe.ValidationError, layout.save)
		layout = frappe.get_doc(LAYOUT, self.layout.name)
		layout.font_family = "\"Noto Naskh Arabic\", 'Traditional Arabic', Tahoma, sans-serif"
		layout.save()
		# a value saved before the check never reaches the page
		layout = frappe.copy_doc(self.layout)
		layout.font_family = "Arial;background:url(//x)"
		self.assertEqual(self.leaf("en", layout).font_family, DEFAULT_FONT_FAMILY)

	def test_layout_company_and_bank_access(self):
		"""Security Major 5 / Minor 8, review Minor 9: a user restricted to another company (or bank account)
		can neither create nor move a layout of this bank account, nor preview its company and currency."""
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(OTHER_COMPANY_MANAGER, ["Treasury Manager"])
		add_user_permission(OTHER_COMPANY_MANAGER, "Company", OTHER_COMPANY, applicable_for=LAYOUT)
		values = {
			"doctype": LAYOUT,
			"layout_name": "Foreign",
			"bank_account": bank_account_name("EGP"),
			"company": OTHER_COMPANY,  # sent by the client; the bank account says otherwise
			"is_default": 1,
		}
		with self.set_user(OTHER_COMPANY_MANAGER):
			self.assertRaises(frappe.PermissionError, frappe.get_doc(values).insert)
			self.assertRaises(frappe.PermissionError, preview_layout, values, "en")
		self.assertEqual(frappe.db.get_value(LAYOUT, self.layout.name, "is_default"), 1)

		other_bank = ensure_other_company_bank().bank_account
		ensure_user(OTHER_BANK_MANAGER, ["Treasury Manager"])
		add_user_permission(OTHER_BANK_MANAGER, "Bank Account", other_bank, applicable_for=LAYOUT)
		with self.set_user(OTHER_BANK_MANAGER):
			# moving this bank's layout to the permitted bank account still needs write on the saved layout
			layout = frappe.get_doc(LAYOUT, self.layout.name)
			layout.bank_account = other_bank
			self.assertRaises(frappe.PermissionError, layout.save)
			self.assertRaises(frappe.PermissionError, preview_layout, values, "en")
			preview = preview_layout({**values, "bank_account": other_bank}, "en")
			self.assertIn("tcl-grid", preview)
		self.assertEqual(frappe.db.get_value(LAYOUT, self.layout.name, "bank_account"), bank_account_name("EGP"))

	def test_currency_fractions(self):
		"""Review Minor 7: decimals only from a power of 10; a currency without fraction units still words its
		fraction."""
		ensure_currency("TQF", "Fifth", 5)
		ensure_currency("TQT", "Tenth", 10)
		self.assertEqual((currency_decimals("TQF"), currency_decimals("TQT")), (2, 1))
		if not frappe.db.exists("Currency", "TQZ"):
			frappe.get_doc(
				{"doctype": "Currency", "currency_name": "TQZ", "fraction_units": 0, "enabled": 1}
			).insert(ignore_permissions=True)
		self.assertEqual(currency_decimals("TQZ"), 2)
		self.assertIn("Fifty", treasury_amount_in_words(10.50, "TQZ", "en"))

	# ------------------------------------------------------------------ batches

	def make_batch(self, amounts, **values):
		doc = frappe.get_doc(
			{
				"doctype": BATCH,
				"direction": "Outgoing",
				"company": COMPANY,
				"party_type": "Supplier",
				"party": SUPPLIER,
				"bank_account": bank_account_name("EGP"),
				"issue_date": day(-60),
				"purpose": "Rent",
				**values,
			}
		)
		for i, amount in enumerate(amounts):
			doc.append(
				"cheques",
				{"due_date": add_days(day(-30), 30 * i), "amount": amount, "cheque_no": unique_cheque_no()},
			)
		doc.insert()
		doc.submit()
		doc.reload()
		return doc

	def test_batch_print(self):
		batch = self.make_batch([1000, 2000, 3000])
		first, second, third = (row.pdc_cheque for row in batch.cheques)
		frappe.get_doc(CHEQUE, first).cancel_cheque(reason="Not needed")

		result = batch.print_cheques(language="en")
		self.assertEqual(
			(result["print_format"], result["printed"], result["skipped"], result["language"]),
			(FORMAT_BATCH, [second, third], [], "en"),
		)
		html = self.render(BATCH, batch.name, FORMAT_BATCH, result["print_job"])
		self.assertEqual(html.count('class="treasury-cheque-page"'), 2)
		figures = [fmt_money(amount, precision=2) for amount in (2000, 3000)]
		self.assertLess(html.index(figures[0]), html.index(figures[1]))  # batch row order

		self.assertRaises(ChequeAlreadyPrintedError, batch.print_cheques, language="en")
		result = batch.print_cheques(language="en", only_unprinted=1)
		self.assertEqual((result["printed"], sorted(result["skipped"])), ([], sorted([second, third])))
		self.assertIsNone(result["print_job"])

		replaced = frappe.get_doc(CHEQUE, second).replace(
			{"cheque_no": unique_cheque_no(), "replacement_date": day(-5), "reason": "Wrong date"}
		)
		result = batch.print_cheques(language="en", only_unprinted=1)
		self.assertEqual((result["printed"], result["skipped"]), ([replaced["replaced_by"]], [third]))
		# security Major 1: "Print Unprinted Only" prints the leaf it registered, never the skipped cheque
		html = self.render(BATCH, batch.name, FORMAT_BATCH, result["print_job"])
		self.assertEqual(html.count('class="treasury-cheque-page"'), 1)
		self.assertEqual(html.count('class="tcl-box tcl-figures'), 1)  # the leaf, not the stylesheet rule
		self.assertIn(fmt_money(2000, precision=2), html)  # the replacement of the 2000 cheque
		self.assertNotIn(fmt_money(3000, precision=2), html)  # the skipped, already printed cheque
		# no job, a made-up job: nothing prints
		for job in (None, FORGED_JOB):
			html = self.render(BATCH, batch.name, FORMAT_BATCH, job)
			self.assertNotIn("tcl-figures", html)
			self.assertIn("treasury-cheque-notice", html)

		result = batch.print_cheques(language="ar", reprint=1, reason="Printer jam")
		self.assertEqual(sorted(result["printed"]), sorted([third, replaced["replaced_by"]]))
		self.assertEqual(frappe.db.get_value(CHEQUE, third, "print_count"), 2)
		for name in (first, second, third, replaced["replaced_by"]):
			self.assertEqual(get_voucher_count(CHEQUE, name), 0)
		self.assertEqual(get_voucher_count(BATCH, batch.name), 0)

	def test_batch_all_or_nothing(self):
		self.use_company_settings(outgoing_party_posting="Manual", outgoing_bank_posting="Manual")
		batch = self.make_batch([1000, 2000, 3000])
		first, second, third = (row.pdc_cheque for row in batch.cheques)
		cleared = frappe.get_doc(CHEQUE, first)
		cleared.deliver(handover_date=day(-25))
		cleared.clear(clearance_date=day(-24))
		ensure_user(BATCH_MANAGER, ["Treasury Manager"])
		add_user_permission(BATCH_MANAGER, CHEQUE, third, applicable_for=CHEQUE)

		with self.set_user(BATCH_MANAGER):
			result = batch.print_cheques(language="en")
		self.assertEqual(result["printed"], [third])  # the unreadable and the Cleared cheques are left out
		self.assertEqual(frappe.db.get_value(CHEQUE, second, "print_count"), 0)

		# all or nothing: one cheque that cannot be reprinted stops the whole batch
		self.assertRaises(ChequeAlreadyPrintedError, batch.print_cheques, language="en")
		self.assertEqual(frappe.db.get_value(CHEQUE, second, "print_count"), 0)

		incoming = self.make_batch([500], direction="Incoming", party_type="Customer", party=CUSTOMER)
		self.assertRaises(frappe.ValidationError, incoming.print_cheques, language="en")

	def test_batch_without_layout(self):
		self.disable_layouts()
		batch = self.make_batch([1000, 2000])
		result = batch.print_cheques(language="en")
		self.assertEqual(result["doctype"], CHEQUE)
		self.assertEqual(result["names"], [row.pdc_cheque for row in batch.cheques])
		self.assertEqual(result["print_format"], LEGACY_FORMAT)

	# ------------------------------------------------------------------ layout test print and preview

	def test_layout_test_print(self):
		self.layout.print_crossing = 0
		self.layout.save()
		for lang, payee in (("en", "Sample Payee Name Trading Company"), ("ar", None)):
			self.set_lang(lang)
			html = self.render(LAYOUT, self.layout.name, FORMAT_TEST)
			self.assertIn("tcl-grid", html)
			self.assertIn("tcl-leaf-outline", html)
			for box in BOXES:
				self.assertIn(box, html)
			if payee:
				self.assertIn(payee, html)
			else:
				self.assertIn('dir="rtl"', html)
		self.assertEqual(frappe.db.get_value(CHEQUE, self.cheque.name, "print_count"), 0)

	def test_preview_unsaved(self):
		count = frappe.db.count(LAYOUT)
		values = {**self.layout.as_dict(), "date_left_mm": 50, "nudge_x_mm": 0, "layout_name": "Unsaved"}
		values.pop("name")
		html = preview_layout(frappe.as_json(values), "en")
		self.assertIn("left:50.00mm", html)
		self.assertIn("Sample Payee Name Trading Company", html)
		self.assertIn('dir="ltr"', preview_layout(values, "English"))
		self.assertIn('dir="rtl"', preview_layout(values))  # the layout's default language
		self.assertEqual(frappe.db.count(LAYOUT), count)
		self.assertRaises(frappe.ValidationError, preview_layout, {"doctype": CHEQUE}, "en")
		with self.set_user(NO_ACCESS_USER):
			self.assertRaises(frappe.PermissionError, preview_layout, values, "en")

	# ------------------------------------------------------------------ 7f-2: dialogs and test grid

	def test_cheque_text(self):
		"""The texts of the "Print Cheque" dialog: what build_leaf prints, escaped; nothing registered."""
		before = self.state(self.cheque)
		text = get_cheque_text(self.cheque.name, "English")
		self.assertEqual((text["lang"], text["dir"], text["figures"]), ("en", "ltr", "#1,250.50#"))
		self.assertEqual(text["words1"], self.leaf("en").words1)
		self.assertEqual(text["crossing"], "A/C Payee Only")
		self.assertEqual(get_cheque_text(self.cheque.name)["dir"], "rtl")  # the layout's default language
		self.assertEqual(self.state(self.cheque), before)
		self.assertEqual(frappe.db.get_value(CHEQUE, self.cheque.name, "print_count"), 0)

		doc = frappe.get_doc(CHEQUE, self.cheque.name)
		doc.payee_on_cheque = "Tom's \"ACME\" & Co"  # no "<": stored as typed (Frappe sanitizes HTML only)
		doc.save()
		self.assertEqual(
			get_cheque_text(self.cheque.name, "en")["payee"], "Tom&apos;s &quot;ACME&quot; &amp; Co"
		)

		cancelled = make_pdc_cheque()
		cancelled.cancel_cheque(reason="Wrong payee")
		self.assertRaises(frappe.ValidationError, get_cheque_text, cancelled.name, "en")
		self.assertRaises(frappe.ValidationError, get_cheque_text, self.cheque.name, "French")
		with self.set_user(NO_ACCESS_USER):
			self.assertRaises(frappe.PermissionError, get_cheque_text, self.cheque.name, "en")

	def test_batch_print_summary(self):
		batch = self.make_batch([1000, 2000, 3000])
		first, second, third = (row.pdc_cheque for row in batch.cheques)
		frappe.get_doc(CHEQUE, first).cancel_cheque(reason="Not needed")
		frappe.get_doc(CHEQUE, second).print_cheque(language="en")
		leaves = [frappe.db.get_value(CHEQUE, name, "cheque_no") for name in (second, third)]

		batch.run_method("onload")
		summary = batch.get_onload().cheque_print_summary
		self.assertEqual(
			(summary["printable"], summary["printed"], summary["unprinted"], summary["total"]), (2, 1, 1, 5000)
		)
		self.assertEqual((summary["leaves"], summary["unprinted_leaves"]), (leaves, leaves[1:]))
		self.assertEqual(
			summary["layout"],
			{"name": self.layout.name, "default_language": "Arabic", "reprint_requires_manager": True},
		)

		incoming = self.make_batch([500], direction="Incoming", party_type="Customer", party=CUSTOMER)
		incoming.run_method("onload")
		self.assertIsNone(incoming.get_onload().cheque_print_summary)

	def test_test_grid_labels_and_fonts(self):
		leaf = self.leaf("en")
		self.assertEqual((leaf.boxes.payee.position, leaf.boxes.date.position), ("25,22", "130,10"))
		self.assertEqual((leaf.figures_guard, leaf.figures_amount), ("#", fmt_money(1250.50, precision=2)))
		self.assertEqual((leaf.boxes.payee.font_size, leaf.boxes.crossing.font_size), ("11.0pt", "9.9pt"))
		layout = frappe.copy_doc(self.layout)
		layout.crossing_font_size_pt = 12
		self.assertEqual(self.leaf("en", layout).boxes.crossing.font_size, "12.0pt")

		html = str(render_leaf(self.layout, leaf, test_grid=True))
		self.assertIn("25,22", html)
		html = str(render_leaf(self.layout, leaf))
		self.assertNotIn("tcl-label", html)
		self.assertNotIn("tcl-grid", html)

"""Phase 7b-2: the server side of the Bank Transfer UX (design spec 7b).

- the extended fee preview (P1): bank currencies, the party's default bank details, the method limit and the
  approval rule, as the server will apply them on save;
- `get_transfer_settings` for a new transfer form;
- the default Expense Account of a fee rule (P12: not a grid column any more);
- the Bank Transfer Order print format (P7, P15; spec §7 J);
- the DocType / grid layout the scripts rely on (P2, P3, P9, P12, P16) and the whitelisted methods the
  client scripts call by path.
"""

import json
import re
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, formatdate, getdate, today

from treasury.bank_transfers import fees
from treasury.bank_transfers.api import get_fee_preview, get_transfer_settings
from treasury.tests.utils import (
	BANK,
	BANK_SWIFT,
	COMPANY,
	CUSTOMER,
	OTHER_BANK,
	OTHER_BANK_SWIFT,
	SUPPLIER,
	TRANSFER_SETTINGS,
	USD_RATE,
	TreasuryTestMixin,
	bank_account_name,
	ensure_other_bank,
	ensure_party_bank_account,
	ensure_second_egp_bank,
	ensure_user,
	fee_rule,
	make_bank_transfer,
	make_iban,
	set_fee_rules,
	set_transfer_limits,
	standard_fee_rules,
)

APP_PATH = Path(frappe.get_app_path("treasury"))
ARABIC = re.compile(r"[؀-ۿ]")
PRINT_FORMAT = "Bank Transfer Order"
TREASURY_USER = "treasury-bt-ux-user@example.com"
TREASURY_MANAGER = "treasury-bt-ux-manager@example.com"
NO_ACCESS_USER = "treasury-bt-ux-noaccess@example.com"
BENEFICIARY_KEYS = ("party_bank_account", "beneficiary_iban", "beneficiary_bank", "beneficiary_swift")


class BankTransferUXTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_other_bank()
		cls.b1 = bank_account_name("EGP")
		cls.bu = bank_account_name("USD")
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(TREASURY_MANAGER, ["Treasury Manager"])
		ensure_user(NO_ACCESS_USER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())


class TestFeePreview(BankTransferUXTestCase):
	def preview(self, **values):
		args = {
			"company": COMPANY,
			"bank_account": self.b1,
			"method": "Normal",
			"transfer_type": "Party Payment",
			"amount": 20000,
			**values,
		}
		return get_fee_preview(**args)

	def test_party_defaults(self):
		iban = make_iban("OTHB")
		account = ensure_party_bank_account("Supplier", SUPPLIER, bank=OTHER_BANK, iban=iban)
		r = self.preview(party_type="Supplier", party=SUPPLIER)
		self.assertEqual(r["party_bank_account"], account)
		self.assertEqual(r["beneficiary_iban"], iban)
		self.assertEqual(r["beneficiary_bank"], OTHER_BANK)
		self.assertEqual(r["beneficiary_swift"], OTHER_BANK_SWIFT)
		# the scope and fee already use them (R3: 10 + 0.1 %, min 15, max 50)
		self.assertEqual((r["scope"], r["computed_fee"]), (fees.OTHER, 30))

		# the values the user entered are kept: nothing is returned for them
		r = self.preview(
			party_type="Supplier",
			party=SUPPLIER,
			party_bank_account=account,
			beneficiary_iban=make_iban("TTRB"),
			beneficiary_bank=BANK,
		)
		for key in ("party_bank_account", "beneficiary_iban", "beneficiary_bank"):
			self.assertNotIn(key, r)
		# the SWIFT code of the bank the user chose
		self.assertEqual(r["beneficiary_swift"], BANK_SWIFT)
		self.assertEqual(r["scope"], fees.SAME)

	def test_no_details_of_another_partys_account(self):
		account = ensure_party_bank_account("Customer", CUSTOMER, bank=OTHER_BANK, iban=make_iban("OTHB"))
		r = self.preview(party_type="Supplier", party=SUPPLIER, party_bank_account=account)
		for key in BENEFICIARY_KEYS:
			self.assertNotIn(key, r)
		# an unknown party type / party: no defaults, no error
		r = self.preview(party_type="Not A Party Type", party=SUPPLIER)
		self.assertFalse(set(BENEFICIARY_KEYS) & set(r))
		r = self.preview(party_type="Supplier", party="No Such Supplier")
		self.assertFalse(set(BENEFICIARY_KEYS) & set(r))

	def test_internal_transfer_currencies_and_no_party(self):
		second = ensure_second_egp_bank().bank_account
		r = self.preview(
			transfer_type="Internal Transfer", to_bank_account=second, party_type="Supplier", party=SUPPLIER
		)
		self.assertEqual((r["currency"], r["to_currency"]), ("EGP", "EGP"))
		self.assertEqual(r["bank"], r["to_bank"])
		self.assertEqual(r["scope"], fees.SAME)
		self.assertFalse(set(BENEFICIARY_KEYS) & set(r))
		# the target is ignored for a party transfer
		r = self.preview(to_bank_account=second)
		self.assertIsNone(r["to_currency"])

	def test_exchange_rate_and_base_amount(self):
		r = self.preview()
		self.assertEqual((r["currency"], r["company_currency"], r["exchange_rate"]), ("EGP", "EGP", 1))
		self.assertEqual(r["base_amount"], 20000)

		usd = self.preview(bank_account=self.bu, amount=2500)
		self.assertEqual(usd["currency"], "USD")
		self.assertEqual(usd["exchange_rate"], USD_RATE)
		self.assertEqual(usd["base_amount"], 2500 * USD_RATE)
		# the user's own rate is kept (as the controller keeps it on save)
		own = self.preview(bank_account=self.bu, amount=2500, exchange_rate=52)
		self.assertEqual((own["exchange_rate"], own["base_amount"]), (52, 130000))

	def test_approval(self):
		self.use_company_settings(transfer_approval_threshold=100000)
		with self.set_user(TREASURY_USER):
			above = self.preview(amount=150000)
			self.assertEqual(above["approval_threshold"], 100000)
			self.assertTrue(above["above_threshold"])
			self.assertTrue(above["approval_required"])
			self.assertFalse(above["is_manager"])
			# the fee counts: 99,950 + 50 (R3, capped) = 100,000 is not above; 100,000 + 50 is
			at = self.preview(amount=99950)
			self.assertEqual(at["approval_amount"], 100000)
			self.assertFalse(at["above_threshold"])
			self.assertFalse(at["approval_required"])
			self.assertTrue(self.preview(amount=100000)["approval_required"])
			# an overridden fee counts as entered
			self.assertTrue(self.preview(amount=99950, fee_amount=51)["approval_required"])
			# in the company currency: 2,500 USD at 50 = 125,000
			self.assertTrue(self.preview(bank_account=self.bu, amount=2500)["approval_required"])
			# a low rate typed on the transfer does not lower it: the system rate (50) is used
			low = self.preview(bank_account=self.bu, amount=2500, exchange_rate=0.5)
			self.assertEqual(low["base_amount"], 1250)
			self.assertTrue(low["approval_required"])
		with self.set_user(TREASURY_MANAGER):
			manager = self.preview(amount=150000)
			self.assertTrue(manager["above_threshold"])
			self.assertFalse(manager["approval_required"])
			self.assertTrue(manager["is_manager"])
		self.use_company_settings(transfer_approval_threshold=0)
		with self.set_user(TREASURY_USER):
			self.assertFalse(self.preview(amount=150000)["approval_required"])

	def test_limit(self):
		limit = {
			"bank_account": self.b1,
			"method": "Fawran",
			"per_transaction_limit": 50000,
			"daily_limit": 100000,
		}
		set_transfer_limits(self, [limit])
		day = getdate(add_days(today(), -1))
		existing = make_bank_transfer(method="Fawran", amount=45000, execution_date=day)

		r = self.preview(method="Fawran", amount=60000, execution_date=day)
		self.assertEqual(
			r["limit"],
			{
				"method": "Fawran",
				"per_transaction_limit": 50000,
				"daily_limit": 100000,
				"used": 45000,
				"date": str(day),
			},
		)
		self.assertEqual(len(r["limit_messages"]), 2)  # per transaction and daily
		self.assertEqual(r["limit_action"], "Warn")

		# the saved transfer itself is left out of its own day
		own = self.preview(method="Fawran", amount=45000, execution_date=day, name=existing.name)
		self.assertEqual((own["limit"]["used"], own["limit_messages"]), (0, []))

		self.use_company_settings(transfer_limit_action="Block")
		self.assertEqual(self.preview(method="Fawran", amount=60000)["limit_action"], "Block")
		self.assertIsNone(self.preview(method="Normal")["limit"])

	def test_zero_amount_still_returns_the_bank_details(self):
		r = self.preview(amount=0)
		self.assertEqual(r["currency"], "EGP")
		self.assertEqual(r["limit_messages"], [])
		self.assertEqual(r["base_amount"], 0)

	def test_permission(self):
		with self.set_user(NO_ACCESS_USER):
			self.assertRaises(frappe.PermissionError, self.preview)
			self.assertRaises(frappe.PermissionError, get_transfer_settings, COMPANY)


class TestTransferSettings(BankTransferUXTestCase):
	def test_settings_for_a_new_form(self):
		self.use_company_settings(
			default_transfer_method="Fawran",
			default_fawran_bank_account=self.b1,
			refund_fees_on_return=1,
			transfer_approval_threshold=5000,
		)
		with self.set_user(TREASURY_USER):
			settings = get_transfer_settings(COMPANY)
		self.assertEqual(settings["default_transfer_method"], "Fawran")
		self.assertEqual(settings["default_bank_accounts"]["Fawran"], self.b1)
		self.assertEqual(settings["default_charge_bearer"], "SHA")
		self.assertEqual(settings["refund_fees_on_return"], 1)
		self.assertEqual(settings["transfer_approval_threshold"], 5000)
		self.assertEqual(settings["company_currency"], "EGP")
		self.assertRaises(frappe.ValidationError, get_transfer_settings, "No Such Company")

	def test_onload_carries_the_settings(self):
		doc = make_bank_transfer(submit=False)
		doc = frappe.get_doc("Bank Transfer", doc.name)
		doc.run_method("onload")
		self.assertIn("refund_fees_on_return", doc.get_onload().transfer_settings)

	def test_fee_rule_expense_account_default(self):
		settings = frappe.get_doc("Treasury Company Settings", COMPANY)
		mapping = next(r for r in settings.pdc_accounts if r.bank_account == self.b1)
		settings.set("transfer_fee_rules", [fee_rule(self.b1, "Normal", fixed_fee=5, expense_account=None)])
		settings.set("transfer_limits", [])
		settings.validate()
		self.assertEqual(settings.transfer_fee_rules[0].expense_account, mapping.bank_charges_account)

		# a bank without a PDC Accounts row: the Bounce Charges Account
		second = ensure_second_egp_bank().bank_account
		settings.set("pdc_accounts", [r for r in settings.pdc_accounts if r.bank_account != second])
		settings.bounce_charges_account = mapping.bank_charges_account
		settings.set("transfer_fee_rules", [fee_rule(second, "Normal", fixed_fee=5, expense_account=None)])
		settings.validate()
		self.assertEqual(settings.transfer_fee_rules[0].expense_account, mapping.bank_charges_account)


class TestBankTransferOrderPrint(BankTransferUXTestCase):
	def setUp(self):
		super().setUp()
		previous = frappe.local.lang
		frappe.local.lang = "en"
		self.addCleanup(setattr, frappe.local, "lang", previous)

	@staticmethod
	def render(doc) -> str:
		return frappe.get_print("Bank Transfer", doc.name, print_format=PRINT_FORMAT)

	@staticmethod
	def words_ar(html: str) -> str:
		return re.search(r'class="bt-value bt-words-ar"[^>]*>([^<]*)<', html).group(1)

	def test_party_payment(self):
		iban = make_iban("OTHB")
		doc = make_bank_transfer(
			amount=20000, beneficiary_iban=iban, bank_reference="FT26273", remarks="Invoice <b>42</b>"
		)
		self.assertEqual(doc.status, "Sent")
		html = self.render(doc)

		self.assertIn("BANK TRANSFER ORDER", html)
		self.assertIn(doc.payment_entry, html)  # the filing key
		self.assertIn(doc.name, html)
		self.assertIn(doc.party_name, html)
		self.assertIn("Twenty Thousand", html)
		self.assertTrue(ARABIC.search(self.words_ar(html)))
		self.assertIn(f"{iban[:4]} {iban[4:8]}", html)  # grouped in fours
		self.assertIn("FT26273", html)
		self.assertIn("Bank Charges", html)  # fee 30, R3
		self.assertIn("&lt;b&gt;42&lt;/b&gt;", html)
		self.assertNotIn("<b>42</b>", html)
		self.assertNotIn('class="bt-watermark"', html)
		self.assertIn('dir="ltr"', html)
		self.assertIn('dir="rtl"', html)

	def test_draft_and_scheduled(self):
		draft = make_bank_transfer(submit=False, amount=500)
		html = self.render(draft)
		self.assertIn('class="bt-watermark"', html)
		self.assertIn("DRAFT", html)
		self.assertIn("— ____________", html)  # the Payment Entry number is written by hand

		future = getdate(add_days(today(), 5))
		scheduled = make_bank_transfer(amount=500, execution_date=future)
		self.assertEqual(scheduled.status, "Scheduled")
		html = self.render(scheduled)
		self.assertIn(f"Scheduled for {formatdate(future)}", html)
		self.assertNotIn('class="bt-watermark"', html)

	def test_type_variants(self):
		receipt = make_bank_transfer(submit=False, transfer_type="Party Receipt", amount=7000)
		html = self.render(receipt)
		self.assertIn("TRANSFER RECEIPT ADVICE", html)
		self.assertIn("Net Credited", html)
		self.assertIn("Amount Paid by Party", html)
		self.assertNotIn("Please debit our account", html)

		internal = make_bank_transfer(
			submit=False,
			transfer_type="Internal Transfer",
			to_bank_account=self.bu,
			amount=50000,
			received_amount=990,
		)
		html = self.render(internal)
		self.assertIn("INTERNAL TRANSFER ORDER", html)
		self.assertIn("Please transfer the amount below between our accounts.", html)
		self.assertIn("Amount to Credit", html)
		self.assertIn("1 USD = ", html)  # the rate in the foreign currency

	def test_default_print_format(self):
		self.assertEqual(frappe.get_meta("Bank Transfer").default_print_format, PRINT_FORMAT)
		self.assertEqual(frappe.db.get_value("Print Format", PRINT_FORMAT, "doc_type"), "Bank Transfer")


class TestBankTransferLayout(IntegrationTestCase):
	@staticmethod
	def definition(module: str, doctype: str) -> dict:
		path = APP_PATH / module / "doctype" / frappe.scrub(doctype) / f"{frappe.scrub(doctype)}.json"
		return json.loads(path.read_text(encoding="utf-8"))

	def assert_grid(self, module: str, doctype: str, expected: dict):
		fields = self.definition(module, doctype)["fields"]
		columns = {df["fieldname"]: df.get("columns") for df in fields if df.get("in_list_view")}
		self.assertEqual(columns, expected)
		self.assertLessEqual(sum(columns.values()), 10)

	def test_grids_fit_ten_columns(self):
		self.assert_grid(
			"treasury_setup",
			"Bank Transfer Fee Rule",
			{
				"bank_account": 2,
				"method": 1,
				"direction": 1,
				"scope": 1,
				"amount_from": 1,
				"amount_to": 1,
				"fixed_fee": 1,
				"fee_percent": 1,
				"max_fee": 1,
			},
		)
		self.assert_grid(
			"treasury_setup",
			"Bank Transfer Limit",
			{"bank_account": 4, "method": 2, "per_transaction_limit": 2, "daily_limit": 2},
		)
		self.assert_grid(
			"pdc",
			"PDC Clearing Match",
			{
				"selected": 1,
				"match_quality": 1,
				"bank_transaction": 2,
				"amount": 1,
				"cheque": 2,
				"cheque_no": 1,
				"bank_transfer": 2,
			},
		)

	def test_bank_transfer_form_layout(self):
		meta = frappe.get_meta("Bank Transfer")
		order = [df.fieldname for df in meta.fields]
		# the fee strip heads the Fees section; the voucher table has its own full-width section
		self.assertEqual(order[order.index("fees_section") + 1], "fee_summary_html")
		self.assertEqual(order[order.index("vouchers_html") - 1], "voucher_table_section")
		self.assertEqual(meta.get_field("status").depends_on, "eval:doc.docstatus>0")
		self.assertTrue(meta.get_field("references_section").collapsible_depends_on)
		list_view = {df.fieldname for df in meta.fields if df.in_list_view}
		self.assertLessEqual({"method", "execution_date", "amount", "payment_entry"}, list_view)
		# the type is shown by the method column's arrow, the status by the indicator
		self.assertFalse({"transfer_type", "status"} & list_view)

	def test_settings_sections_fold_once_filled(self):
		meta = frappe.get_meta("Treasury Company Settings")
		for section, table in (
			("transfer_fees_section", "transfer_fee_rules"),
			("transfer_limits_section", "transfer_limits"),
		):
			df = meta.get_field(section)
			expected = (1, f"eval:!(doc.{table}||[]).length")
			self.assertEqual((df.collapsible, df.collapsible_depends_on), expected)
		self.assertEqual(meta.get_field("transfer_fees_help").fieldtype, "HTML")

	def test_workspace_shortcut_counter(self):
		path = APP_PATH / "pdc" / "workspace" / "treasury" / "treasury.json"
		shortcuts = json.loads(path.read_text(encoding="utf-8"))["shortcuts"]
		shortcut = next(s for s in shortcuts if s["label"] == "Bank Transfer")
		self.assertEqual(shortcut["format"], "{} Awaiting Confirmation")
		self.assertEqual(
			json.loads(shortcut["stats_filter"]),
			[["Bank Transfer", "status", "=", "Sent", False], ["Bank Transfer", "docstatus", "=", 1, False]],
		)

	def test_client_scripts_call_whitelisted_methods(self):
		"""Every module method the Treasury scripts call by path (`frappe.xcall`) exists and is whitelisted."""
		literal = re.compile(r"""xcall\(\s*["'](treasury\.[\w.]+)["']""")
		prefixed = re.compile(r"""xcall\(\s*BT_API\s*\+\s*["'](\w+)["']""")
		paths, checked = set(), 0
		for script in APP_PATH.rglob("*.js"):
			source = script.read_text(encoding="utf-8")
			paths.update(literal.findall(source))
			paths.update(f"treasury.bank_transfers.api.{name}" for name in prefixed.findall(source))
		missing = []
		for path in sorted(paths):
			checked += 1
			try:
				method = frappe.get_attr(path)
			except (AttributeError, ImportError):
				method = None
			if not method or method not in frappe.whitelisted:
				missing.append(path)
		self.assertEqual(missing, [])
		self.assertIn("treasury.bank_transfers.api.get_fee_preview", paths)
		self.assertIn("treasury.bank_transfers.api.get_transfer_settings", paths)
		self.assertGreater(checked, 2)

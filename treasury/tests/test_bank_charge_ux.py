"""Phase 7d-2: the server side of the Bank Charges UX (design spec 7d).

- the settings the Bank Charge form's draft intro reads from `onload` (§1.3);
- the default Expense Account of a new category account row (P10);
- the "not pre-selected" hint of a keyword proposal whose category is not pre-selected (P14);
- the DocType / grid / workspace layout the scripts rely on (P1, P2, P3, P6, P9, P11, P12, P13, §6) and the
  whitelisted methods the new client scripts call by path.
"""

import json
import re
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	acc,
	add_user_permission,
	day,
	ensure_charge_category,
	ensure_company,
	ensure_second_egp_bank,
	ensure_user,
	make_bank_charge,
	make_bank_transaction,
	set_bank_suppliers,
)
from treasury.treasury_setup.doctype.bank_charge_category.bank_charge_category import (
	get_company_charge_account,
)

APP_PATH = Path(frappe.get_app_path("treasury"))
TREASURY_USER = "treasury-bch-ux-user@example.com"
MANAGER = "treasury-bch-ux-manager@example.com"
MANAGER_ONLY = "treasury-bch-ux-manager-only@example.com"
DOCTYPE = "Bank Charge"
NOT_PRESELECTED = "Not pre-selected (category setting)"


def definition(module: str, doctype: str) -> dict:
	folder = frappe.scrub(doctype)
	path = APP_PATH / module / "doctype" / folder / f"{folder}.json"
	return json.loads(path.read_text(encoding="utf-8"))


class TestBankChargeUX(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(TREASURY_USER, ["Treasury User", "Accounts User"])
		ensure_user(MANAGER, ["Treasury Manager", "Accounts User"])
		ensure_user(MANAGER_ONLY, ["Treasury Manager"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		set_bank_suppliers(self, [])

	# ------------------------------------------------------------------ form onload (§1.3)

	def test_onload_charge_settings(self):
		charge = make_bank_charge(submit=False, amount=311.5)
		charge.run_method("onload")
		settings = charge.get_onload().charge_settings
		self.assertEqual(
			(
				settings["bank_charge_voucher"],
				settings["bank_charge_clearance"],
				settings["company_currency"],
			),
			("Payment Entry", "On Posting", "EGP"),
		)
		self.use_company_settings(
			bank_charge_voucher="Journal Entry", bank_charge_clearance="On Bank Transaction"
		)
		charge = frappe.get_doc(DOCTYPE, charge.name)
		charge.run_method("onload")
		settings = charge.get_onload().charge_settings
		self.assertEqual(
			(settings["bank_charge_voucher"], settings["bank_charge_clearance"]),
			("Journal Entry", "On Bank Transaction"),
		)

	# ------------------------------------------------------------------ category account row (P10)

	def test_company_charge_account(self):
		# the test company's PDC Accounts rows share one Bank Charges Account
		self.assertEqual(get_company_charge_account(COMPANY), acc("Treasury Bank Charges"))
		self.assertIsNone(get_company_charge_account(""))
		with self.set_user(TREASURY_USER):  # read-only on categories
			self.assertRaises(frappe.PermissionError, get_company_charge_account, COMPANY)
		with self.set_user(MANAGER):
			self.assertEqual(get_company_charge_account(COMPANY), acc("Treasury Bank Charges"))
		# a Treasury Manager without any Company permission (security review 7d #3) ...
		with self.set_user(MANAGER_ONLY):
			self.assertEqual(get_company_charge_account(COMPANY), acc("Treasury Bank Charges"))
		# ... but restricted to another company by a User Permission
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		add_user_permission(MANAGER_ONLY, "Company", OTHER_COMPANY)
		with self.set_user(MANAGER_ONLY):
			self.assertRaises(frappe.PermissionError, get_company_charge_account, COMPANY)

		# two distinct accounts: no default
		second = ensure_second_egp_bank()
		other = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": f"BCH UX Charges {frappe.generate_hash(length=5)}",
				"parent_account": acc("Indirect Expenses"),
				"company": COMPANY,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		row = frappe.db.get_value(
			"PDC Account Mapping",
			{"parent": COMPANY, "bank_account": second.bank_account},
			["name", "bank_charges_account"],
			as_dict=True,
		)
		frappe.db.set_value("PDC Account Mapping", row.name, "bank_charges_account", other.name)
		self.addCleanup(
			frappe.db.set_value,
			"PDC Account Mapping",
			row.name,
			"bank_charges_account",
			row.bank_charges_account,
		)
		self.assertIsNone(get_company_charge_account(COMPANY))

	# ------------------------------------------------------------------ clearing proposals (P14)

	def test_not_preselected_hint(self):
		egp = self.data.egp
		manual = ensure_charge_category("_Test BCH UX Manual", keywords=("ux manual fee",), auto_select=0)
		auto = ensure_charge_category("_Test BCH UX Auto", keywords=("ux auto fee",))
		manual_bt = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=321.1, description="UX MANUAL FEE"
		)
		auto_bt = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=321.2, description="UX AUTO FEE"
		)

		tool = frappe.get_doc("PDC Bank Clearing")
		tool.update(
			{"company": COMPANY, "bank_account": egp.bank_account, "from_date": day(-30), "to_date": day(0)}
		)
		tool.set("matches", [])
		tool.get_suggestions()
		rows = {row.bank_transaction: row for row in tool.matches}
		self.assertEqual(
			(rows[manual_bt.name].bank_charge_category, rows[manual_bt.name].selected), (manual, 0)
		)
		self.assertIn(NOT_PRESELECTED, rows[manual_bt.name].result)
		self.assertEqual((rows[auto_bt.name].bank_charge_category, rows[auto_bt.name].selected), (auto, 1))
		self.assertFalse(rows[auto_bt.name].result)

	# ------------------------------------------------------------------ layout the scripts rely on

	def test_bank_charge_form_layout(self):
		meta = frappe.get_meta(DOCTYPE)
		order = [df.fieldname for df in meta.fields]
		# P2: the voucher table has its own full-width section; statuses only once submitted
		self.assertEqual(order[order.index("vouchers_html") - 1], "voucher_table_section")
		for fieldname in ("status", "accounting_status"):
			self.assertEqual(meta.get_field(fieldname).depends_on, "eval:doc.docstatus>0")
		self.assertEqual(meta.get_field("more_info_section").collapsible_depends_on, "eval:doc.remarks")
		# P1: the bank account is remembered; P3: a refund of a charge keeps the charge's accounts
		self.assertTrue(meta.get_field("bank_account").remember_last_selected_value)
		for fieldname in ("category", "expense_account", "bank_account", "company"):
			self.assertEqual(
				meta.get_field(fieldname).read_only_depends_on,
				"eval:doc.entry_type==='Refund' && doc.against_bank_charge",
				fieldname,
			)
		# P6: the list columns; the type is in the amount formatter, the status in the indicator
		list_view = {df.fieldname for df in meta.fields if df.in_list_view}
		self.assertEqual(list_view, {"posting_date", "category", "bank_account", "amount", "payment_entry"})
		self.assertLessEqual({"payment_entry", "journal_entry"}, set(meta.search_fields.split(",")))
		# the currency fields are shown by the script (company currency of the charge)
		self.assertFalse(meta.get_field("exchange_rate").depends_on)

	def test_settings_and_category_layout(self):
		settings = frappe.get_meta("Treasury Settings")
		hint = settings.get_field("bank_suppliers_hint")
		self.assertEqual((hint.fieldtype, hint.depends_on), ("HTML", "eval:!(doc.bank_suppliers||[]).length"))
		self.assertTrue(
			frappe.get_meta("Treasury Company Settings").get_field("bank_charges_section").collapsible
		)

		category = frappe.get_meta("Bank Charge Category")
		order = [df.fieldname for df in category.fields]
		self.assertEqual(order[order.index("keywords_preview") + 1], "accounts_section")
		self.assertEqual(category.get_field("keywords_preview").fieldtype, "HTML")
		self.assertTrue(category.get_field("auto_select_proposals").in_list_view)

	def test_clearing_grid(self):
		fields = {df["fieldname"]: df for df in definition("pdc", "PDC Clearing Match")["fields"]}
		self.assertEqual(
			fields["bank_charge_category"]["read_only_depends_on"],
			"eval:doc.match_quality!=='Keyword' || doc.bank_charge",
		)
		self.assertFalse(fields["bank_transfer"].get("in_list_view"))
		self.assertEqual(fields["cheque"]["columns"], 2)

	def test_workspace(self):
		path = APP_PATH / "pdc" / "workspace" / "treasury" / "treasury.json"
		workspace = json.loads(path.read_text(encoding="utf-8"))
		links = workspace["links"]
		start = next(
			i
			for i, link in enumerate(links)
			if link["type"] == "Card Break" and link["label"] == "Bank Transfers"
		)
		card = []
		for link in links[start + 1 :]:
			if link["type"] == "Card Break":
				break
			card.append(link["label"])
		self.assertEqual(
			card,
			[
				"Bank Transfer",
				"Bank Charge",
				"Bank Transfer Register",
				"Transfer Fees Analysis",
				"Bank Charges Analysis",
				"Bank Charge Category",
			],
		)
		self.assertEqual(links[start]["link_count"], len(card))
		shortcut = next(s for s in workspace["shortcuts"] if s["label"] == "Bank Charge")
		self.assertEqual(shortcut["color"], "Orange")
		self.assertFalse(shortcut.get("stats_filter"))

		sidebar = json.loads((APP_PATH / "workspace_sidebar" / "treasury.json").read_text(encoding="utf-8"))
		labels = [item["label"] for item in sidebar["items"]]
		self.assertEqual(labels[labels.index("Bank Transfer") + 1], "Bank Charge")
		self.assertIn("Bank Charge Category", labels[labels.index("Treasury Setup") :])

	def test_client_scripts_call_whitelisted_methods(self):
		"""Every module method the Bank Charge scripts call through their method prefix exists, is
		whitelisted and accepts GET (they only read)."""
		prefixes = {
			"BCH_METHOD": "treasury.bank_transfers.doctype.bank_charge.bank_charge.",
			"BCC_METHOD": "treasury.treasury_setup.doctype.bank_charge_category.bank_charge_category.",
		}
		paths = set()
		for script in APP_PATH.rglob("*.js"):
			source = script.read_text(encoding="utf-8")
			for constant, prefix in prefixes.items():
				declared = re.search(rf"""const\s+{constant}\s*=\s*["']([\w.]+)["']""", source)
				if declared:
					self.assertEqual(declared.group(1), prefix, script.name)
				names = re.findall(rf"""xcall\(\s*{constant}\s*\+\s*["'](\w+)["']""", source)
				paths.update(prefix + name for name in names)
		self.assertEqual(
			paths,
			{
				prefixes["BCH_METHOD"] + "get_category_defaults",
				prefixes["BCH_METHOD"] + "make_refund",
				prefixes["BCC_METHOD"] + "get_company_charge_account",
			},
		)
		for path in paths:
			method = frappe.get_attr(path)
			self.assertIn(method, frappe.whitelisted, path)
			self.assertIn("GET", frappe.allowed_http_methods_for_whitelisted_func.get(method, ()), path)

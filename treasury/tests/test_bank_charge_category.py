"""Phase 7d: Bank Charge Category validation, keywords and the default categories (spec 7d §2.1, §3.6, §7 E)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from treasury.bank_transfers import charge_defaults
from treasury.bank_transfers.doctype.bank_charge.bank_charge import get_category_defaults
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	acc,
	add_user_permission,
	ensure_company,
	ensure_second_egp_bank,
	ensure_user,
)
from treasury.treasury_setup.doctype.bank_charge_category.bank_charge_category import (
	get_account_row,
	parse_keywords,
)

DOCTYPE = "Bank Charge Category"
NO_ACCESS_USER = "treasury-bcc-accounts@example.com"
OTHER_COMPANY_USER = "treasury-bcc-other-company@example.com"


def category(name: str | None = None, **values):
	return frappe.get_doc(
		{"doctype": DOCTYPE, "category_name": name or f"_Test BCC {frappe.generate_hash(length=6)}", **values}
	)


class TestBankChargeCategory(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(NO_ACCESS_USER, ["Accounts User"])
		ensure_user(OTHER_COMPANY_USER, ["Treasury User"])

	def row(self, **values):
		return {"company": COMPANY, "expense_account": acc("Treasury Bank Charges"), **values}

	def test_account_rows(self):
		doc = category(accounts=[self.row(), self.row()])
		self.assertRaisesRegex(frappe.ValidationError, "already has an account", doc.insert)

		doc = category(accounts=[self.row(expense_account=acc("Creditors"))])
		self.assertRaisesRegex(frappe.ValidationError, "Root Type", doc.insert)

		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		other_cost_center = frappe.get_cached_value("Company", OTHER_COMPANY, "cost_center")
		doc = category(accounts=[self.row(cost_center=other_cost_center)])
		self.assertRaisesRegex(frappe.ValidationError, "does not belong to company", doc.insert)

		main = frappe.get_cached_value("Company", COMPANY, "cost_center")
		doc = category(accounts=[self.row(cost_center=main)]).insert()
		self.assertEqual(
			get_account_row(doc.name, COMPANY),
			{"expense_account": acc("Treasury Bank Charges"), "cost_center": main},
		)
		self.assertIsNone(get_account_row(doc.name, OTHER_COMPANY))

	def test_keywords(self):
		doc = category(keywords="  Acct   Maint Fee \n\nacct maint fee\nSMS Fee\n").insert()
		self.assertEqual(doc.keywords, "Acct Maint Fee\nSMS Fee")
		self.assertEqual(parse_keywords(doc.keywords), ["acct maint fee", "sms fee"])
		self.assertRaisesRegex(frappe.ValidationError, "too short", category(keywords="ok\nlonger").insert)

	def test_category_defaults(self):
		main = frappe.get_cached_value("Company", COMPANY, "cost_center")
		doc = category(accounts=[self.row(cost_center=main)], default_description="Monthly fee").insert()
		self.assertEqual(
			get_category_defaults(doc.name, COMPANY),
			{
				"expense_account": acc("Treasury Bank Charges"),
				"cost_center": main,
				"default_description": "Monthly fee",
			},
		)
		with self.set_user(NO_ACCESS_USER):
			self.assertRaises(frappe.PermissionError, get_category_defaults, doc.name, COMPANY)

		# a Treasury User restricted to another company by a User Permission (review 7d #7)
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		add_user_permission(OTHER_COMPANY_USER, "Company", OTHER_COMPANY)
		with self.set_user(OTHER_COMPANY_USER):
			self.assertRaises(frappe.PermissionError, get_category_defaults, doc.name, COMPANY)
			self.assertIsNone(get_category_defaults(doc.name, OTHER_COMPANY)["expense_account"])

	def test_default_categories(self):
		previous = frappe.db.get_global(charge_defaults.DEFAULTS_CREATED_KEY)
		self.addCleanup(frappe.db.set_global, charge_defaults.DEFAULTS_CREATED_KEY, previous)
		names = [d["category_name"] for d in charge_defaults.DEFAULT_CATEGORIES]
		for name in names:
			if frappe.db.exists(DOCTYPE, name):
				frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)

		# the mapping of the test company has one Bank Charges Account for its two banks
		frappe.db.set_global(charge_defaults.DEFAULTS_CREATED_KEY, None)
		self.assertEqual(sorted(charge_defaults.ensure_default_categories()), sorted(names))
		self.assertEqual(frappe.db.get_global(charge_defaults.DEFAULTS_CREATED_KEY), "1")
		self.assertEqual(
			get_account_row("Account Maintenance", COMPANY).expense_account, acc("Treasury Bank Charges")
		)
		self.assertEqual(frappe.db.get_value(DOCTYPE, "Transfer Fees", "auto_select_proposals"), 0)
		self.assertIn("sms", parse_keywords(frappe.db.get_value(DOCTYPE, "SMS Alerts", "keywords")))

		# a deleted default is not created again
		frappe.delete_doc(DOCTYPE, "Cheque Book", ignore_permissions=True, force=True)
		self.assertEqual(charge_defaults.ensure_default_categories(), [])
		self.assertFalse(frappe.db.exists(DOCTYPE, "Cheque Book"))

		# a second, distinct Bank Charges Account: no account row on a fresh run
		second = ensure_second_egp_bank()
		other_account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": f"BCC Charges {frappe.generate_hash(length=5)}",
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
		frappe.db.set_value("PDC Account Mapping", row.name, "bank_charges_account", other_account.name)
		self.addCleanup(
			frappe.db.set_value,
			"PDC Account Mapping",
			row.name,
			"bank_charges_account",
			row.bank_charges_account,
		)
		for name in names:
			if frappe.db.exists(DOCTYPE, name):
				frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)
		frappe.db.set_global(charge_defaults.DEFAULTS_CREATED_KEY, None)
		self.assertEqual(sorted(charge_defaults.ensure_default_categories()), sorted(names))
		self.assertIsNone(get_account_row("Account Maintenance", COMPANY))

	def test_default_categories_skip_invalid_account(self):
		"""An account a category row would reject does not break install / migrate: the categories are
		created without that company's row (review 7d #5)."""
		previous = frappe.db.get_global(charge_defaults.DEFAULTS_CREATED_KEY)
		self.addCleanup(frappe.db.set_global, charge_defaults.DEFAULTS_CREATED_KEY, previous)
		names = [d["category_name"] for d in charge_defaults.DEFAULT_CATEGORIES]
		account = acc("Treasury Bank Charges")
		for case, change, restore in (
			("disabled", {"disabled": 1}, {"disabled": 0}),
			("not an expense", {"root_type": "Liability"}, {"root_type": "Expense"}),
		):
			with self.subTest(case=case):
				for name in names:
					if frappe.db.exists(DOCTYPE, name):
						frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)
				frappe.db.set_value("Account", account, change)
				self.addCleanup(frappe.db.set_value, "Account", account, restore)
				frappe.db.set_global(charge_defaults.DEFAULTS_CREATED_KEY, None)
				self.assertNotIn(COMPANY, charge_defaults.valid_charge_accounts())
				self.assertEqual(sorted(charge_defaults.ensure_default_categories()), sorted(names))
				self.assertIsNone(get_account_row("Account Maintenance", COMPANY))
				frappe.db.set_value("Account", account, restore)
		self.assertEqual(charge_defaults.valid_charge_accounts().get(COMPANY), account)

	def test_uninstall_clears_defaults_key(self):
		# the categories are deleted with the app: a reinstall creates the defaults again (security 7d #5)
		from treasury import install

		previous = frappe.db.get_global(charge_defaults.DEFAULTS_CREATED_KEY)
		self.addCleanup(frappe.db.set_global, charge_defaults.DEFAULTS_CREATED_KEY, previous)
		frappe.db.set_global(charge_defaults.DEFAULTS_CREATED_KEY, "1")
		with (
			patch.object(install, "delete_custom_fields"),
			patch.object(install, "delete_app_doctype_customisations"),
			patch.object(install, "remove_legacy_permissions"),
			patch.object(install, "report_kept_series"),
		):
			install.before_uninstall()
		self.assertFalse(frappe.db.get_global(charge_defaults.DEFAULTS_CREATED_KEY))

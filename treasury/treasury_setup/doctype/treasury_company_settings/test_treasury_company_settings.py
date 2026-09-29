# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase

from treasury.tests.utils import (
	BANK,
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	acc,
	ensure_account,
	ensure_bank_account,
	ensure_company,
	mapping_row,
)

# Fixtures are built by treasury.tests.utils; don't let Frappe pull ERPNext's global test records.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Company", "Account", "Bank Account", "Print Format"]


class TestTreasuryCompanySettings(TreasuryTestMixin, IntegrationTestCase):
	def get_settings(self):
		return frappe.get_doc("Treasury Company Settings", COMPANY)

	def append_bank_row(self, doc, bank_account: str):
		row = mapping_row(self.data.egp)
		row["bank_account"] = bank_account
		doc.append("pdc_accounts", row)

	def test_valid_settings_save(self):
		doc = self.get_settings()
		doc.save()
		self.assertEqual(len(doc.pdc_accounts), 2)
		self.assertEqual(doc.outgoing_party_posting, "On Handover")
		self.assertEqual(doc.incoming_party_posting, "On Receipt")
		self.assertEqual(doc.on_bounce, "Reverse by Journal Entry")

	def test_rejects_non_bank_type_pdc_account(self):
		doc = self.get_settings()
		doc.pdc_accounts[0].pdc_payable_account = self.data.payable
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_rejects_wrong_root_type(self):
		doc = self.get_settings()
		doc.pdc_accounts[0].pdc_payable_account = self.data.egp.pdc_receivable
		self.assertRaises(frappe.ValidationError, doc.save)

		doc = self.get_settings()
		doc.pdc_accounts[0].pdc_receivable_account = self.data.egp.pdc_payable
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_rejects_currency_mismatch(self):
		doc = self.get_settings()
		doc.pdc_accounts[0].pdc_payable_account = self.data.usd.pdc_payable
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_rejects_account_of_another_company(self):
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		foreign = ensure_account(
			"PDC Payable EGP", "Current Liabilities", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR
		)
		doc = self.get_settings()
		doc.pdc_accounts[0].pdc_payable_account = foreign
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_rejects_duplicate_bank_account(self):
		doc = self.get_settings()
		row = doc.pdc_accounts[0].as_dict()
		for key in ("name", "idx", "creation", "modified"):
			row.pop(key, None)
		doc.append("pdc_accounts", row)
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_rejects_non_company_bank_account(self):
		name = "Treasury Party Account - " + BANK
		if not frappe.db.exists("Bank Account", name):
			frappe.get_doc(
				{
					"doctype": "Bank Account",
					"account_name": "Treasury Party Account",
					"bank": BANK,
					"is_company_account": 0,
				}
			).insert(ignore_permissions=True)
		doc = self.get_settings()
		self.append_bank_row(doc, name)
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

	def test_rejects_bank_account_of_another_company(self):
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		gl = ensure_account(
			"Treasury Bank EGP", "Bank Accounts", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR
		)
		bank_account = ensure_bank_account("Treasury Other Co Current", gl, company=OTHER_COMPANY)
		doc = self.get_settings()
		self.append_bank_row(doc, bank_account)
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

	def test_rejects_bank_account_with_non_bank_gl_account(self):
		gl = ensure_account("Treasury Non Bank GL", "Current Assets")
		bank_account = ensure_bank_account("Treasury Non Bank Current", gl)
		doc = self.get_settings()
		self.append_bank_row(doc, bank_account)
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

	def test_rejects_group_account(self):
		doc = self.get_settings()
		doc.bounce_charges_account = acc("Indirect Expenses")
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

	def test_charges_accounts_must_be_expense_in_company_currency(self):
		doc = self.get_settings()
		doc.bounce_charges_account = self.data.egp.bank_charges
		doc.run_method("validate")

		doc.bounce_charges_account = self.data.payable
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

		doc.bounce_charges_account = ensure_account(
			"Treasury USD Charges", "Indirect Expenses", currency="USD"
		)
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

		doc = self.get_settings()
		doc.pdc_accounts[0].bank_charges_account = self.data.egp.pdc_payable
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

	def test_under_collection_account_required_on_deposit(self):
		doc = self.get_settings()
		doc.incoming_under_collection = "On Deposit"
		doc.pdc_accounts[0].under_collection_account = None
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

		doc.incoming_under_collection = "Disabled"
		doc.run_method("validate")

	def test_warns_on_outgoing_on_creation(self):
		frappe.clear_messages()
		doc = self.get_settings()
		doc.outgoing_party_posting = "On Creation"
		# validate only (no DB write) so other tests keep the default settings
		doc.run_method("validate")
		messages = " ".join(
			str(m.get("message") if isinstance(m, dict) else m) for m in frappe.local.message_log
		)
		# compare with the translated msgid so the test does not depend on the user's language
		expected = _(
			"Outgoing cheques will be posted to the party as soon as they are created, even if they are never handed over."
		)
		self.assertIn(expected, messages)

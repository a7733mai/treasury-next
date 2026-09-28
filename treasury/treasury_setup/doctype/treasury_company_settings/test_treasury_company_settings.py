# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	ensure_account,
	ensure_company,
)

# Fixtures are built by treasury.tests.utils; don't let Frappe pull ERPNext's global test records.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Company", "Account", "Bank Account", "Print Format"]


class TestTreasuryCompanySettings(TreasuryTestMixin, IntegrationTestCase):
	def get_settings(self):
		return frappe.get_doc("Treasury Company Settings", COMPANY)

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

	def test_warns_on_outgoing_on_creation(self):
		frappe.clear_messages()
		doc = self.get_settings()
		doc.outgoing_party_posting = "On Creation"
		# validate only (no DB write) so other tests keep the default settings
		doc.run_method("validate")
		messages = " ".join(str(m) for m in frappe.local.message_log)
		self.assertIn("never handed over", messages)

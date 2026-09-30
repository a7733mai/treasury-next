"""Phase 7c: the "Pay / Receive from Documents" section of Treasury Company Settings and its migration patch
(spec 7c §2.1, §6, §7)."""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.patches.v1_1 import set_document_payment_defaults
from treasury.tests.utils import COMPANY, TreasuryTestMixin, make_own_bank

DOCTYPE = "Treasury Company Settings"


class TestDocumentPaymentSettings(TreasuryTestMixin, IntegrationTestCase):
	def settings(self, **values):
		doc = frappe.get_doc(DOCTYPE, COMPANY)
		doc.update(values)
		return doc

	def test_valid_settings_save(self):
		egp = self.data.egp
		self.addCleanup(
			frappe.db.set_value,
			DOCTYPE,
			COMPANY,
			frappe.db.get_value(
				DOCTYPE,
				COMPANY,
				[
					"default_outgoing_cheque_bank_account",
					"default_incoming_cheque_bank_account",
					"default_installment_count",
					"tender_bond_percent",
				],
				as_dict=True,
			),
		)
		doc = self.settings(
			default_outgoing_cheque_bank_account=egp.bank_account,
			default_incoming_cheque_bank_account=self.data.usd.bank_account,
			default_installment_count=120,
			tender_bond_percent=100,
		)
		doc.save()
		self.assertEqual(doc.default_installment_count, 120)

	def test_default_bank_needs_a_mapping_row(self):
		own = make_own_bank()
		for fieldname in ("default_outgoing_cheque_bank_account", "default_incoming_cheque_bank_account"):
			with self.subTest(fieldname=fieldname):
				with self.assertRaisesRegex(frappe.ValidationError, "needs a row in PDC Accounts"):
					self.settings(**{fieldname: own.bank_account}).save()

	def test_installment_count_and_tender_bond(self):
		for count in (0, 121):
			with self.subTest(count=count):
				with self.assertRaisesRegex(frappe.ValidationError, "must be between 1 and 120"):
					self.settings(default_installment_count=count).save()
		for percent in (-1, 101):
			with self.subTest(percent=percent):
				with self.assertRaisesRegex(frappe.ValidationError, "must be between 0 and 100"):
					self.settings(tender_bond_percent=percent).save()

	def test_patch_is_idempotent(self):
		self.use_company_settings(
			default_installment_count=0, default_installment_frequency="", auto_select_cheque_book=0
		)
		set_document_payment_defaults.execute()
		set_document_payment_defaults.execute()
		values = frappe.db.get_value(
			DOCTYPE,
			COMPANY,
			["default_installment_count", "default_installment_frequency", "auto_select_cheque_book"],
			as_dict=True,
		)
		self.assertEqual(
			(values.default_installment_count, values.default_installment_frequency, values.auto_select_cheque_book),
			(12, "Monthly", 1),
		)

		# values already set are kept
		self.use_company_settings(default_installment_count=6, default_installment_frequency="Quarterly")
		set_document_payment_defaults.execute()
		self.assertEqual(
			frappe.db.get_value(DOCTYPE, COMPANY, ["default_installment_count", "default_installment_frequency"]),
			(6, "Quarterly"),
		)

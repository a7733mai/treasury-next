"""Phase 7c: paying hrms Expense Claims / Employee Advances from Treasury (spec 7c §3.2, §4 H, §7).

hrms is optional: without it (or with a Payment Entry class that does not accept these references) the
mappers refuse clearly; the posting scenarios run only where hrms is installed."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate, today

from treasury.bank_transfers.api import make_bank_transfer as build_transfer
from treasury.pdc.api import make_cheque_batch as map_batch
from treasury.pdc.api import make_pdc_cheque as map_cheque
from treasury.tests.utils import (
	COMPANY,
	PE_SETTINGS,
	TreasuryTestMixin,
	assert_gl_entries,
	ensure_account,
	ensure_employee,
	ensure_fiscal_year,
	hrms_installed,
	isolate_cheque_books,
	make_expense_claim,
	settings_for_documents,
	unique_cheque_no,
)
from treasury.utils import hrms
from treasury.utils.references import reference_rules


class TestHRMSPayments(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_fiscal_year(COMPANY, today())

	def setUp(self):
		super().setUp()
		self.use_company_settings(
			**PE_SETTINGS,
			**settings_for_documents(default_outgoing_cheque_bank_account=self.data.egp.bank_account),
		)

	def test_hrms_absent(self):
		with patch("treasury.utils.hrms.hrms_installed", return_value=False):
			for mapper in (map_cheque, map_batch, build_transfer):
				with self.subTest(mapper=mapper.__name__):
					with self.assertRaisesRegex(frappe.ValidationError, "Expense Claim needs the HRMS app"):
						mapper("Expense Claim", "X")
			self.assertEqual(reference_rules("Outgoing", "Employee"), {"Journal Entry": None})
			self.assertEqual(hrms.supported_reference_doctypes(), ())

	def test_unsupported_reference(self):
		with (
			patch("treasury.utils.hrms.hrms_installed", return_value=True),
			patch("treasury.utils.hrms.employee_reference_doctypes", return_value=("Journal Entry",)),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not accept Expense Claim"):
				hrms.check_supported("Expense Claim")
			self.assertEqual(reference_rules("Outgoing", "Employee"), {"Journal Entry": None})
		with (
			patch("treasury.utils.hrms.hrms_installed", return_value=True),
			patch(
				"treasury.utils.hrms.employee_reference_doctypes",
				return_value=("Expense Claim", "Journal Entry", "Employee Advance"),
			),
		):
			self.assertEqual(
				reference_rules("Outgoing", "Employee"),
				{"Journal Entry": None, "Expense Claim": "employee", "Employee Advance": "employee"},
			)

	def test_expense_claim_cheque(self):
		if not hrms_installed():
			self.skipTest("hrms is not installed")
		egp = self.data.egp
		isolate_cheque_books(self, egp.bank_account)
		employee = ensure_employee()
		claim = make_expense_claim(1500, employee=employee)
		values = map_cheque("Expense Claim", claim.name)
		self.assertEqual(
			(values["direction"], values["party_type"], values["party"], values["amount"]),
			("Outgoing", "Employee", employee, 1500),
		)
		self.assertEqual(
			[
				(r["reference_doctype"], r["reference_name"], r["allocated_amount"])
				for r in values["references"]
			],
			[("Expense Claim", claim.name, 1500)],
		)
		values["cheque_no"] = unique_cheque_no()
		cheque = frappe.get_doc(values).insert()
		cheque.deliver(handover_date=today())
		cheque.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(claim.payable_account, 1500, 0, employee, getdate(today())),
				(egp.pdc_payable, 0, 1500, None, getdate(today())),
			],
		)
		self.assertEqual(frappe.db.get_value("Expense Claim", claim.name, "total_amount_reimbursed"), 1500)
		# nothing left to pay
		with self.assertRaisesRegex(frappe.ValidationError, "nothing outstanding"):
			map_cheque("Expense Claim", claim.name)

	def test_employee_advance_account_mismatch(self):
		if not hrms_installed():
			self.skipTest("hrms is not installed")
		employee = ensure_employee()
		advance_account = ensure_account("Treasury Employee Advances", "Accounts Receivable", "Receivable")
		advance = frappe.get_doc(
			{
				"doctype": "Employee Advance",
				"company": COMPANY,
				"employee": employee,
				"posting_date": today(),
				"purpose": "Travel",
				"advance_amount": 2000,
				"currency": "EGP",
				"exchange_rate": 1,
				"advance_account": advance_account,
			}
		)
		advance.flags.ignore_mandatory = True
		advance.insert(ignore_permissions=True)
		advance.submit()
		for mapper in (map_cheque, build_transfer):
			with self.subTest(mapper=mapper.__name__):
				with self.assertRaisesRegex(frappe.ValidationError, "uses account"):
					mapper("Employee Advance", advance.name)

"""Phase 7g (spec §7 A): lenders (bank / finance company), the Islamic flag and wording, late payment charity
settings, the security cheque to the lender, permissions of the new actions and the migration patch.

Fixed dates in 2025 (a fiscal year `ensure_company` creates); every test creates its own facility.
"""

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.bank_facilities import lenders
from treasury.bank_facilities.report.facility_repayment_schedule_report.facility_repayment_schedule_report import (
	execute as schedule_report,
)
from treasury.bank_transfers import bank_supplier
from treasury.security_cheques import api as security_api
from treasury.tests.utils import (
	BANK,
	COMPANY,
	FACILITY_PE,
	LENDER,
	TreasuryTestMixin,
	bank_account_name,
	ensure_facility_accounts,
	ensure_islamic_accounts,
	ensure_lender,
	ensure_other_company_bank,
	ensure_user,
	get_voucher_count,
	make_bank_facility,
	make_drawdown,
	make_finance_facility,
	set_bank_suppliers,
	unique_cheque_no,
)

TREASURY_USER = "treasury-7g-lender-user@example.com"
ACCOUNTS_USER = "treasury-7g-lender-accounts@example.com"


class LenderTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.islamic = ensure_islamic_accounts()
		ensure_lender()
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(ACCOUNTS_USER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0, **FACILITY_PE)
		set_bank_suppliers(self, [])

	def assert_error(self, message: str, fn, *args, **kwargs):
		with self.assertRaises(frappe.ValidationError) as ctx:
			fn(*args, **kwargs)
		self.assertIn(message, str(ctx.exception))


class TestLenders(LenderTestCase):
	def test_finance_company_validation(self):
		# a lender is required
		self.assert_error(
			"needs a Lender",
			make_bank_facility,
			activate=False,
			lender_type="Finance Company",
			lender=None,
			bank=None,
		)
		# the bank is optional; the lender's name comes from the Supplier; the default bank account's bank is
		# not compared with a finance company's (it has none)
		facility = make_finance_facility(activate=False)
		self.assertIsNone(facility.bank)
		self.assertEqual(facility.lender, LENDER)
		self.assertEqual(facility.lender_name, frappe.db.get_value("Supplier", LENDER, "supplier_name"))
		self.assertEqual(facility.default_bank_account, bank_account_name("EGP"))
		self.assertEqual(facility.is_islamic, 1)  # a Murabaha sub-limit
		# no overdraft or bank instrument sub-limit
		for limit_type in ("Overdraft", "Letter of Guarantee"):
			with self.subTest(limit_type=limit_type):
				self.assert_error(
					"A finance company facility cannot have a",
					make_finance_facility,
					activate=False,
					limits=[{"limit_type": limit_type, "limit_amount": 10}],
				)
		# a bank facility still needs its bank, and its default bank account at that bank
		self.assert_error("Bank is required", make_bank_facility, activate=False, bank=None)
		other = ensure_other_company_bank()
		self.assert_error(
			"is not at bank", make_bank_facility, activate=False, bank_account=other.bank_account
		)
		# a disabled lender
		disabled = ensure_lender("_Test Treasury Lender Disabled")
		frappe.db.set_value("Supplier", disabled, "disabled", 1)
		self.addCleanup(frappe.db.set_value, "Supplier", disabled, "disabled", 0)
		self.assert_error("is disabled", make_finance_facility, activate=False, lender=disabled)

	def test_bank_lender_supplier_on_demand(self):
		facility = make_bank_facility()
		self.assertIsNone(facility.lender)
		self.assertEqual(facility.lender_name, frappe.db.get_value("Bank", BANK, "bank_name") or BANK)
		supplier = lenders.get_lender_supplier(facility.name)
		self.assertEqual(supplier, bank_supplier.get_bank_supplier(BANK))
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "lender"), supplier)
		# reused, not created again
		suppliers = frappe.db.count("Supplier")
		self.assertEqual(lenders.get_lender_supplier(facility.name), supplier)
		self.assertEqual(frappe.db.count("Supplier"), suppliers)
		# a save from a form loaded before keeps the stored (locked) lender
		facility.collateral_notes = "Pledge"
		facility.save()
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "lender"), supplier)

	def test_is_islamic_auto_and_locked(self):
		murabaha = [
			{
				"limit_type": "Murabaha",
				"limit_amount": 500_000,
				"interest_rate": 12,
				"liability_account": self.islamic.murabaha,
			}
		]
		facility = make_bank_facility(limits=murabaha, activate=False)
		self.assertEqual(facility.is_islamic, 1)
		conventional = make_bank_facility(activate=False)
		self.assertEqual(conventional.is_islamic, 0)

		facility.activate(activation_date=facility.start_date)
		facility.reload()
		facility.is_islamic = 0
		self.assert_error("cannot be changed after the facility is activated", facility.save)
		facility.reload()
		facility.lender_type = "Finance Company"
		facility.lender = LENDER
		self.assert_error("cannot be changed after the facility is activated", facility.save)

	def test_charity_settings_validation(self):
		self.assert_error(
			"Set the Late Payment (Charity) Account and the Charity Payable Account.",
			make_bank_facility,
			activate=False,
			late_payment_mode="Payable to Charity",
		)
		self.assert_error(
			"must have Root Type",
			make_bank_facility,
			activate=False,
			late_payment_charity_account=self.islamic.charity_payable,
		)
		self.assert_error(
			"must have Root Type",
			make_bank_facility,
			activate=False,
			late_payment_charity_account=self.islamic.charity,
			charity_payable_account=self.islamic.charity,
		)
		facility = make_bank_facility(
			activate=False,
			late_payment_mode="Payable to Charity",
			late_payment_charity_account=self.islamic.charity,
			charity_payable_account=self.islamic.charity_payable,
		)
		self.assertEqual(facility.late_payment_mode, "Payable to Charity")
		# the default
		self.assertEqual(make_bank_facility(activate=False).late_payment_mode, "Paid to Lender")

	def test_terms_labels(self):
		self.assertEqual(lenders.terms(True).interest, _("Profit"))
		self.assertEqual(lenders.terms(False).interest, _("Interest"))
		self.assertEqual(lenders.terms(True).accrued_interest, _("Accrued Profit"))
		self.assertEqual(lenders.terms_for([1, 1]).interest, _("Profit"))
		self.assertEqual(lenders.terms_for([0]).interest, _("Interest"))
		self.assertEqual(lenders.terms_for([0, 1]).interest, _("Interest / Profit"))

		facility = make_finance_facility()
		drawdown = make_drawdown(facility, limit_type="Murabaha")
		self.assertEqual(drawdown.is_islamic, 1)
		filters = {
			"company": COMPANY,
			"from_date": "2025-01-01",
			"to_date": "2026-12-31",
			"drawdown": drawdown.name,
			"include_paid": 1,
		}
		columns, data, *_rest = schedule_report(filters)
		self.assertTrue(data)
		labels = {c["fieldname"]: c["label"] for c in columns}
		self.assertEqual(labels["interest"], "Profit")
		drawdown.load_from_db()
		drawdown.run_method("onload")
		self.assertEqual(drawdown.get_onload("terms").interest, _("Profit"))

	def test_security_cheque_mapper(self):
		far = "2030-12-31"
		bank_facility = make_bank_facility(expiry_date=far)
		data = security_api.make_security_cheque("Bank Facility", bank_facility.name)
		self.assertEqual((data["direction"], data["purpose"]), ("Outgoing", "Facility Collateral"))
		# no lender Supplier yet: party "Other" with the lender's (bank's) name
		self.assertEqual(data["party_type"], "Other")
		self.assertEqual(data["party_name_other"], bank_facility.lender_name)

		facility = make_finance_facility(expiry_date=far, total_limit=750_000)
		data = security_api.make_security_cheque("Bank Facility", facility.name)
		self.assertEqual((data["party_type"], data["party"]), ("Supplier", LENDER))
		self.assertEqual((data["amount"], data["required_amount"]), (750_000, 750_000))
		self.assertEqual(getdate(data["expiry_date"]), getdate(far))
		self.assertEqual(
			(data["reference_doctype"], data["reference_name"]), ("Bank Facility", facility.name)
		)
		self.assertEqual(data["bank_account"], facility.default_bank_account)

		values = {k: v for k, v in data.items() if not k.startswith("__") and k != "name"}
		values.update(cheque_no=unique_cheque_no(), cheque_book=None)
		cheque = frappe.get_doc(values).insert()
		facility.reload()
		self.assertEqual([row.security_cheque for row in facility.security_cheques], [cheque.name])
		self.assertEqual(facility.security_cheques[0].amount, 750_000)
		# rule 4: no accounting
		self.assertEqual(get_voucher_count("Security Cheque", cheque.name), 0)

		# not for a Draft facility
		draft = make_finance_facility(activate=False, expiry_date=far)
		self.assertRaises(
			frappe.ValidationError, security_api.make_security_cheque, "Bank Facility", draft.name
		)

	def test_permissions(self):
		# a Treasury User may not disburse an Istisna'a stage (Treasury Manager action)
		facility = make_bank_facility(
			limits=[
				{
					"limit_type": "Istisna'a",
					"limit_amount": 500_000,
					"interest_rate": 10,
					"liability_account": self.islamic.istisna,
				}
			]
		)
		drawdown = make_drawdown(
			facility,
			amount=100_000,
			limit_type="Istisna'a",
			disbursement_stages=[{"stage_date": "2025-02-15", "amount": 100_000}],
		)
		with self.set_user(TREASURY_USER):
			self.assertRaises(
				frappe.PermissionError, drawdown.disburse_stage, stage=drawdown.disbursement_stages[0].name
			)
		# an Accounts User may not generate installment cheques (no Cheque Batch create permission)
		loan = make_drawdown(make_bank_facility(), repayment_mode="PDC to Lender")
		with self.set_user(ACCOUNTS_USER):
			self.assertRaises(frappe.PermissionError, loan.make_installment_cheques)


class TestFacilityDefaultsPatch(LenderTestCase):
	def test_patch_fills_the_empty_values(self):
		from treasury.patches.v1_1 import set_facility_defaults

		facility = make_bank_facility(
			limits=[
				{
					"limit_type": "Murabaha",
					"limit_amount": 500_000,
					"interest_rate": 12,
					"liability_account": self.islamic.murabaha,
				}
			]
		)
		drawdown = make_drawdown(facility, limit_type="Murabaha")
		frappe.db.set_value(
			"Bank Facility",
			facility.name,
			{"lender_type": "", "lender_name": "", "is_islamic": 0, "late_payment_mode": ""},
		)
		frappe.db.set_value("Facility Drawdown", drawdown.name, {"repayment_mode": "", "disburse_to": ""})
		row = drawdown.repayment_schedule[0].name
		frappe.db.set_value(
			"Facility Repayment Schedule", row, {"payment_journal": "JV-PATCH", "payment_voucher_type": ""}
		)
		for _run in range(2):  # idempotent
			set_facility_defaults.execute()
		values = frappe.db.get_value(
			"Bank Facility",
			facility.name,
			["lender_type", "lender_name", "is_islamic", "late_payment_mode"],
			as_dict=True,
		)
		self.assertEqual(
			(values.lender_type, values.is_islamic, values.late_payment_mode), ("Bank", 1, "Paid to Lender")
		)
		self.assertEqual(values.lender_name, frappe.db.get_value("Bank", BANK, "bank_name") or BANK)
		self.assertEqual(
			frappe.db.get_value("Facility Drawdown", drawdown.name, ["repayment_mode", "disburse_to"]),
			("Manual", "Bank Account"),
		)
		self.assertEqual(
			frappe.db.get_value("Facility Repayment Schedule", row, "payment_voucher_type"), "Journal Entry"
		)
		self.assertIn(
			frappe.db.get_value("Treasury Company Settings", COMPANY, "facility_voucher"),
			("Payment Entry", "Journal Entry"),
		)

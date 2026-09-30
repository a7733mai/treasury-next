"""Phase 7g (spec §7 B): facility vouchers as Payment Entries with the whole amount in deductions (spec §4.2,
§4.3): drawdowns (Receive from the lender), repayments (Pay to the lender), accrued interest, Islamic rows,
late payment charity, foreign currency and the Journal Entry setting.

Every Payment Entry is checked row by row (GL and deductions), books no Payment Ledger Entry and leaves the
lender's balance at 0. The standard example: EGP 120,000 drawn on 2025-01-31, 12 monthly Equal Principal
installments at 12 % Actual/365; installment 1 (2025-02-28) = 10,000.00 + 1,104.66.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.accounting import posting
from treasury.bank_facilities import accounting, interest, lenders
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.tests.utils import (
	COMPANY,
	FACILITY_JE,
	FACILITY_PE,
	SUPPLIER,
	TreasuryTestMixin,
	assert_deductions,
	assert_gl_entries,
	ensure_facility_accounts,
	ensure_islamic_accounts,
	ensure_lender,
	get_party_balance,
	get_pe,
	make_bank_facility,
	make_drawdown,
	make_finance_facility,
	make_own_bank,
	make_repayment,
	set_bank_suppliers,
)

D = getdate


def ple_count(voucher_no: str) -> int:
	return frappe.db.count("Payment Ledger Entry", {"voucher_no": voucher_no})


def schedule_row(drawdown: str, installment_no: int) -> frappe._dict:
	return frappe.get_all(
		"Facility Repayment Schedule",
		filters={"parent": drawdown, "installment_no": installment_no, "is_prepayment": 0},
		fields=["*"],
	)[0]


class FacilityPETestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.islamic = ensure_islamic_accounts()
		ensure_lender()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0, **FACILITY_PE)
		set_bank_suppliers(self, [])

	def loan(self, facility=None, **values):
		bank = make_own_bank()
		facility = facility or make_bank_facility(bank_account=bank.bank_account)
		values.setdefault("bank_account", facility.default_bank_account)
		drawdown = make_drawdown(facility, **values)
		drawdown.reload()
		return frappe._dict(
			facility=facility,
			drawdown=drawdown,
			bank_gl=frappe.db.get_value("Bank Account", drawdown.bank_account, "account"),
		)

	def assert_no_party_effect(self, pe_name: str, facility):
		pe = get_pe(pe_name)
		self.assertEqual(ple_count(pe_name), 0)
		self.assertEqual((pe.party_type, pe.party), ("Supplier", lenders.get_lender_supplier(facility.name)))
		self.assertEqual(get_party_balance("Supplier", pe.party, "2026-12-31"), 0)


class TestFacilityPaymentEntries(FacilityPETestCase):
	def test_drawdown_pe_with_fees(self):
		s = self.loan(upfront_fees=1200, fees_account=self.accounts.fees)
		dd, date = s.drawdown, D("2025-01-31")
		self.assertTrue(dd.payment_entry)
		self.assertEqual((dd.journal_entry, dd.fees_journal_entry), (None, None))
		assert_gl_entries(
			self,
			"Payment Entry",
			dd.payment_entry,
			[
				(s.bank_gl, 118_800, 0, None, date),
				(self.accounts.fees, 1200, 0, None, date),
				(self.accounts.loan_egp, 0, 120_000, None, date),
			],
		)
		pe = get_pe(dd.payment_entry)
		self.assertEqual((pe.payment_type, pe.paid_amount, pe.received_amount), ("Receive", 118_800, 118_800))
		self.assertEqual(pe.paid_to, s.bank_gl)
		self.assertEqual(pe.clearance_date, date)
		assert_deductions(self, pe, [(self.accounts.loan_egp, -120_000, 0), (self.accounts.fees, 1200, 0)])
		self.assert_no_party_effect(dd.payment_entry, s.facility)
		self.assertEqual(get_utilisation(s.facility.name).utilised, 120_000)

		# idempotent
		self.assertEqual(accounting.post_drawdown(dd).payment_entry, dd.payment_entry)
		dd.cancel()
		self.assertEqual(frappe.db.get_value("Payment Entry", dd.payment_entry, "docstatus"), 2)
		self.assertEqual(get_utilisation(s.facility.name).utilised, 0)

	def test_repayment_pe_accrual_off(self):
		s = self.loan()
		repayment = make_repayment(s.drawdown, "2025-02-28")
		date = D("2025-02-28")
		self.assertTrue(repayment.payment_entry)
		self.assertIsNone(repayment.journal_entry)
		self.assertEqual(repayment.settlement, "Bank")
		assert_gl_entries(
			self,
			"Payment Entry",
			repayment.payment_entry,
			[
				(self.accounts.loan_egp, 10_000, 0, None, date),
				(self.accounts.interest, 1104.66, 0, None, date),
				(s.bank_gl, 0, 11_104.66, None, date),
			],
		)
		pe = get_pe(repayment.payment_entry)
		self.assertEqual((pe.payment_type, pe.paid_from, pe.paid_amount), ("Pay", s.bank_gl, 11_104.66))
		self.assertEqual(pe.clearance_date, date)
		assert_deductions(
			self, pe, [(self.accounts.loan_egp, 10_000, 0), (self.accounts.interest, 1104.66, 0)]
		)
		self.assert_no_party_effect(repayment.payment_entry, s.facility)
		row = schedule_row(s.drawdown.name, 1)
		self.assertEqual(
			(row.status, row.payment_voucher_type, row.payment_journal),
			("Paid", "Payment Entry", repayment.payment_entry),
		)
		self.assertEqual(get_utilisation(s.facility.name).utilised, 110_000)
		# idempotent
		self.assertEqual(
			accounting.post_repayment(repayment, s.drawdown, s.facility),
			("Payment Entry", repayment.payment_entry),
		)

	def test_repayment_pe_accrual_on(self):
		self.use_company_settings(accrue_facility_interest=1)
		s = self.loan()
		accruals = interest.accrue_drawdown(s.drawdown.name, "2025-03-01")
		self.assertEqual(len(accruals), 1)
		assert_gl_entries(
			self,
			"Journal Entry",
			accruals[0],
			[(self.accounts.interest, 1104.66, 0), (self.accounts.accrued_egp, 0, 1104.66)],
		)
		repayment = make_repayment(s.drawdown, "2025-02-28")
		self.assertEqual((repayment.accrued_interest_cleared, repayment.interest_expensed), (1104.66, 0))
		assert_gl_entries(
			self,
			"Payment Entry",
			repayment.payment_entry,
			[
				(self.accounts.loan_egp, 10_000, 0),
				(self.accounts.accrued_egp, 1104.66, 0),
				(s.bank_gl, 0, 11_104.66),
			],
		)
		self.assertEqual(interest.accrued_balance(s.drawdown, "2025-02-28"), 0)

	def test_foreign_currency_keeps_je(self):
		facility = make_bank_facility(currency="USD", total_limit=100_000)
		drawdown = make_drawdown(facility, amount=12_000)
		drawdown.reload()
		self.assertTrue(drawdown.journal_entry)
		self.assertIsNone(drawdown.payment_entry)
		repayment = make_repayment(drawdown, "2025-02-28")
		self.assertEqual((repayment.principal_paid, repayment.interest_paid), (1000, 110.47))
		self.assertIsNone(repayment.payment_entry)
		assert_gl_entries(
			self,
			"Journal Entry",
			repayment.journal_entry,
			[
				(self.accounts.loan_usd, 50_000, 0),
				(self.accounts.interest, 5523.50, 0),
				(self.data.usd.bank_gl, 0, 55_523.50),
			],
		)

	def test_je_setting(self):
		self.use_company_settings(**FACILITY_JE)
		s = self.loan(upfront_fees=1200, fees_account=self.accounts.fees)
		with self.subTest("drawdown"):
			self.assertTrue(s.drawdown.journal_entry and s.drawdown.fees_journal_entry)
			self.assertIsNone(s.drawdown.payment_entry)
			assert_gl_entries(
				self,
				"Journal Entry",
				s.drawdown.journal_entry,
				[(s.bank_gl, 120_000, 0), (self.accounts.loan_egp, 0, 120_000)],
			)
		with self.subTest("repayment"):
			repayment = make_repayment(s.drawdown, "2025-02-28")
			self.assertTrue(repayment.journal_entry)
			self.assertIsNone(repayment.payment_entry)
			row = schedule_row(s.drawdown.name, 1)
			self.assertEqual(
				(row.payment_voucher_type, row.payment_journal), ("Journal Entry", repayment.journal_entry)
			)

	def test_murabaha_and_ijarah_rows(self):
		# R4: Murabaha with a fixed profit of 7,200 (600 per installment)
		facility = make_finance_facility()
		s = self.loan(facility, limit_type="Murabaha", profit_amount=7200, interest_rate=0)
		self.assertEqual(s.drawdown.is_islamic, 1)
		repayment = make_repayment(s.drawdown, "2025-02-28")
		self.assertEqual((repayment.principal_paid, repayment.interest_paid), (10_000, 600))
		pe = get_pe(repayment.payment_entry)
		assert_deductions(self, pe, [(self.islamic.murabaha, 10_000, 0), (self.islamic.profit, 600, 0)])
		self.assertEqual(pe.party, frappe.db.get_value("Bank Facility", facility.name, "lender"))
		# R5: Ijarah 240,000, profit 24,000, 24 monthly rentals of 11,000
		ijarah = make_finance_facility(
			limits=[
				{
					"limit_type": "Ijarah",
					"limit_amount": 500_000,
					"interest_rate": 0,
					"liability_account": self.islamic.ijarah,
				}
			]
		)
		s = self.loan(ijarah, limit_type="Ijarah", amount=240_000, tenor_months=24, profit_amount=24_000)
		self.assertEqual({row.total for row in s.drawdown.repayment_schedule}, {11_000})
		repayment = make_repayment(s.drawdown, "2025-02-28")
		assert_gl_entries(
			self,
			"Payment Entry",
			repayment.payment_entry,
			[(self.islamic.ijarah, 10_000, 0), (self.islamic.profit, 1000, 0), (s.bank_gl, 0, 11_000)],
		)

	def islamic_loan(self, mode: str):
		facility = make_finance_facility(
			late_payment_mode=mode,
			late_payment_charity_account=self.islamic.charity,
			charity_payable_account=self.islamic.charity_payable,
		)
		return self.loan(facility, limit_type="Murabaha")

	@staticmethod
	def late_repayment(drawdown):
		"""Installment 1 paid late on 2025-03-05 with a late payment penalty of 500."""
		return make_repayment(
			drawdown, "2025-03-05", allocation_mode="Selected Row", installment_no=1, penalty=500
		)

	def test_late_payment_paid_to_lender(self):
		s = self.islamic_loan("Paid to Lender")
		repayment = self.late_repayment(s.drawdown)
		# the charity account is the default penalty account of an Islamic facility
		self.assertEqual(repayment.penalty_account, self.islamic.charity)
		self.assertEqual((repayment.penalty_treatment, repayment.total_paid), ("Paid to Lender", 11_604.66))
		self.assertIsNone(repayment.charity_journal_entry)
		assert_gl_entries(
			self,
			"Payment Entry",
			repayment.payment_entry,
			[
				(self.islamic.murabaha, 10_000, 0),
				(self.islamic.profit, 1104.66, 0),
				(self.islamic.charity, 500, 0),
				(s.bank_gl, 0, 11_604.66),
			],
		)

	def test_late_payment_payable_to_charity(self):
		s = self.islamic_loan("Payable to Charity")
		repayment = self.late_repayment(s.drawdown)
		self.assertEqual(
			(repayment.penalty_treatment, repayment.total_paid), ("Payable to Charity", 11_104.66)
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			repayment.payment_entry,
			[
				(self.islamic.murabaha, 10_000, 0),
				(self.islamic.profit, 1104.66, 0),
				(s.bank_gl, 0, 11_104.66),
			],
		)
		self.assertTrue(repayment.charity_journal_entry)
		assert_gl_entries(
			self,
			"Journal Entry",
			repayment.charity_journal_entry,
			[(self.islamic.charity, 500, 0), (self.islamic.charity_payable, 0, 500)],
		)
		self.assertEqual(
			frappe.db.get_value("Journal Entry", repayment.charity_journal_entry, "treasury_event"),
			accounting.EVENT_CHARITY,
		)
		repayment.cancel()
		for doctype, name in (
			("Journal Entry", repayment.charity_journal_entry),
			("Payment Entry", repayment.payment_entry),
		):
			self.assertEqual(frappe.db.get_value(doctype, name, "docstatus"), 2)

	def test_penalty_only_payable_to_charity(self):
		"""Review 7g M1: the penalty charged after the installment was paid, payable to charity: nothing goes
		through the bank (no repayment voucher), only the charity Journal Entry."""
		for setting in (FACILITY_PE, FACILITY_JE):
			with self.subTest(setting=setting):
				self.use_company_settings(accrue_facility_interest=0, **setting)
				s = self.islamic_loan("Payable to Charity")
				first = make_repayment(
					s.drawdown, "2025-03-05", allocation_mode="Selected Row", installment_no=1
				)
				penalty = make_repayment(
					s.drawdown,
					"2025-03-10",
					allocation_mode="Selected Row",
					installment_no=1,
					principal_paid=0,
					interest_paid=0,
					penalty=500,
				)
				penalty.reload()
				self.assertEqual((penalty.docstatus, penalty.total_paid, penalty.allocations), (1, 0, []))
				self.assertEqual((penalty.payment_entry, penalty.journal_entry), (None, None))
				assert_gl_entries(
					self,
					"Journal Entry",
					penalty.charity_journal_entry,
					[(self.islamic.charity, 500, 0), (self.islamic.charity_payable, 0, 500)],
				)
				row = schedule_row(s.drawdown.name, 1)
				self.assertEqual((row.status, row.repayment), ("Paid", first.name))
				penalty.cancel()
				self.assertEqual(
					frappe.db.get_value("Journal Entry", penalty.charity_journal_entry, "docstatus"), 2
				)
				row = schedule_row(s.drawdown.name, 1)
				self.assertEqual((row.status, row.repayment), ("Paid", first.name))

	def test_utilisation_and_accrued_from_pe(self):
		results = {}
		for setting in (FACILITY_PE, FACILITY_JE):
			with self.subTest(setting=setting):
				self.use_company_settings(accrue_facility_interest=1, **setting)
				s = self.loan()
				interest.accrue_drawdown(s.drawdown.name, "2025-03-01")
				before = (
					get_utilisation(s.facility.name).utilised,
					interest.accrued_balance(s.drawdown, "2025-03-01"),
				)
				make_repayment(s.drawdown, "2025-02-28")
				s.drawdown.reload()
				after = (
					get_utilisation(s.facility.name).utilised,
					interest.accrued_balance(s.drawdown, "2025-03-01"),
				)
				results[setting["facility_voucher"]] = (before, after)
		self.assertEqual(results["Payment Entry"], ((120_000, 1104.66), (110_000, 0)))
		self.assertEqual(results["Payment Entry"], results["Journal Entry"])

	def test_create_deductions_payment_direct(self):
		s = self.loan()
		values = dict(
			source_doctype="Facility Drawdown",
			source_name=s.drawdown.name,
			event="test_deductions_payment",
			payment_type="Pay",
			company=COMPANY,
			posting_date="2025-02-01",
			party_type="Supplier",
			party=SUPPLIER,
			bank_account_head=s.bank_gl,
			deductions=[{"account": self.accounts.fees, "amount": 100}],
			reference_no="DEDUCTIONS",
			reference_date="2025-02-01",
		)
		name = posting.create_deductions_payment(**values)
		self.assertEqual(posting.create_deductions_payment(**values), name)
		assert_gl_entries(self, "Payment Entry", name, [(self.accounts.fees, 100, 0), (s.bank_gl, 0, 100)])
		self.assertEqual(ple_count(name), 0)
		cases = (
			("company currency", {"deductions": [{"account": self.accounts.loan_usd, "amount": 100}]}),
			("Pay or Receive", {"payment_type": "Internal Transfer"}),
			("greater than zero", {"deductions": [{"account": self.accounts.fees, "amount": 0}]}),
			("greater than zero", {"payment_type": "Receive"}),
		)
		for index, (message, change) in enumerate(cases):
			with self.subTest(message=message, change=change):
				arguments = {**values, **change, "event": f"test_deductions_{index}"}
				with self.assertRaises(frappe.ValidationError) as ctx:
					posting.create_deductions_payment(**arguments)
				self.assertIn(message, str(ctx.exception))

	def test_cancel_pe_repayment_restores_schedule(self):
		s = self.loan()
		repayment = make_repayment(
			s.drawdown, "2025-02-28", principal_paid=40_000, interest_paid=1104.66, prepayment=1
		)
		self.assertEqual(repayment.prepayment_amount, 30_000)
		self.assertEqual(get_utilisation(s.facility.name).utilised, 80_000)
		repayment.cancel()
		self.assertEqual(frappe.db.get_value("Payment Entry", repayment.payment_entry, "docstatus"), 2)
		s.drawdown.reload()
		rows = s.drawdown.repayment_schedule
		self.assertEqual(len(rows), 12)
		self.assertEqual({r.principal for r in rows}, {10_000})
		self.assertEqual({(r.paid_principal, r.paid_interest) for r in rows}, {(0, 0)})
		self.assertEqual({(r.payment_voucher_type, r.payment_journal) for r in rows}, {(None, None)})
		self.assertEqual(get_utilisation(s.facility.name).utilised, 120_000)

	def test_cancel_with_snapshot_before_1_1(self):
		"""Review 7g m1: a snapshot taken before 1.1 has no `payment_voucher_type`; the restored rows paid
		earlier get "Journal Entry" (the only voucher type then), not an empty type under a voucher."""
		self.use_company_settings(**FACILITY_JE)
		s = self.loan()
		first = make_repayment(s.drawdown, "2025-02-28")
		second = make_repayment(s.drawdown, "2025-03-31")
		saved = json.loads(frappe.db.get_value("Facility Repayment", second.name, "schedule_snapshot"))
		for row in saved:
			row.pop("payment_voucher_type", None)
		frappe.db.set_value("Facility Repayment", second.name, "schedule_snapshot", json.dumps(saved))
		second.reload()
		second.cancel()
		row = schedule_row(s.drawdown.name, 1)
		self.assertEqual(
			(row.payment_voucher_type, row.payment_journal, row.repayment),
			("Journal Entry", first.journal_entry, first.name),
		)
		row = schedule_row(s.drawdown.name, 2)
		self.assertEqual((row.payment_voucher_type, row.payment_journal, row.repayment), (None, None, None))

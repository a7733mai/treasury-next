"""Phase 7g (spec §7 C): Auto Debit drawdowns — the daily job on the due date, the bank statement match in PDC
Bank Clearing, the low balance guard and the job's isolation.

Every test uses its own bank account (`make_own_bank`) and its own facility: the job runs over every Auto
Debit drawdown of the company, so the assertions only look at the test's own drawdown. `frappe.enqueue` is
mocked (the alert evaluation the job triggers is recorded, not queued).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.balance_alerts.guard import LowBalanceError
from treasury.bank_facilities import auto_debit
from treasury.tests.utils import (
	COMPANY,
	FACILITY_PE,
	TreasuryTestMixin,
	ensure_alert_bank,
	ensure_facility_accounts,
	ensure_mode,
	ensure_user,
	get_pe,
	make_bank_facility,
	make_bank_transaction,
	make_drawdown,
	make_own_bank,
	make_repayment,
	set_bank_suppliers,
	set_mode_map,
	set_thresholds,
)

D = getdate
TOOL = "PDC Bank Clearing"
CLEARING_USER = "treasury-7g-autodebit-user@example.com"


class AutoDebitTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		ensure_user(CLEARING_USER, ["Treasury User", "Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(
			accrue_facility_interest=0, auto_debit_posting="On Due Date", on_low_balance="Warn", **FACILITY_PE
		)
		set_bank_suppliers(self, [])
		enqueue = patch("frappe.enqueue")
		self.enqueue = enqueue.start()
		self.addCleanup(enqueue.stop)

	def loan(self, bank=None, **values):
		bank = bank or make_own_bank()
		facility = make_bank_facility(bank_account=bank.bank_account, **values.pop("facility_values", {}))
		drawdown = make_drawdown(
			facility, bank_account=bank.bank_account, repayment_mode="Auto Debit", **values
		)
		return frappe._dict(bank=bank, facility=facility, drawdown=drawdown)

	@staticmethod
	def repayments(drawdown: str) -> list[frappe._dict]:
		return frappe.get_all(
			"Facility Repayment",
			filters={"drawdown": drawdown, "docstatus": 1},
			fields=["name", "repayment_date", "settlement", "payment_entry", "bank_transaction", "total_paid"],
			order_by="repayment_date asc",
		)

	def tool(self, bank_account: str, tolerance: int = 3):
		tool = frappe.get_doc(TOOL)
		tool.company = COMPANY
		tool.bank_account = bank_account
		tool.from_date = D("2025-01-01")
		tool.to_date = D("2025-12-31")
		tool.date_tolerance = tolerance
		tool.set("matches", [])
		return tool

	@staticmethod
	def installment_rows(tool, drawdown: str) -> list:
		return [row for row in tool.matches if row.facility_drawdown == drawdown]

	@staticmethod
	def select_only(tool, rows: list):
		"""Select these rows only (e.g. not a keyword proposal of a new bank charge)."""
		names = {id(row) for row in rows}
		for row in tool.matches:
			row.selected = 1 if id(row) in names else 0


class TestAutoDebit(AutoDebitTestCase):
	def test_due_date_posting(self):
		s = self.loan()
		mode = ensure_mode("_Test Treasury Auto Debit", account=s.bank.bank_gl)
		row = {"bank_account": s.bank.bank_account, "method": "Auto Debit", "mode_of_payment": mode}
		set_mode_map(self, [row])
		result = auto_debit.post_due_auto_debits(as_of="2025-03-01")
		rows = self.repayments(s.drawdown.name)
		self.assertEqual(len(rows), 1)
		self.assertIn(rows[0].name, result["posted"])
		self.assertEqual((rows[0].repayment_date, rows[0].settlement), (D("2025-02-28"), "Auto Debit"))
		pe = get_pe(rows[0].payment_entry)
		self.assertEqual((pe.posting_date, pe.clearance_date), (D("2025-02-28"), D("2025-02-28")))
		self.assertEqual(pe.mode_of_payment, mode)
		self.assertEqual(flt(pe.paid_amount), 11_104.66)
		# the alert evaluation of the company is triggered
		self.assertTrue(
			any(c.args and c.args[0].endswith("evaluate_company") for c in self.enqueue.call_args_list)
		)
		# a second run posts nothing
		result = auto_debit.post_due_auto_debits(as_of="2025-03-01")
		self.assertEqual(len(self.repayments(s.drawdown.name)), 1)
		self.assertNotIn(rows[0].name, result["posted"])

	def test_catch_up_uses_due_dates(self):
		s = self.loan()
		auto_debit.post_due_auto_debits(as_of="2025-04-02")
		self.assertEqual(
			[r.repayment_date for r in self.repayments(s.drawdown.name)], [D("2025-02-28"), D("2025-03-31")]
		)

	def test_statement_match_mode(self):
		self.use_company_settings(auto_debit_posting="On Bank Statement Match")
		s = self.loan()
		auto_debit.post_due_auto_debits(as_of="2025-04-02")
		self.assertEqual(self.repayments(s.drawdown.name), [])

		weak = make_bank_transaction(
			s.bank.bank_account, "2025-03-02", withdrawal=11_104.66, description="LOAN DEBIT"
		)
		make_bank_transaction(s.bank.bank_account, "2025-03-02", withdrawal=999, description="OTHER DEBIT")
		tool = self.tool(s.bank.bank_account)
		tool.get_suggestions()
		rows = self.installment_rows(tool, s.drawdown.name)
		self.assertEqual(
			[(r.bank_transaction, r.match_quality, r.installment_no, r.reference_doctype) for r in rows],
			[(weak.name, "Weak", 1, "Facility Drawdown")],
		)
		self.assertEqual(rows[0].cheque_no, "#1")

		strong = make_bank_transaction(
			s.bank.bank_account,
			"2025-03-02",
			withdrawal=11_104.66,
			description=f"INSTALLMENT {s.drawdown.name}",
		)
		tool.get_suggestions()
		rows = self.installment_rows(tool, s.drawdown.name)
		self.assertEqual([(r.bank_transaction, r.match_quality) for r in rows], [(strong.name, "Strong")])
		self.select_only(tool, rows)
		result = tool.clear_selected()
		self.assertEqual(len(result["installments"]), 1)

		repayments = self.repayments(s.drawdown.name)
		self.assertEqual(len(repayments), 1)
		repayment = repayments[0]
		self.assertEqual(
			(repayment.repayment_date, repayment.bank_transaction, repayment.settlement),
			(D("2025-03-02"), strong.name, "Auto Debit"),
		)
		pe = get_pe(repayment.payment_entry)
		self.assertEqual((pe.posting_date, pe.clearance_date), (D("2025-03-02"), D("2025-03-02")))
		strong.reload()
		self.assertEqual(strong.status, "Reconciled")
		self.assertEqual(
			[(p.payment_document, p.payment_entry) for p in strong.payment_entries],
			[("Payment Entry", pe.name)],
		)

	def test_reconcile_posted_auto_debit(self):
		s = self.loan()
		auto_debit.post_due_auto_debits(as_of="2025-03-01")
		posted = self.repayments(s.drawdown.name)[0]
		bt = make_bank_transaction(s.bank.bank_account, "2025-02-28", withdrawal=11_104.66, description="DEBIT")
		tool = self.tool(s.bank.bank_account)
		tool.get_suggestions()
		rows = self.installment_rows(tool, s.drawdown.name)
		self.assertEqual(
			[(r.bank_transaction, r.facility_repayment, r.reference_doctype) for r in rows],
			[(bt.name, posted.name, "Facility Repayment")],
		)
		self.select_only(tool, rows)
		result = tool.clear_selected()
		self.assertEqual(result["skipped"], [])
		self.assertEqual(result["installments"], [])
		self.assertEqual(len(self.repayments(s.drawdown.name)), 1)  # reconciled only
		self.assertEqual(frappe.db.get_value("Facility Repayment", posted.name, "bank_transaction"), bt.name)
		bt.reload()
		self.assertEqual(bt.status, "Reconciled")

	def test_guard_not_blocking_scheduler(self):
		self.use_company_settings(on_low_balance="Block")
		bank = ensure_alert_bank()
		s = self.loan(bank=bank)
		set_thresholds(
			self,
			[{"bank_account": bank.bank_account, "warning_balance": 10_000_000, "minimum_balance": 10_000_000}],
		)
		self.enqueue.reset_mock()
		auto_debit.post_due_auto_debits(as_of="2025-03-01")
		self.assertEqual(len(self.repayments(s.drawdown.name)), 1)
		self.assertTrue(
			any(c.args and c.args[0].endswith("evaluate_company") for c in self.enqueue.call_args_list)
		)
		# a repayment entered by a user in the same state is blocked (the penalty lowers the projected balance)
		self.assertRaises(
			LowBalanceError,
			make_repayment,
			s.drawdown,
			"2025-03-31",
			penalty=100,
			penalty_account=self.accounts.penalty,
		)

	def test_failure_isolated_and_logged(self):
		good = self.loan()
		broken = self.loan()
		account = frappe.db.get_value("Bank Facility", broken.facility.name, "interest_expense_account")
		frappe.db.set_value("Bank Facility", broken.facility.name, "interest_expense_account", None)
		self.addCleanup(
			frappe.db.set_value, "Bank Facility", broken.facility.name, "interest_expense_account", account
		)
		error = {"reference_doctype": "Facility Drawdown", "reference_name": broken.drawdown.name}
		frappe.db.delete("Error Log", error)
		for _run in range(2):
			result = auto_debit.post_due_auto_debits(as_of="2025-03-01")
			self.assertIn(broken.drawdown.name, result["failed"])
		self.assertEqual(len(self.repayments(good.drawdown.name)), 1)
		self.assertEqual(self.repayments(broken.drawdown.name), [])
		self.assertEqual(frappe.db.count("Error Log", error), 1)

	def test_clearing_permission(self):
		self.use_company_settings(auto_debit_posting="On Bank Statement Match")
		s = self.loan()
		make_bank_transaction(
			s.bank.bank_account, "2025-02-28", withdrawal=11_104.66, description=f"LOAN {s.drawdown.name}"
		)
		with self.set_user(CLEARING_USER):
			tool = self.tool(s.bank.bank_account)
			tool.get_suggestions()
			rows = self.installment_rows(tool, s.drawdown.name)
			self.assertEqual(len(rows), 1)
			self.select_only(tool, rows)
			result = tool.clear_selected()
		self.assertEqual(len(result["skipped"]), 1)
		self.assertIn("not permitted to submit Facility Repayments", result["skipped"][0]["reason"])
		self.assertEqual(self.repayments(s.drawdown.name), [])

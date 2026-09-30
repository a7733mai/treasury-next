"""Phase 7b: PDC Bank Clearing matches Bank Transactions with Sent Bank Transfers too (spec 7b §3.8, §7 F)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, today

from treasury.tests.utils import (
	BANK,
	COMPANY,
	DEFAULT_PDC_SETTINGS,
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	add_user_permission,
	ensure_second_egp_bank,
	ensure_user,
	make_bank_transaction,
	make_bank_transfer,
	make_pdc_cheque,
	set_fee_rules,
	standard_fee_rules,
)

TOOL = "PDC Bank Clearing"
NO_TRANSFER_USER = "treasury-clearing-no-transfer@example.com"


class TestBankTransferClearing(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# a Treasury role; restricted to single transfers by a User Permission (see the test)
		ensure_user(NO_TRANSFER_USER, ["Treasury User", "Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())
		self.today = getdate(today())

	def day(self, offset: int):
		return getdate(add_days(self.today, offset))

	def tool(self, bank_account=None, tolerance=3):
		tool = frappe.get_doc(TOOL)
		tool.company = COMPANY
		tool.bank_account = bank_account or self.data.egp.bank_account
		tool.from_date = self.day(-30)
		tool.to_date = self.today
		tool.date_tolerance = tolerance
		tool.set("matches", [])
		return tool

	@staticmethod
	def row_for(tool, transfer):
		return next((row for row in tool.matches if row.bank_transfer == transfer.name), None)

	@staticmethod
	def select_only(tool, *names):
		for row in tool.matches:
			row.selected = 1 if (row.bank_transfer or row.cheque) in names else 0

	def assert_reconciled(self, bt, payment_entry):
		bt.reload()
		self.assertEqual(bt.status, "Reconciled")
		self.assertEqual(flt(bt.unallocated_amount), 0)
		self.assertEqual(
			[(p.payment_document, p.payment_entry) for p in bt.payment_entries],
			[("Payment Entry", payment_entry)],
		)

	# ------------------------------------------------------------------ source side

	def test_payment_matched_with_its_bank_reference(self):
		egp = self.data.egp
		transfer = make_bank_transfer(amount=20000, execution_date=self.day(-5), bank_reference="FT778899")
		bt = make_bank_transaction(
			egp.bank_account, self.day(-4), withdrawal=20030, description="TRF FT778899 SUPPLIER"
		)
		# the principal alone (a statement without the fee) is not suggested
		principal = make_bank_transaction(egp.bank_account, self.day(-4), withdrawal=20000)

		tool = self.tool()
		tool.get_suggestions()
		row = self.row_for(tool, transfer)
		self.assertEqual(
			(row.bank_transaction, row.match_quality, row.selected, row.reference_doctype),
			(bt.name, "Strong", 1, "Bank Transfer"),
		)
		self.assertEqual(
			(row.cheque_no, row.direction, getdate(row.due_date)), ("FT778899", "Outgoing", self.day(-5))
		)
		self.assertNotIn(principal.name, [r.bank_transaction for r in tool.matches if r.bank_transfer])

		self.select_only(tool, transfer.name)
		result = tool.clear_selected()
		self.assertEqual(result["cleared"], [transfer.name])
		transfer.reload()
		self.assertEqual((transfer.status, transfer.accounting_status), ("Completed", "Posted"))
		self.assertEqual(getdate(transfer.bank_transaction_date), self.day(-4))
		self.assert_reconciled(bt, transfer.payment_entry)
		self.assertEqual(
			getdate(frappe.db.get_value("Payment Entry", transfer.payment_entry, "clearance_date")),
			self.day(-4),
		)

	def test_receipt_matched(self):
		egp = self.data.egp
		receipt = make_bank_transfer(transfer_type="Party Receipt", amount=20000, execution_date=self.day(-3))
		bt = make_bank_transaction(egp.bank_account, self.day(-2), deposit=19995)
		tool = self.tool()
		tool.get_suggestions()
		row = self.row_for(tool, receipt)
		self.assertEqual(
			(row.bank_transaction, row.match_quality, row.direction), (bt.name, "Weak", "Incoming")
		)
		self.select_only(tool, receipt.name)
		self.assertEqual(tool.clear_selected()["cleared"], [receipt.name])
		self.assertEqual(frappe.db.get_value("Bank Transfer", receipt.name, "status"), "Completed")
		self.assert_reconciled(bt, receipt.payment_entry)

	# ------------------------------------------------------------------ internal transfer

	def test_internal_source_then_target(self):
		egp, second = self.data.egp, ensure_second_egp_bank()
		transfer = make_bank_transfer(
			transfer_type="Internal Transfer",
			amount=50000,
			to_bank_account=second.bank_account,
			execution_date=self.day(-6),
		)
		source_bt = make_bank_transaction(
			egp.bank_account, self.day(-5), withdrawal=50000, description=transfer.name
		)
		target_bt = make_bank_transaction(second.bank_account, self.day(-4), deposit=50000)

		tool = self.tool()
		tool.get_suggestions()
		self.assertEqual(self.row_for(tool, transfer).match_quality, "Strong")
		self.select_only(tool, transfer.name)
		self.assertEqual(tool.clear_selected()["cleared"], [transfer.name])
		transfer.reload()
		self.assertEqual(transfer.status, "Completed")
		self.assert_reconciled(source_bt, transfer.payment_entry)
		clearance = frappe.db.get_value("Payment Entry", transfer.payment_entry, "clearance_date")
		self.assertEqual(getdate(clearance), self.day(-5))

		target_tool = self.tool(bank_account=second.bank_account)
		target_tool.get_suggestions()
		row = self.row_for(target_tool, transfer)
		self.assertEqual((row.bank_transaction, row.direction), (target_bt.name, "Incoming"))
		self.select_only(target_tool, transfer.name)
		self.assertEqual(target_tool.clear_selected()["cleared"], [transfer.name])
		transfer.reload()
		self.assertEqual(transfer.status, "Completed")
		self.assertEqual(transfer.status_log[-1].event, "Target Confirmed")
		self.assert_reconciled(target_bt, transfer.payment_entry)
		clearance = frappe.db.get_value("Payment Entry", transfer.payment_entry, "clearance_date")
		self.assertEqual(getdate(clearance), self.day(-5))

		# confirmed: never suggested again
		make_bank_transaction(second.bank_account, self.day(-3), deposit=50000)
		target_tool.get_suggestions()
		self.assertIsNone(self.row_for(target_tool, transfer))

	# ------------------------------------------------------------------ mixed list, permissions

	def test_each_transaction_used_once_with_cheques(self):
		egp = self.data.egp
		cheque = make_pdc_cheque(amount=1250, issue_date=self.day(-20), due_date=self.day(-4))
		cheque.deliver(handover_date=self.day(-19))
		# Same Bank transfer: no fee, the statement amount is the amount
		transfer = make_bank_transfer(amount=1250, execution_date=self.day(-5), beneficiary_bank=BANK)
		self.assertEqual(transfer.bank_amount, 1250)
		bt = make_bank_transaction(egp.bank_account, self.day(-4), withdrawal=1250)

		tool = self.tool()
		tool.get_suggestions()
		rows = [r for r in tool.matches if r.bank_transaction == bt.name]
		self.assertEqual(len(rows), 1)
		transactions = [r.bank_transaction for r in tool.matches]
		self.assertEqual(len(transactions), len(set(transactions)))

	def test_row_skipped_without_transfer_permission(self):
		egp = self.data.egp
		transfer = make_bank_transfer(amount=20000, execution_date=self.day(-5), bank_reference="FT445566")
		make_bank_transaction(egp.bank_account, self.day(-4), withdrawal=20030, reference_number="FT445566")
		tool = self.tool()
		tool.get_suggestions()
		self.select_only(tool, transfer.name)
		# the user may read and write Bank Transfers of another transfer only (User Permission)
		other = make_bank_transfer(amount=100, execution_date=self.day(-5))
		add_user_permission(NO_TRANSFER_USER, "Bank Transfer", other.name)

		with self.set_user(NO_TRANSFER_USER):
			result = tool.clear_selected()
		self.assertEqual(result["cleared"], [])
		self.assertEqual([d["bank_transfer"] for d in result["skipped"]], [transfer.name])
		self.assertEqual(frappe.db.get_value("Bank Transfer", transfer.name, "status"), "Sent")

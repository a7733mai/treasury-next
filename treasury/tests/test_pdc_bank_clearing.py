"""PDC Bank Clearing tool and clearing an incoming cheque received without a bank (PLAN.md §4)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.pdc.doctype.pdc_bank_clearing.pdc_bank_clearing import cheque_no_in
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	add_user_permission,
	assert_gl_entries,
	day,
	ensure_company,
	ensure_user,
	get_voucher_count,
	make_pdc_cheque,
)

DOCTYPE = "PDC Cheque"
TOOL = "PDC Bank Clearing"


def make_bank_transaction(bank_account: str, date, deposit=0, withdrawal=0, **values):
	"""A submitted Bank Transaction in the currency of the bank account."""
	gl_account = frappe.db.get_value("Bank Account", bank_account, "account")
	currency = frappe.get_cached_value("Account", gl_account, "account_currency")
	doc = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": getdate(date),
			"bank_account": bank_account,
			"currency": currency,
			"deposit": deposit,
			"withdrawal": withdrawal,
			**values,
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


class TestPDCBankClearing(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	# ------------------------------------------------------------------ helpers

	def tool(self, from_date=-120, to_date=0, tolerance=3, bank_account=None):
		tool = frappe.get_doc(TOOL)
		tool.company = COMPANY
		tool.bank_account = bank_account or self.data.egp.bank_account
		tool.from_date = day(from_date)
		tool.to_date = day(to_date)
		tool.date_tolerance = tolerance
		tool.set("matches", [])
		return tool

	@staticmethod
	def row_for(tool, cheque):
		return next((row for row in tool.matches if row.cheque == cheque.name), None)

	@staticmethod
	def select_only(tool, *cheques):
		names = {c.name for c in cheques}
		for row in tool.matches:
			row.selected = 1 if row.cheque in names else 0

	def delivered(self, amount, due=-30):
		cheque = make_pdc_cheque(amount=amount, issue_date=day(due - 30), due_date=day(due))
		cheque.deliver(handover_date=day(due - 29))
		cheque.reload()
		return cheque

	def assert_reconciled(self, bt, journal):
		bt.reload()
		self.assertEqual(bt.status, "Reconciled")
		self.assertEqual(flt(bt.unallocated_amount), 0)
		self.assertEqual(
			[(p.payment_document, p.payment_entry) for p in bt.payment_entries], [("Journal Entry", journal)]
		)

	# ------------------------------------------------------------------ suggestions

	def test_cheque_no_token_match(self):
		self.assertTrue(cheque_no_in("12345", "CHQ 12345 PAID"))
		self.assertTrue(cheque_no_in("12345", "chq#12345"))
		self.assertFalse(cheque_no_in("12345", "CHQ 912345 PAID"))
		self.assertFalse(cheque_no_in("12345", None))

	def test_suggestion_quality(self):
		strong = self.delivered(1301.25)
		weak = self.delivered(1302.25)
		too_far = self.delivered(1303.25)
		bt_strong = make_bank_transaction(
			self.data.egp.bank_account, day(-10), withdrawal=1301.25, description=f"CHQ {strong.cheque_no}"
		)
		bt_weak = make_bank_transaction(self.data.egp.bank_account, day(-28), withdrawal=1302.25)
		make_bank_transaction(self.data.egp.bank_account, day(-15), withdrawal=1303.25)  # 15 days off
		# a deposit of the same amount never matches an outgoing cheque
		make_bank_transaction(self.data.egp.bank_account, day(-30), deposit=1301.25)

		tool = self.tool()
		tool.get_suggestions()

		row = self.row_for(tool, strong)
		self.assertEqual(
			(row.bank_transaction, row.match_quality, row.selected), (bt_strong.name, "Strong", 1)
		)
		self.assertEqual(flt(row.amount), 1301.25)
		self.assertEqual(getdate(row.transaction_date), day(-10))
		row = self.row_for(tool, weak)
		self.assertEqual((row.bank_transaction, row.match_quality, row.selected), (bt_weak.name, "Weak", 0))
		self.assertIsNone(self.row_for(tool, too_far))
		transactions = [r.bank_transaction for r in tool.matches]
		self.assertEqual(len(transactions), len(set(transactions)))

	def test_transaction_before_the_handover_is_never_suggested(self):
		cheque = self.delivered(1311.25)  # handed over on day(-59)
		# the cheque number matches, but the bank cannot have paid a cheque it was not yet given
		make_bank_transaction(
			self.data.egp.bank_account, day(-65), withdrawal=1311.25, reference_number=cheque.cheque_no
		)

		tool = self.tool()
		tool.get_suggestions()

		self.assertIsNone(self.row_for(tool, cheque))

	def test_user_restricted_to_another_company_gets_no_suggestions(self):
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		cheque = self.delivered(1321.25)
		make_bank_transaction(
			self.data.egp.bank_account, day(-28), withdrawal=1321.25, reference_number=cheque.cheque_no
		)
		user = ensure_user("treasury-clearing-other-co@example.com", ["Treasury User", "Accounts User"])
		add_user_permission(user, "Company", OTHER_COMPANY)

		tool = self.tool()
		with self.set_user(user):
			self.assertRaises(frappe.PermissionError, tool.get_suggestions)
			# nor through the other company with this company's bank account
			tool.company = OTHER_COMPANY
			self.assertRaises(frappe.PermissionError, tool.get_suggestions)
		self.assertFalse(tool.matches)

	def test_user_restricted_to_the_company_gets_suggestions(self):
		cheque = self.delivered(1331.25)
		make_bank_transaction(
			self.data.egp.bank_account, day(-28), withdrawal=1331.25, reference_number=cheque.cheque_no
		)
		user = ensure_user("treasury-clearing-own-co@example.com", ["Treasury User", "Accounts User"])
		add_user_permission(user, "Company", COMPANY)

		tool = self.tool()
		with self.set_user(user):
			tool.get_suggestions()
		self.assertEqual(self.row_for(tool, cheque).match_quality, "Strong")

	# ------------------------------------------------------------------ clearing

	def test_clear_selected_outgoing_reconciles(self):
		egp = self.data.egp
		cheque = self.delivered(1404.5)
		bt = make_bank_transaction(
			egp.bank_account, day(-28), withdrawal=1404.5, reference_number=cheque.cheque_no
		)

		tool = self.tool()
		tool.get_suggestions()
		self.select_only(tool, cheque)
		result = tool.clear_selected()

		self.assertEqual(result["cleared"], [cheque.name])
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Cleared", "Bank Posted"))
		self.assertEqual(getdate(cheque.bank_transaction_date), day(-28))
		self.assertEqual(getdate(cheque.actual_clearance_date), day(-28))
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.pdc_payable, 1404.5, 0, None, day(-28)), (egp.bank_gl, 0, 1404.5, None, day(-28))],
		)
		self.assert_reconciled(bt, cheque.bank_journal)
		self.assertEqual(
			getdate(frappe.db.get_value("Journal Entry", cheque.bank_journal, "clearance_date")), day(-28)
		)

		# the same row again: skipped, nothing new
		vouchers = get_voucher_count(DOCTYPE, cheque.name)
		row = self.row_for(tool, cheque)
		row.selected = 1
		result = tool.clear_selected()
		self.assertEqual(result["cleared"], [])
		self.assertEqual([d["cheque"] for d in result["skipped"]], [cheque.name])
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), vouchers)

	def test_clear_selected_incoming_on_bank_transaction_date_mode(self):
		self.use_company_settings(incoming_bank_posting="On Bank Transaction Date")
		egp = self.data.egp
		cheque = make_pdc_cheque("Incoming", amount=1505.75, issue_date=day(-60), due_date=day(-20))
		cheque.receive(receipt_date=day(-59))
		cheque.deposit(deposit_date=day(-21))
		bt = make_bank_transaction(egp.bank_account, day(-18), deposit=1505.75)

		tool = self.tool()
		tool.get_suggestions()
		row = self.row_for(tool, cheque)
		self.assertEqual(row.match_quality, "Weak")
		self.select_only(tool, cheque)
		tool.clear_selected()

		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Cleared", "Bank Posted"))
		self.assertEqual(getdate(cheque.bank_transaction_date), day(-18))
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.bank_gl, 1505.75, 0, None, day(-18)), (egp.under_collection, 0, 1505.75, None, day(-18))],
		)
		self.assert_reconciled(bt, cheque.bank_journal)

	def test_cleared_in_manual_mode_is_posted_and_reconciled(self):
		self.use_company_settings(outgoing_bank_posting="Manual")
		egp = self.data.egp
		cheque = self.delivered(1606.5)
		cheque.clear(clearance_date=day(-29))
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Cleared", "Party Posted"))
		bt = make_bank_transaction(
			egp.bank_account, day(-27), withdrawal=1606.5, reference_number=cheque.cheque_no
		)

		tool = self.tool()
		tool.get_suggestions()
		self.assertEqual(self.row_for(tool, cheque).match_quality, "Strong")
		self.select_only(tool, cheque)
		tool.clear_selected()

		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Bank Posted")
		self.assertEqual(getdate(cheque.bank_transaction_date), day(-27))
		self.assertEqual(getdate(cheque.bank_posting_date), day(-27))
		self.assert_reconciled(bt, cheque.bank_journal)

	def test_cleared_with_another_bank_transaction_date_is_skipped(self):
		self.use_company_settings(outgoing_bank_posting="Manual")
		egp = self.data.egp
		cheque = self.delivered(1616.5)
		cheque.clear(clearance_date=day(-29), bank_transaction_date=day(-29))
		bt = make_bank_transaction(
			egp.bank_account, day(-27), withdrawal=1616.5, reference_number=cheque.cheque_no
		)

		tool = self.tool()
		tool.get_suggestions()
		self.select_only(tool, cheque)
		result = tool.clear_selected()

		# posting it would date the bank JE / cheque on day(-29) but reconcile it on day(-27)
		self.assertEqual(result["cleared"], [])
		self.assertEqual([d["cheque"] for d in result["skipped"]], [cheque.name])
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Cleared", "Party Posted"))
		self.assertEqual(getdate(cheque.bank_transaction_date), day(-29))
		bt.reload()
		self.assertEqual(flt(bt.unallocated_amount), 1616.5)

	def test_rows_of_another_bank_or_currency_are_skipped(self):
		usd = self.data.usd
		cheque = self.delivered(1707.5)
		usd_bt = make_bank_transaction(usd.bank_account, day(-28), withdrawal=1707.5)
		good = self.delivered(1708.5)
		good_bt = make_bank_transaction(
			self.data.egp.bank_account, day(-28), withdrawal=1708.5, reference_number=good.cheque_no
		)

		tool = self.tool()
		tool.get_suggestions()
		self.assertIsNone(self.row_for(tool, cheque))  # the USD transaction is never suggested
		self.select_only(tool, good)
		# a tampered row pairing the EGP cheque with the USD bank transaction
		tool.append(
			"matches",
			{"selected": 1, "bank_transaction": usd_bt.name, "cheque": cheque.name, "amount": 1707.5},
		)
		result = tool.clear_selected()

		self.assertEqual(result["cleared"], [good.name])
		self.assertEqual([d["cheque"] for d in result["skipped"]], [cheque.name])
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Delivered", "Party Posted"))
		usd_bt.reload()
		self.assertEqual(flt(usd_bt.unallocated_amount), 1707.5)
		self.assert_reconciled(good_bt, frappe.db.get_value(DOCTYPE, good.name, "bank_journal"))

	def test_failing_row_does_not_stop_the_others(self):
		early = self.delivered(1808.5)  # handed over on day(-59)
		early_bt = make_bank_transaction(self.data.egp.bank_account, day(-70), withdrawal=1808.5)
		good = self.delivered(1809.5)
		make_bank_transaction(
			self.data.egp.bank_account, day(-28), withdrawal=1809.5, reference_number=good.cheque_no
		)

		tool = self.tool()
		tool.get_suggestions()
		self.select_only(tool, good)
		# a bank transaction dated before the handover cannot clear the cheque (lifecycle date order)
		tool.append("matches", {"selected": 1, "bank_transaction": early_bt.name, "cheque": early.name})
		result = tool.clear_selected()

		self.assertEqual(result["cleared"], [good.name])
		self.assertEqual([d["cheque"] for d in result["skipped"]], [early.name])
		early.reload()
		self.assertEqual(early.operational_status, "Delivered")
		self.assertEqual(get_voucher_count(DOCTYPE, early.name), 1)  # the party Payment Entry only

	def test_requires_treasury_role(self):
		cheque = self.delivered(1909.5)
		make_bank_transaction(
			self.data.egp.bank_account, day(-28), withdrawal=1909.5, reference_number=cheque.cheque_no
		)
		tool = self.tool()
		tool.get_suggestions()
		self.select_only(tool, cheque)

		frappe.set_user(ensure_user("treasury-clearing-noperm@example.com", ["Accounts User"]))
		try:
			self.assertRaises(frappe.PermissionError, tool.clear_selected)
		finally:
			frappe.set_user("Administrator")
		cheque.reload()
		self.assertEqual(cheque.operational_status, "Delivered")

	def test_cheque_the_user_may_not_write_is_skipped(self):
		allowed = self.delivered(1919.5)
		denied = self.delivered(1920.5)
		for cheque in (allowed, denied):
			make_bank_transaction(
				self.data.egp.bank_account,
				day(-28),
				withdrawal=cheque.amount,
				reference_number=cheque.cheque_no,
			)
		tool = self.tool()
		tool.get_suggestions()
		self.select_only(tool, allowed, denied)
		# Treasury role, but restricted to one cheque by a User Permission
		user = ensure_user("treasury-clearing-one-cheque@example.com", ["Treasury User", "Accounts User"])
		add_user_permission(user, DOCTYPE, allowed.name)

		with self.set_user(user):
			result = tool.clear_selected()

		self.assertEqual(result["cleared"], [allowed.name])
		self.assertEqual([d["cheque"] for d in result["skipped"]], [denied.name])
		denied.reload()
		self.assertEqual((denied.operational_status, denied.accounting_status), ("Delivered", "Party Posted"))
		self.assertEqual(get_voucher_count(DOCTYPE, denied.name), 1)
		allowed.reload()
		self.assertEqual(allowed.accounting_status, "Bank Posted")
		# the vouchers record the user who cleared the cheque, although they were saved as Administrator
		self.assertEqual(frappe.db.get_value("Journal Entry", allowed.bank_journal, "owner"), user)
		self.assertEqual(allowed.status_log[-1].user, user)


class TestClearWithBankAccount(TreasuryTestMixin, IntegrationTestCase):
	"""`clear` of an incoming cheque received without a bank account."""

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		self.use_company_settings(incoming_party_posting="Manual")

	def received_without_bank(self, amount=900):
		cheque = make_pdc_cheque(
			"Incoming", amount=amount, bank_account=None, issue_date=day(-40), due_date=day(-15)
		)
		cheque.receive(receipt_date=day(-39))
		cheque.reload()
		self.assertFalse(cheque.bank_account)
		return cheque

	def test_clear_sets_the_bank_account(self):
		egp = self.data.egp
		cheque = self.received_without_bank()
		self.assertRaises(frappe.ValidationError, cheque.clear, day(-10))  # a bank is required

		cheque.clear(clearance_date=day(-10), bank_account=egp.bank_account)
		cheque.reload()
		self.assertEqual(cheque.bank_account, egp.bank_account)
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Cleared", "Bank Posted"))
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(egp.pdc_receivable, 900, 0, None, day(-10)),
				(self.data.receivable, 0, 900, CUSTOMER, day(-10)),
			],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.bank_gl, 900, 0, None, day(-10)), (egp.pdc_receivable, 0, 900, None, day(-10))],
		)

	def test_clear_into_a_bank_in_another_currency_is_rejected(self):
		cheque = self.received_without_bank()
		self.assertRaises(
			frappe.ValidationError, cheque.clear, day(-10), bank_account=self.data.usd.bank_account
		)
		cheque.reload()
		self.assertFalse(cheque.bank_account)
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Received", "Not Posted"))
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 0)

	def test_cheque_with_a_bank_keeps_it(self):
		cheque = make_pdc_cheque("Incoming", amount=950, issue_date=day(-40), due_date=day(-15))
		cheque.receive(receipt_date=day(-39))
		other = frappe.db.get_value(
			"Bank Account", {"company": COMPANY, "name": ["!=", cheque.bank_account], "is_company_account": 1}
		)
		if other:
			self.assertRaises(frappe.ValidationError, cheque.clear, day(-10), bank_account=other)
		# passing its own bank account is harmless
		cheque.clear(clearance_date=day(-10), bank_account=cheque.bank_account)
		cheque.reload()
		self.assertEqual(cheque.operational_status, "Cleared")

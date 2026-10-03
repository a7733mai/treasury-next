"""CB-14: a Cash account (cash box) never goes negative on a voucher's posting date.

The block is validation only: a voucher that passes posts exactly what it did before.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, today

from treasury.accounting import cash_balance
from treasury.tests.utils import COMPANY, TreasuryTestMixin, ensure_account, ensure_fiscal_year

BOX = "Treasury CB14 Box"
OTHER_BOX = "Treasury CB14 Other Box"
INCOME = "Treasury CB14 Income"
EXPENSE = "Treasury CB14 Expense"
SETTING = "block_negative_cash_balance"


class TestCashBalance(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.box = ensure_account(BOX, "Cash In Hand", "Cash")
		cls.other_box = ensure_account(OTHER_BOX, "Cash In Hand", "Cash")
		cls.income = ensure_account(INCOME, "Indirect Income")
		cls.expense = ensure_account(EXPENSE, "Indirect Expenses")
		ensure_fiscal_year(COMPANY, today())
		ensure_fiscal_year(COMPANY, add_days(today(), -1))

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ helpers

	def journal(self, rows: list[tuple[str, float, float]], date=None, submit=True, **values):
		doc = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": COMPANY,
				"posting_date": date or today(),
				"accounts": [
					{
						"account": account,
						"debit_in_account_currency": debit,
						"credit_in_account_currency": credit,
					}
					for account, debit, credit in rows
				],
				**values,
			}
		).insert(ignore_permissions=True)
		if submit:
			doc.submit()
		return doc

	def fund(self, amount: float, date=None, account: str | None = None):
		return self.journal([(account or self.box, amount, 0), (self.income, 0, amount)], date=date)

	def spend(self, amount: float, date=None, submit=True):
		return self.journal([(self.expense, amount, 0), (self.box, 0, amount)], date=date, submit=submit)

	def transfer(self, amount: float, source: str, target: str, date=None, submit=True):
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Internal Transfer",
				"company": COMPANY,
				"posting_date": date or today(),
				"paid_from": source,
				"paid_to": target,
				"paid_from_account_currency": "EGP",
				"paid_to_account_currency": "EGP",
				"paid_amount": amount,
				"received_amount": amount,
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
			}
		).insert(ignore_permissions=True)
		if submit:
			pe.submit()
		return pe

	def balance(self, account=None, date=None) -> float:
		return cash_balance.get_balance(account or self.box, date or today())

	def assert_blocked(self, fn, *args, **kwargs):
		with self.assertRaises(frappe.ValidationError) as cm:
			fn(*args, **kwargs)
		self.assertIn("would be negative", str(cm.exception))
		return cm.exception

	# ------------------------------------------------------------------ tests

	def test_default_is_on(self):
		self.assertEqual(frappe.db.get_value("Treasury Company Settings", COMPANY, SETTING), 1)
		self.assertTrue(cash_balance.is_enabled(COMPANY))
		# a company without a settings document is protected too
		self.assertTrue(cash_balance.is_enabled("A Company Without Treasury Settings"))

	def test_journal_entry_blocked_when_balance_too_low(self):
		self.fund(1000)
		self.assert_blocked(self.spend, 1000.01)
		self.assertEqual(self.balance(), 1000)

		spent = self.spend(1000)
		self.assertEqual(spent.docstatus, 1)
		self.assertEqual(self.balance(), 0)
		# the next credit on the empty box is refused
		self.assert_blocked(self.spend, 1)

	def test_posting_date_balance(self):
		"""The balance counts up to the posting date: money funded today cannot pay a voucher of yesterday."""
		self.fund(500)
		yesterday = add_days(today(), -1)
		self.assert_blocked(self.spend, 100, date=yesterday)
		self.fund(300, date=yesterday)
		self.assertEqual(self.spend(300, date=yesterday).docstatus, 1)
		self.assert_blocked(self.spend, 1, date=yesterday)

	def test_payment_entry_internal_transfer(self):
		self.fund(1000)
		self.assert_blocked(self.transfer, 1500, self.box, self.other_box)
		pe = self.transfer(400, self.box, self.other_box)
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual((self.balance(self.box), self.balance(self.other_box)), (600, 400))
		# the receiving box is debited, never blocked
		self.assertEqual(self.transfer(400, self.other_box, self.box).docstatus, 1)

	def test_debit_and_net_credit(self):
		"""Only the net credit on the account counts: a row debiting and crediting the box nets out."""
		self.fund(100)
		doc = self.journal([(self.box, 50, 0), (self.box, 0, 150), (self.expense, 100, 0)], submit=False)
		self.assertEqual(cash_balance.cash_credits(doc), {self.box: 100})
		doc.submit()
		self.assertEqual(self.balance(), 0)

	def test_setting_off_allows_negative(self):
		self.use_company_settings(**{SETTING: 0})
		self.assertFalse(cash_balance.is_enabled(COMPANY))
		doc = self.spend(250)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(self.balance(), -250)

	def test_setting_on_again_blocks(self):
		self.use_company_settings(**{SETTING: 0})
		self.spend(10)
		frappe.db.set_value("Treasury Company Settings", COMPANY, SETTING, 1)
		# already negative: any further credit stays refused, a debit (funding) is fine
		self.assert_blocked(self.spend, 1)
		self.assertEqual(self.fund(100).docstatus, 1)

	def test_bank_accounts_are_not_cash_boxes(self):
		bank_gl = self.data.egp.pdc_payable
		self.assertNotEqual(frappe.get_cached_value("Account", bank_gl, "account_type"), "Cash")
		doc = self.journal([(self.expense, 5, 0), (bank_gl, 0, 5)], submit=False)
		self.assertEqual(cash_balance.cash_credits(doc), {})

	def test_credits_of_a_pay_payment_entry(self):
		"""Pay / Internal Transfer take money out of `paid_from`; Receive takes nothing out."""
		pay = frappe._dict(
			doctype="Payment Entry",
			payment_type="Pay",
			paid_from=self.box,
			paid_to=self.expense,
			base_paid_amount=75,
			base_received_amount=75,
		)
		self.assertEqual(cash_balance.cash_credits(pay), {self.box: 75})
		receive = frappe._dict(
			doctype="Payment Entry",
			payment_type="Receive",
			paid_from=self.income,
			paid_to=self.box,
			base_paid_amount=75,
			base_received_amount=75,
		)
		self.assertEqual(cash_balance.cash_credits(receive), {})

	def test_opening_entry_exempt(self):
		doc = frappe._dict(
			doctype="Journal Entry",
			company=COMPANY,
			is_opening="Yes",
			posting_date=today(),
			accounts=[frappe._dict(account=self.box, debit=0, credit=30)],
		)
		self.assertEqual(cash_balance.cash_credits(doc), {self.box: 30})
		# no balance on the box, yet the opening entry passes
		self.assertEqual(flt(self.balance()), 0)
		self.assertIsNone(cash_balance.before_submit(doc))
		doc.is_opening = "No"
		self.assert_blocked(cash_balance.before_submit, doc)

	def test_message_is_translatable(self):
		error = self.assert_blocked(self.spend, 10)
		self.assertIn(self.box, str(error))

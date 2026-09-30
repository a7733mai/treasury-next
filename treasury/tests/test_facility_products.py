"""Phase 7g (spec §7 E): Istisna'a stages, Ijarah disbursed to another account, the Trust Receipt created by a
Letter of Credit settlement, ERPNext's Invoice Discounting under a facility, and a finance company end to end.

Fixed dates in 2025; every test creates its own facility (and bank account where balances matter).
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.accounting import posting
from treasury.bank_facilities import accounting, lc, trust_receipt
from treasury.bank_facilities.report.cost_of_financing.cost_of_financing import execute as cost_of_financing
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc import lifecycle
from treasury.pdc.scheduler import post_due_party_postings
from treasury.security_cheques import api as security_api
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	FACILITY_PE,
	LENDER,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	acc,
	assert_gl_entries,
	ensure_account,
	ensure_alert_bank,
	ensure_facility_accounts,
	ensure_instrument_accounts,
	ensure_islamic_accounts,
	ensure_lender,
	get_account_balance,
	get_voucher_count,
	make_bank_facility,
	make_drawdown,
	make_finance_facility,
	make_installment_batch,
	make_letter_of_credit,
	make_own_bank,
	make_repayment,
	make_sales_invoice,
	set_bank_suppliers,
	unique_cheque_no,
)

D = getdate
DRAWDOWN = "Facility Drawdown"


class ProductTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.islamic = ensure_islamic_accounts()
		cls.inst = ensure_instrument_accounts()
		ensure_lender()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0, **PE_SETTINGS, **FACILITY_PE)
		set_bank_suppliers(self, [])

	def assert_error(self, message: str, fn, *args, **kwargs):
		with self.assertRaises(frappe.ValidationError) as ctx:
			fn(*args, **kwargs)
		self.assertIn(message, str(ctx.exception))


class TestIstisnaAndIjarah(ProductTestCase):
	def istisna_facility(self, bank):
		return make_bank_facility(
			bank_account=bank.bank_account,
			limits=[
				{
					"limit_type": "Istisna'a",
					"limit_amount": 500_000,
					"interest_rate": 10,
					"liability_account": self.islamic.istisna,
				}
			],
		)

	def test_istisna_stages(self):
		bank = make_own_bank()
		facility = self.istisna_facility(bank)
		stages = [
			{"stage_date": "2025-02-15", "amount": 100_000, "description": "Foundations"},
			{"stage_date": "2025-05-15", "amount": 200_000, "description": "Structure"},
		]
		drawdown = make_drawdown(
			facility,
			amount=300_000,
			limit_type="Istisna'a",
			bank_account=bank.bank_account,
			disburse_to="Other Account",
			disbursement_account=self.islamic.cwip,
			disbursement_stages=stages,
			upfront_fees=1000,
			fees_account=self.accounts.fees,
		)
		drawdown.reload()
		# submit: only the upfront fees (a Journal Entry); the whole amount is committed
		self.assertEqual((drawdown.journal_entry, drawdown.payment_entry), (None, None))
		assert_gl_entries(
			self,
			"Journal Entry",
			drawdown.fees_journal_entry,
			[(self.accounts.fees, 1000, 0), (bank.bank_gl, 0, 1000)],
		)
		self.assertEqual(get_utilisation(facility.name).utilised, 300_000)
		self.assertEqual({row.status for row in drawdown.disbursement_stages}, {"Pending"})

		# D2: stage 1 to Capital Work in Progress
		first = drawdown.disbursement_stages[0]
		result = drawdown.disburse_stage(stage=first.name, disbursement_date="2025-02-15")
		self.assertEqual(result["voucher_type"], "Journal Entry")
		date = D("2025-02-15")
		assert_gl_entries(
			self,
			"Journal Entry",
			result["voucher_no"],
			[(self.islamic.cwip, 100_000, 0, None, date), (self.islamic.istisna, 0, 100_000, None, date)],
		)
		self.assertEqual(
			posting.find_voucher("Journal Entry", DRAWDOWN, drawdown.name, accounting.EVENT_STAGE, 1),
			result["voucher_no"],
		)
		drawdown.reload()
		self.assertEqual(
			(drawdown.disbursement_stages[0].status, D(drawdown.disbursement_stages[0].disbursed_on)),
			("Disbursed", date),
		)
		self.assertEqual(drawdown.disbursed_amount, 100_000)
		self.assertEqual(get_utilisation(facility.name).utilised, 300_000)  # 100,000 GL + 200,000 pending
		self.assert_error("already disbursed", drawdown.disburse_stage, stage=first.name)

		# no principal beyond what was disbursed and not repaid
		self.assert_error(
			"exceeds the disbursed principal outstanding",
			make_repayment,
			drawdown,
			"2025-03-31",
			principal_paid=150_000,
			interest_paid=0,
			prepayment=1,
		)
		# the stages must add up to the amount
		self.assert_error(
			"must add up to the drawdown amount",
			make_drawdown,
			facility,
			amount=300_000,
			limit_type="Istisna'a",
			bank_account=bank.bank_account,
			disbursement_stages=[{"stage_date": "2025-02-15", "amount": 100_000}],
		)

		# a stage paid into the bank in Payment Entry mode: a Receive from the lender; a rate sent by the client
		# is ignored for a company currency drawdown (review 7g M2: it would double the liability and the bank)
		to_bank = make_drawdown(
			facility,
			amount=100_000,
			limit_type="Istisna'a",
			bank_account=bank.bank_account,
			disbursement_stages=[{"stage_date": "2025-03-01", "amount": 100_000}],
		)
		result = to_bank.disburse_stage(
			stage=to_bank.disbursement_stages[0].name, disbursement_date="2025-03-01", exchange_rate=2
		)
		self.assertEqual(result["voucher_type"], "Payment Entry")
		self.assertEqual(
			frappe.db.get_value("Payment Entry", result["voucher_no"], "payment_type"), "Receive"
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			result["voucher_no"],
			[(bank.bank_gl, 100_000, 0), (self.islamic.istisna, 0, 100_000)],
		)

		# cancelling the drawdown cancels its stage vouchers
		to_bank.reload()
		to_bank.cancel()
		self.assertEqual(frappe.db.get_value("Payment Entry", result["voucher_no"], "docstatus"), 2)

	def test_ijarah_other_account(self):
		facility = make_finance_facility(
			limits=[
				{
					"limit_type": "Ijarah",
					"limit_amount": 500_000,
					"interest_rate": 8,
					"liability_account": self.islamic.ijarah,
				}
			]
		)
		values = dict(
			amount=240_000,
			limit_type="Ijarah",
			tenor_months=24,
			disburse_to="Other Account",
			ijarah_asset="Delivery trucks",
			ownership_transfer=1,
		)
		drawdown = make_drawdown(facility, disbursement_account=self.islamic.cwip, **values)
		drawdown.reload()
		self.assertIsNone(drawdown.payment_entry)
		assert_gl_entries(
			self,
			"Journal Entry",
			drawdown.journal_entry,
			[(self.islamic.cwip, 240_000, 0), (self.islamic.ijarah, 0, 240_000)],
		)
		self.assertEqual(get_utilisation(facility.name).utilised, 240_000)
		usd_account = ensure_account("Treasury CWIP USD", "Current Assets", currency="USD")
		for account, message in (
			(None, "Disbursement Account is required"),
			(frappe.get_cached_value("Company", COMPANY, "default_payable_account"), "not a party account"),
			(self.islamic.ijarah, "not a party account"),
			(usd_account, "not a party account"),
		):
			with self.subTest(account=account):
				self.assert_error(
					message, make_drawdown, facility, disbursement_account=account, submit=False, **values
				)
		# only Ijarah / Istisna'a are disbursed to another account
		loan = make_bank_facility()
		other = make_drawdown(loan, disburse_to="Other Account", disbursement_account=self.islamic.cwip)
		self.assertEqual((other.disburse_to, other.disbursement_account), ("Bank Account", None))


class TestTrustReceipt(ProductTestCase):
	def lc_facility(self, tr_limit=500_000):
		bank = make_own_bank()
		facility = make_bank_facility(
			bank_account=bank.bank_account,
			limits=[
				{
					"limit_type": "Letter of Credit",
					"limit_amount": 500_000,
					"cash_margin_percent": 10,
					"commission_rate": 0,
				},
				{
					"limit_type": "Trust Receipt",
					"limit_amount": tr_limit,
					"interest_rate": 11,
					"default_tenor_months": 3,
					"liability_account": self.islamic.trust_receipt,
				},
			],
		)
		return facility, bank

	def credit(self, facility, bank):
		credit = make_letter_of_credit(facility, amount=100_000, bank_account=bank.bank_account)
		credit.receive_documents(documents_date="2025-03-01")
		credit.reload()
		return credit

	def test_trust_receipt_from_lc(self):
		facility, bank = self.lc_facility()
		credit = self.credit(facility, bank)
		self.assertEqual(credit.margin_held, 10_000)
		before = get_account_balance(bank.bank_gl, "2025-03-09")
		result = credit.settle(settlement_date="2025-03-10", finance_by_trust_receipt=1)
		credit.reload()
		date = D("2025-03-10")
		tr = frappe.get_doc(DRAWDOWN, result["trust_receipt_drawdown"])
		self.assertEqual(credit.trust_receipt_drawdown, tr.name)
		self.assertEqual(
			(tr.letter_of_credit, tr.limit_type, tr.amount), (credit.name, "Trust Receipt", 90_000)
		)
		self.assertEqual((tr.docstatus, tr.repayment_mode), (1, "Manual"))
		# T1: margin release, supplier Payment Entry, the Trust Receipt's Payment Entry; the bank's net is nil
		assert_gl_entries(
			self,
			"Journal Entry",
			posting.find_voucher("Journal Entry", "Letter of Credit", credit.name, lc.EVENT_MARGIN_RELEASE),
			[(bank.bank_gl, 10_000, 0, None, date), (self.inst.lc_margin, 0, 10_000, None, date)],
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			credit.payment_entry,
			[(self.data.payable, 100_000, 0, SUPPLIER, date), (bank.bank_gl, 0, 100_000, None, date)],
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			tr.payment_entry,
			[(bank.bank_gl, 90_000, 0, None, date), (self.islamic.trust_receipt, 0, 90_000, None, date)],
		)
		for doctype, name in (("Payment Entry", credit.payment_entry), ("Payment Entry", tr.payment_entry)):
			self.assertEqual(frappe.db.get_value(doctype, name, "clearance_date"), date)
		self.assertEqual(flt(get_account_balance(bank.bank_gl, "2025-03-10") - before, 2), 0)
		# one bullet installment after the tenor: 90,000 + 90,000 x 11 % x 92 / 365
		self.assertEqual(
			[(D(r.due_date), r.principal, r.interest) for r in tr.repayment_schedule],
			[(D("2025-06-10"), 90_000, 2495.34)],
		)
		utilisation = {row.limit_type: row.utilised for row in get_utilisation(facility.name).limits}
		self.assertEqual((utilisation["Letter of Credit"], utilisation["Trust Receipt"]), (0, 90_000))
		self.assertEqual(credit.status_log[-1].event, "Trust Receipt Drawdown")

		# a settled credit is never cancelled; its cancellation would cancel the Trust Receipt first
		self.assertRaises(frappe.ValidationError, credit.cancel)
		credit.reload()
		repayment = make_repayment(tr.name, "2025-04-01", principal_paid=1000, interest_paid=0)
		self.assert_error("Cancel the repayments of Trust Receipt", lc.cancel_trust_receipt, credit)
		tr.reload()
		self.assert_error("has submitted repayments", tr.cancel)
		repayment.cancel()
		# the user cancels the Trust Receipt itself (e.g. wrong terms): the settled credit's links to it and to
		# its voucher (its field, its status log) do not block it; the credit stays settled and logs it
		tr.reload()
		tr.cancel()
		self.assertEqual(frappe.db.get_value(DRAWDOWN, tr.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Payment Entry", tr.payment_entry, "docstatus"), 2)
		credit.reload()
		self.assertEqual((credit.docstatus, credit.status), (1, "Settled"))
		self.assertEqual(credit.trust_receipt_drawdown, tr.name)
		self.assertEqual(trust_receipt.drawdown_summary(tr.name).repayment_status, "Cancelled")
		self.assertEqual(
			(credit.status_log[-1].event, credit.status_log[-1].remarks), ("Trust Receipt Cancelled", tr.name)
		)
		utilisation = {row.limit_type: row.utilised for row in get_utilisation(facility.name).limits}
		self.assertEqual((utilisation["Letter of Credit"], utilisation["Trust Receipt"]), (0, 0))
		self.assertEqual(flt(get_account_balance(bank.bank_gl, "2025-03-10") - before, 2), -90_000)

	def test_trust_receipt_refusals_roll_back(self):
		# the Trust Receipt above its available limit: the whole settlement is rolled back
		facility, bank = self.lc_facility(tr_limit=50_000)
		credit = self.credit(facility, bank)
		self.assert_error(
			"exceeds the available",
			credit.settle,
			settlement_date="2025-03-10",
			finance_by_trust_receipt=1,
		)
		credit.reload()
		self.assertEqual((credit.status, credit.payment_entry), ("Documents Received", None))
		self.assertIsNone(
			posting.find_voucher("Journal Entry", "Letter of Credit", credit.name, lc.EVENT_MARGIN_RELEASE)
		)
		self.assertFalse(frappe.db.exists(DRAWDOWN, {"letter_of_credit": credit.name}))
		# the margin covers the amount settled: nothing to finance
		self.assert_error(
			"nothing to finance",
			credit.settle,
			settlement_date="2025-03-10",
			amount=10_000,
			finance_by_trust_receipt=1,
		)
		# no Trust Receipt sub-limit
		plain = make_bank_facility(
			limits=[{"limit_type": "Letter of Credit", "limit_amount": 500_000, "cash_margin_percent": 10}]
		)
		other = make_letter_of_credit(plain, amount=50_000)
		other.receive_documents(documents_date="2025-03-01")
		self.assert_error(
			"has no Trust Receipt sub-limit",
			other.settle,
			settlement_date="2025-03-10",
			finance_by_trust_receipt=1,
		)


class TestInvoiceDiscounting(ProductTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.receivables = frappe._dict(
			credit=ensure_account("Treasury AR Credit", "Accounts Receivable", "Receivable"),
			discounted=ensure_account("Treasury AR Discounted", "Accounts Receivable", "Receivable"),
			unpaid=ensure_account("Treasury AR Unpaid", "Accounts Receivable", "Receivable"),
		)

	def discounting(self, facility, amount: float, submit: bool = True):
		invoice = make_sales_invoice(amount, posting_date="2025-03-01")
		doc = frappe.get_doc(
			{
				"doctype": "Invoice Discounting",
				"company": COMPANY,
				"posting_date": D("2025-03-01"),
				"loan_start_date": D("2025-03-01"),
				"loan_period": 90,
				"treasury_facility": facility.name,
				"short_term_loan": self.accounts.loan_egp,
				"bank_account": self.data.egp.bank_gl,
				"bank_charges_account": acc("Treasury Bank Charges"),
				"accounts_receivable_credit": self.receivables.credit,
				"accounts_receivable_discounted": self.receivables.discounted,
				"accounts_receivable_unpaid": self.receivables.unpaid,
				"invoices": [
					{
						"sales_invoice": invoice.name,
						"customer": CUSTOMER,
						"posting_date": invoice.posting_date,
						"outstanding_amount": amount,
						"debit_to": invoice.debit_to,
					}
				],
			}
		).insert(ignore_permissions=True)
		if submit:
			doc.submit()
		return doc

	def test_invoice_discounting_utilisation(self):
		facility = make_bank_facility(
			limits=[{"limit_type": "Invoice Discounting", "limit_amount": 50_000}],
		)

		def used() -> float:
			rows = get_utilisation(facility.name, "Invoice Discounting").limits
			return rows[0].utilised

		first = self.discounting(facility, 30_000)
		self.assertEqual(first.status, "Sanctioned")
		self.assertEqual(used(), 30_000)
		self.assertEqual(
			get_voucher_count("Invoice Discounting", first.name), 0
		)  # ERPNext posts, not Treasury
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "total_utilised"), 30_000)
		# over the limit
		second = self.discounting(facility, 25_000, submit=False)
		self.assert_error("exceeds the available", second.submit)
		# cancelling frees it (a user's cancel: ERPNext's own ledger rows do not block it, before_cancel)
		first.reload()
		first.cancel()
		self.assertEqual(frappe.db.get_value("Invoice Discounting", first.name, "docstatus"), 2)
		self.assertEqual(used(), 0)
		second.reload()
		second.submit()
		self.assertEqual(used(), 25_000)
		# ERPNext's loan Journal Entries move the status; the stored utilisation follows (Settled: freed)
		disbursement = second.create_disbursement_entry()
		disbursement.posting_date = D("2025-03-02")
		disbursement.submit()
		self.assertEqual(frappe.db.get_value("Invoice Discounting", second.name, "status"), "Disbursed")
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "total_utilised"), 25_000)
		second.reload()
		closing = second.close_loan()
		closing.posting_date = D("2025-06-01")
		closing.submit()
		self.assertEqual(frappe.db.get_value("Invoice Discounting", second.name, "status"), "Settled")
		self.assertEqual(used(), 0)
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "total_utilised"), 0)
		# a facility without the sub-limit
		plain = make_bank_facility()
		self.assert_error("has no Invoice Discounting sub-limit", self.discounting, plain, 1000, submit=False)


class TestFinanceCompanyEndToEnd(ProductTestCase):
	def test_finance_company_end_to_end(self):
		bank = ensure_alert_bank()
		facility = make_finance_facility(bank_account=bank.bank_account, expiry_date="2030-12-31")
		# the collateral cheque to the lender
		data = security_api.make_security_cheque("Bank Facility", facility.name)
		values = {k: v for k, v in data.items() if not k.startswith("__") and k != "name"}
		values.update(cheque_no=unique_cheque_no(), cheque_book=None, bank_account=bank.bank_account)
		security = frappe.get_doc(values).insert()
		self.assertEqual((security.party_type, security.party), ("Supplier", LENDER))
		# Murabaha 120,000 with a fixed profit of 7,200 (600 per installment), repaid by cheques to the lender
		drawdown = make_drawdown(
			facility,
			limit_type="Murabaha",
			profit_amount=7200,
			interest_rate=0,
			bank_account=bank.bank_account,
			repayment_mode="PDC to Lender",
		)
		batch = make_installment_batch(drawdown, submit=False)
		batch.set("cheques", [r for r in batch.cheques if r.installment_no in (1, 2)])
		batch.save()
		batch.submit()
		self.assertEqual((batch.party_type, batch.party), ("Supplier", LENDER))
		post_due_party_postings(as_of="2025-03-31")
		for cheque in frappe.get_all("PDC Cheque", filters={"cheque_batch": batch.name}, pluck="name"):
			doc = frappe.get_doc("PDC Cheque", cheque)
			self.assertEqual(doc.accounting_status, lifecycle.PARTY_POSTED)
			doc.clear(clearance_date=frappe.utils.add_days(doc.effective_due_date, 3))
		self.assertEqual(get_utilisation(facility.name).utilised, 100_000)
		_columns, data, *_rest = cost_of_financing(
			{
				"company": COMPANY,
				"from_date": "2025-01-01",
				"to_date": "2025-12-31",
				"facility": facility.name,
			}
		)
		self.assertEqual((data[0].profit, data[0].interest, data[0].total), (1200, 0, 1200))
		self.assertEqual(data[0].lender_name, frappe.db.get_value("Supplier", LENDER, "supplier_name"))

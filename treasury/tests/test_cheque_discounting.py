"""Cheque Discounting (Phase 5b, PLAN.md §8 and §4 "Discounted"): discount GL with day-count charges, cheques
Discounted and out of the forecast inflows, settlement at maturity, bounce with recourse under both
`on_bounce` settings, cancel, utilisation, limits, validations and the Discounted Cheques report.

Fixed dates in 2025. Every test creates its own facility on its own bank account (`make_own_bank`) and its own
incoming cheques on the standard EGP bank (PDC Receivable EGP), received on 1 Mar 2025 (party posted on
receipt). Scenario cheques: A 50,000 due 30 Jun, B 30,000 due 31 May; discounted on 31 Mar at 12% p.a.
(Actual/365) with a 0.5% commission:
  A: 50,000 x 12% x 91 / 365 = 1,495.89 ; B: 30,000 x 12% x 61 / 365 = 601.64 ; commission 400.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate

from treasury.accounting import posting
from treasury.bank_facilities import dashboard
from treasury.bank_facilities.report.discounted_cheques.discounted_cheques import (
	execute as discounted_report,
)
from treasury.bank_facilities.report.facility_utilisation.facility_utilisation import (
	execute as utilisation_report,
)
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc import lifecycle
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import BankPositionForecast
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	TreasuryTestMixin,
	assert_gl_entries,
	bank_account_name,
	ensure_facility_accounts,
	ensure_instrument_accounts,
	get_account_balance,
	get_voucher_count,
	make_bank_facility,
	make_cheque_discounting,
	make_own_bank,
	make_pdc_cheque,
)

D = getdate
LIMIT = "Cheque Discounting"
CHEQUE = "PDC Cheque"


class DiscountingTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.inst = ensure_instrument_accounts()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0, **DEFAULT_PDC_SETTINGS)

	def facility(self, limit=200_000, total=1_000_000):
		bank = make_own_bank()
		facility = make_bank_facility(
			total_limit=total,
			limits=[
				{
					"limit_type": LIMIT,
					"limit_amount": limit,
					"interest_rate": 12,
					"commission_rate": 0.5,
					"liability_account": self.inst.discounted,
				}
			],
			bank_account=bank.bank_account,
		)
		return facility, bank

	def cheque(self, amount, due_date, **values):
		cheque = make_pdc_cheque(
			"Incoming", amount=amount, issue_date="2025-03-01", due_date=due_date, **values
		)
		cheque.receive(receipt_date="2025-03-01")
		cheque.reload()
		return cheque

	def scenario(self, **values):
		facility, bank = self.facility(**values)
		a = self.cheque(50_000, "2025-06-30")
		b = self.cheque(30_000, "2025-05-31")
		return frappe._dict(facility=facility, bank=bank, a=a, b=b)

	def used(self, facility, as_on=None) -> float:
		return get_utilisation(facility.name, LIMIT, as_on).limits[0].utilised

	def assert_cheque(self, cheque, operational, accounting):
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), (operational, accounting))

	def voucher(self, cheque, event):
		return posting.find_voucher("Journal Entry", CHEQUE, cheque.name, event, cheque.cycle or 1)


class TestChequeDiscounting(DiscountingTestCase):
	def test_discount_posts_net_proceeds_and_charges(self):
		s = self.scenario()
		batch = make_cheque_discounting(s.facility, [s.a, s.b])
		batch.reload()
		self.assertEqual([(r.days, r.discount_charges) for r in batch.items], [(91, 1495.89), (61, 601.64)])
		self.assertEqual(
			(batch.total_amount, batch.discount_charges, batch.commission),
			(80_000, 2097.53, 400),
		)
		self.assertEqual((batch.total_charges, batch.net_proceeds), (2497.53, 77502.47))
		self.assertEqual((batch.discount_rate, batch.day_count), (12, "Actual/365"))  # from the sub-limit
		self.assertEqual(batch.liability_account, self.inst.discounted)
		date = D("2025-03-31")
		assert_gl_entries(
			self,
			"Journal Entry",
			batch.journal_entry,
			[
				(s.bank.bank_gl, 77502.47, 0, None, date),
				(self.inst.discount_expense, 2497.53, 0, None, date),
				(self.inst.discounted, 0, 80_000, None, date),
			],
		)
		self.assertEqual((batch.status, batch.outstanding_amount), ("Discounted", 80_000))
		for cheque in (s.a, s.b):
			# still in PDC Receivable: only the operational status moves
			self.assert_cheque(cheque, "Discounted", "Party Posted")
			self.assertEqual((cheque.cheque_discounting, D(cheque.discount_date)), (batch.name, date))
			self.assertEqual(cheque.status_log[-1].voucher_no, batch.journal_entry)
		self.assertEqual(self.used(s.facility), 80_000)
		self.assertEqual(self.used(s.facility, as_on="2025-03-30"), 0)
		# a discounted cheque is out of the company's hands
		self.assertRaises(frappe.ValidationError, s.a.postpone, new_due_date="2025-07-31", reason="Asked")
		self.assertRaises(frappe.ValidationError, s.a.deposit, deposit_date="2025-04-01")
		self.assertRaises(frappe.ValidationError, s.a.clear, clearance_date="2025-04-01")

	def test_explicit_terms_actual_360(self):
		s = self.scenario()
		batch = make_cheque_discounting(
			s.facility, [s.a, s.b], discount_rate=10, day_count="Actual/360", commission=0
		)
		batch.reload()
		# A: 50,000 x 10% x 91 / 360 ; B: 30,000 x 10% x 61 / 360
		self.assertEqual([r.discount_charges for r in batch.items], [1263.89, 508.33])
		self.assertEqual((batch.total_charges, batch.net_proceeds), (1772.22, 78227.78))
		assert_gl_entries(
			self,
			"Journal Entry",
			batch.journal_entry,
			[
				(s.bank.bank_gl, 78227.78, 0),
				(self.inst.discount_expense, 1772.22, 0),
				(self.inst.discounted, 0, 80_000),
			],
		)

	def test_discounted_cheques_leave_the_forecast_inflows(self):
		s = self.scenario()
		today = D("2025-04-15")

		def inflows(cheque):
			with patch("treasury.pdc.report_utils.today", return_value=str(today)):
				report = BankPositionForecast(
					{
						"company": COMPANY,
						"bank_account": [bank_account_name("EGP")],
						"from_date": today,
						"to_date": add_days(today, 107),  # 31 Jul
						"periodicity": "Monthly",
						"currency_view": "Company Currency",
						"show_details": 1,
					}
				)
				_columns, data, _message, _chart, _summary = report.run()
			return [r.expected_in for r in data if r.get("indent") and r.get("reference_name") == cheque.name]

		self.assertEqual(inflows(s.a), [50_000])
		make_cheque_discounting(s.facility, [s.a])
		# the bank already paid the proceeds: the cheque is no expected inflow anymore
		self.assertEqual(inflows(s.a), [])
		self.assertEqual(inflows(s.b), [30_000])

	def test_settle_at_maturity(self):
		s = self.scenario()
		batch = make_cheque_discounting(s.facility, [s.a, s.b])
		pdc_receivable = self.data.egp.pdc_receivable
		bank_before = get_account_balance(s.bank.bank_gl, "2025-12-31")

		# none due by 30 May
		self.assertRaises(frappe.ValidationError, batch.settle_cheques, settlement_date="2025-05-30")
		batch.settle_cheques(settlement_date="2025-05-31")  # B only: due by then
		date = D("2025-05-31")
		self.assert_cheque(s.b, "Cleared", "Bank Posted")
		self.assert_cheque(s.a, "Discounted", "Party Posted")
		settlement = self.voucher(s.b, lifecycle.EVENT_DISCOUNT_SETTLEMENT)
		self.assertEqual((s.b.bank_journal, D(s.b.actual_clearance_date)), (settlement, date))
		assert_gl_entries(
			self,
			"Journal Entry",
			s.b.bank_journal,
			[(self.inst.discounted, 30_000, 0, None, date), (pdc_receivable, 0, 30_000, None, date)],
		)
		# the bank account does not move at maturity: the proceeds came at the discount
		self.assertEqual(get_account_balance(s.bank.bank_gl, "2025-12-31"), bank_before)
		batch.reload()
		self.assertEqual((batch.status, batch.outstanding_amount), ("Partially Settled", 50_000))
		row = next(r for r in batch.items if r.pdc_cheque == s.b.name)
		self.assertEqual((row.status, D(row.closed_date), row.closing_journal), ("Settled", date, settlement))
		self.assertEqual(self.used(s.facility), 50_000)
		self.assertEqual(self.used(s.facility, as_on="2025-05-30"), 80_000)

		# idempotency: settled once
		count = get_voucher_count(CHEQUE, s.b.name)
		self.assertRaises(frappe.ValidationError, s.b.settle_discounted, settlement_date="2025-06-01")
		self.assertRaises(
			frappe.ValidationError, batch.settle_cheques, cheques=[s.b.name], settlement_date="2025-06-01"
		)
		self.assertEqual(get_voucher_count(CHEQUE, s.b.name), count)

		# the other one from the cheque itself
		s.a.settle_discounted(settlement_date="2025-06-30")
		self.assert_cheque(s.a, "Cleared", "Bank Posted")
		batch.reload()
		self.assertEqual((batch.status, batch.outstanding_amount), ("Settled", 0))
		self.assertEqual(self.used(s.facility), 0)
		self.assertRaises(frappe.ValidationError, batch.cancel)  # cheques settled

	def test_bounce_of_a_discounted_cheque_reverse_by_journal_entry(self):
		s = self.scenario()
		batch = make_cheque_discounting(s.facility, [s.a, s.b])
		pdc_receivable = self.data.egp.pdc_receivable
		s.a.bounce(bounce_date="2025-06-30", reason="Insufficient funds")
		self.assert_cheque(s.a, "Bounced", "Reversed")
		date = D("2025-06-30")
		recourse = self.voucher(s.a, lifecycle.EVENT_DISCOUNT_RECOURSE)
		# the bank takes the amount back from the company
		assert_gl_entries(
			self,
			"Journal Entry",
			recourse,
			[(self.inst.discounted, 50_000, 0, None, date), (s.bank.bank_gl, 0, 50_000, None, date)],
		)
		self.assertEqual(frappe.db.get_value("Journal Entry", recourse, "clearance_date"), date)
		# and the customer owes the cheque again
		assert_gl_entries(
			self,
			"Journal Entry",
			s.a.reversal_journal,
			[(self.data.receivable, 50_000, 0, CUSTOMER, date), (pdc_receivable, 0, 50_000, None, date)],
		)
		self.assertEqual(
			[r.event for r in s.a.status_log[-4:]],
			["Bounced", "Discount Recourse Posted", "Voucher Reversed", "Reversed"],
		)
		batch.reload()
		row = next(r for r in batch.items if r.pdc_cheque == s.a.name)
		self.assertEqual((row.status, row.closing_journal), ("Bounced", recourse))
		self.assertEqual((batch.status, batch.outstanding_amount), ("Partially Settled", 30_000))
		self.assertEqual(self.used(s.facility), 30_000)

		# re-presented, the cheque is an ordinary received cheque again (no discounting link)
		s.a.represent(represent_date="2025-07-01", new_due_date="2025-07-31")
		s.a.reload()
		self.assertEqual(
			(s.a.operational_status, s.a.cheque_discounting, s.a.discount_date), ("Received", None, None)
		)

	def test_bounce_of_a_discounted_cheque_cancel_payment_entry(self):
		self.use_company_settings(on_bounce="Cancel Payment Entry")
		s = self.scenario()
		make_cheque_discounting(s.facility, [s.a])
		payment_entry = s.a.party_payment_entry
		s.a.bounce(bounce_date="2025-06-30", reason="Account closed")
		self.assert_cheque(s.a, "Bounced", "Reversed")
		self.assertEqual(frappe.db.get_value("Payment Entry", payment_entry, "docstatus"), 2)
		assert_gl_entries(
			self,
			"Journal Entry",
			self.voucher(s.a, lifecycle.EVENT_DISCOUNT_RECOURSE),
			[(self.inst.discounted, 50_000, 0), (s.bank.bank_gl, 0, 50_000)],
		)
		self.assertFalse(s.a.reversal_journal)
		self.assertEqual(self.used(s.facility), 0)

	def test_cancel_restores_the_cheques(self):
		s = self.scenario()
		bank_before = get_account_balance(s.bank.bank_gl, "2025-12-31")
		batch = make_cheque_discounting(s.facility, [s.a, s.b])
		batch.cancel()
		batch.reload()
		self.assertEqual((batch.status, batch.outstanding_amount), ("Cancelled", 0))
		self.assertEqual(frappe.db.get_value("Journal Entry", batch.journal_entry, "docstatus"), 2)
		self.assertEqual(get_account_balance(s.bank.bank_gl, "2025-12-31"), bank_before)
		for cheque in (s.a, s.b):
			self.assert_cheque(cheque, "Received", "Party Posted")
			self.assertEqual((cheque.cheque_discounting, cheque.discount_date), (None, None))
			self.assertEqual(cheque.status_log[-1].event, "Discount Cancelled")
		self.assertEqual(self.used(s.facility), 0)
		# discounted again in another batch
		again = make_cheque_discounting(s.facility, [s.a], discount_date="2025-04-15")
		self.assert_cheque(s.a, "Discounted", "Party Posted")
		self.assertEqual(s.a.cheque_discounting, again.name)

	def test_limit_exceeded(self):
		s = self.scenario(limit=60_000)
		self.assertRaises(frappe.ValidationError, make_cheque_discounting, s.facility, [s.a, s.b])
		self.assert_cheque(s.a, "Received", "Party Posted")
		make_cheque_discounting(s.facility, [s.a])
		self.assertEqual(self.used(s.facility), 50_000)

	def test_validations(self):
		s = self.scenario()
		deposited = self.cheque(10_000, "2025-06-30")
		deposited.deposit(deposit_date="2025-03-05")
		early = self.cheque(10_000, "2025-03-20")  # due before the discount date
		usd = self.cheque(100, "2025-06-30", currency="USD", exchange_rate=50)
		cases = [[deposited], [early], [usd], [s.a, s.a], []]
		for cheques in cases:
			with self.subTest(cheques=[c.name for c in cheques]):
				self.assertRaises(
					frappe.ValidationError, make_cheque_discounting, s.facility, cheques, submit=False
				)
		# not posted to the party yet (party posting on the cheque date): not in PDC Receivable
		self.use_company_settings(incoming_party_posting="On Cheque Date")
		unposted = self.cheque(10_000, "2025-06-30")
		self.assertEqual(unposted.accounting_status, "Not Posted")
		self.assertRaises(
			frappe.ValidationError, make_cheque_discounting, s.facility, [unposted], submit=False
		)
		# a sub-limit without liability account
		frappe.db.set_value(
			"Bank Facility Limit", {"parent": s.facility.name, "limit_type": LIMIT}, "liability_account", None
		)
		self.assertRaises(frappe.ValidationError, make_cheque_discounting, s.facility, [s.a], submit=False)

	def test_report_card_and_facility_utilisation(self):
		def card():
			return dashboard.get_instrument_card({"company": COMPANY, "scope": "discounted"})["value"]

		before = card()
		s = self.scenario()
		batch = make_cheque_discounting(s.facility, [s.a, s.b])
		batch.settle_cheques(settlement_date="2025-05-31")
		self.assertEqual(flt(card() - before, 2), 50_000)

		filters = {"company": COMPANY, "cheque_discounting": batch.name}
		with patch("treasury.pdc.report_utils.today", return_value="2025-06-01"):
			_columns, data, _m, _c, summary = discounted_report(filters)
		parent, *children = data
		self.assertEqual(
			(parent.amount, parent.outstanding, parent.discount_charges), (80_000, 50_000, 2097.53)
		)
		self.assertEqual(
			[(c.pdc_cheque, c.status_label, c.days_to_maturity) for c in children],
			[(s.a.name, "Discounted", 29), (s.b.name, "Settled", None)],
		)
		cards = {c["label"]: c["value"] for c in summary}
		self.assertEqual((cards["Discounted, Outstanding"], cards["Paid at Maturity"]), (50_000, 30_000))

		_columns, data, _m, _c, _s = utilisation_report({"company": COMPANY, "facility": s.facility.name})
		row = next(r for r in data if r.get("limit_type") == LIMIT)
		self.assertEqual((row.utilised, row.available), (50_000, 150_000))

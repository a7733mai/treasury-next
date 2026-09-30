"""Phase 7h: the Treasury Dashboard costs a bounded number of queries (spec 7h §7 C). With a warm cache, adding
rows (cheques, transfers, charges, security cheques, instruments, a drawdown, a bank) adds no query; the only
per-row cost is one `get_utilisation` per facility, capped by MAX_FACILITIES.

Every query branch is taken before the first measurement (one Sent transfer with its fee, one bank charge this
month, a facility with Letter of Guarantee / Credit sub-limits holding one guarantee and one credit: a facility's
first guarantee adds the fixed bank-funded-claims lookup of `lg.get_lg_utilisation`, one query per voucher
DocType whatever the number of guarantees), so the two measurements compare the same code paths with more
rows; each measurement follows a warm-up call (caches of the rows just added)."""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc import overview
from treasury.pdc.overview import get_overview
from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	COMPANY,
	USD_SUPPLIER,
	BalanceAlertTestMixin,
	bank_account_name,
	count_queries,
	ensure_alert_bank,
	fund_bank,
	make_alert_fixture_e1,
	make_bank_charge,
	make_bank_facility,
	make_bank_transfer,
	make_drawdown,
	make_letter_of_credit,
	make_letter_of_guarantee,
	make_own_bank,
	make_pdc_cheque,
	make_security_cheque,
	set_thresholds,
)


class TestTreasuryDashboardQueries(BalanceAlertTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		self.fx = make_alert_fixture_e1(self)
		self.bank = self.fx.bank_account
		# every query branch once (see the module docstring)
		frappe.get_doc("Bank Transfer", self.fx.tr1).send_now()
		make_bank_charge(amount=10, posting_date=self.today, bank_account=self.bank)
		instruments_bank = make_own_bank()
		self.instruments = make_bank_facility(
			total_limit=2_000_000,
			limits=[
				{"limit_type": "Letter of Guarantee", "limit_amount": 1_000_000},
				{"limit_type": "Letter of Credit", "limit_amount": 1_000_000},
			],
			bank_account=instruments_bank.bank_account,
			start_date=self.day(-30),
			expiry_date=self.day(365),
		)
		make_letter_of_guarantee(
			self.instruments, amount=500, issue_date=self.today, expiry_date=self.day(20)
		)
		make_letter_of_credit(self.instruments, amount=500, issue_date=self.today, expiry_date=self.day(20))

	def measure(self) -> int:
		get_overview(COMPANY, None, 30)  # warm-up
		_result, count = count_queries(get_overview, COMPANY, None, 30)
		return count

	def add_rows(self):
		for direction, amount in (("Outgoing", 1000), ("Incoming", 2000)):
			cheque = make_pdc_cheque(
				direction, amount=amount, issue_date=self.today, due_date=self.day(4), bank_account=self.bank
			)
			if direction == "Outgoing":
				cheque.deliver(handover_date=self.today)
			else:
				cheque.receive(receipt_date=self.today)
		usd = make_pdc_cheque(
			amount=10,
			party=USD_SUPPLIER,
			currency="USD",
			issue_date=self.today,
			due_date=self.day(6),
		)
		usd.deliver(handover_date=self.today)
		make_bank_transfer(amount=500, bank_account=self.bank)  # Sent today
		make_bank_transfer(amount=700, bank_account=self.bank, execution_date=self.day(8))  # Scheduled
		make_bank_charge(amount=20, posting_date=self.today, bank_account=self.bank)
		make_bank_charge(amount=30, posting_date=self.today, bank_account=self.bank)
		held = make_security_cheque("Incoming", amount=4000, issue_date=self.today)
		held.receive(receipt_date=self.today)
		make_security_cheque("Outgoing", amount=1500, issue_date=self.today).issue(handover_date=self.today)
		make_letter_of_guarantee(
			self.instruments, amount=1000, issue_date=self.today, expiry_date=self.day(10)
		)
		make_letter_of_credit(self.instruments, amount=1000, issue_date=self.today, expiry_date=self.day(10))
		make_drawdown(
			self.fx.facility,
			amount=5000,
			drawdown_date=self.today,
			tenor_months=1,
			frequency="Bullet",
			method="Bullet",
			first_repayment_date=self.day(12),
		)
		second = ensure_alert_bank()
		fund_bank(second, 1000, self.day(-1))
		set_thresholds(
			self,
			[
				{"bank_account": self.bank, "warning_balance": 50000, "minimum_balance": 20000},
				{"bank_account": second.bank_account, "warning_balance": 500, "minimum_balance": 100},
			],
		)

	def test_query_count_bounded(self):
		first = self.measure()
		self.add_rows()
		second = self.measure()
		self.assertEqual(second, first)
		self.assertLessEqual(first, overview.MAX_DASHBOARD_QUERIES)

	def test_per_facility_cost_is_constant(self):
		before = self.measure()
		facility = make_bank_facility(
			bank_account=bank_account_name("EGP"), start_date=self.day(-30), expiry_date=self.day(365)
		)
		get_utilisation(facility.name)  # warm
		_result, per_facility = count_queries(get_utilisation, facility.name)
		after = self.measure()
		self.assertGreaterEqual(after, before)
		self.assertLessEqual(after - before, per_facility)

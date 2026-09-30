"""Phase 7e: per-bank daily positions built on the Bank Position Forecast engine (spec 7e §3.1, §7 A).

Dates are relative to the real today (the engine's today); each test works on banks of its own
(`ensure_alert_bank`), so its GL balances and flows belong to it only.
"""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.balance_alerts.positions import evaluate
from treasury.pdc.bank_positions import get_bank_positions
from treasury.pdc.report.bank_position_forecast import bank_position_forecast
from treasury.tests.utils import (
	BANK,
	COMPANY,
	USD_RATE,
	USD_SUPPLIER,
	BalanceAlertTestMixin,
	ensure_alert_bank,
	fund_bank,
	make_alert_fixture_e1,
	make_pdc_cheque,
	mapping_row,
	set_thresholds,
)


def run_report(bank_account: str, from_date, to_date) -> tuple[list, list]:
	"""(data, summary) of the Daily, Bank Currency report of one bank, overdue included."""
	_columns, data, _message, _chart, summary = bank_position_forecast.execute(
		{
			"company": COMPANY,
			"bank_account": [bank_account],
			"from_date": from_date,
			"to_date": to_date,
			"periodicity": "Daily",
			"currency_view": "Bank Currency",
			"include_overdue": 1,
			"show_details": 1,
		}
	)
	return data, summary


def add_twin(bank) -> str:
	"""A second Bank Account on the GL account of `bank` (with its own PDC Accounts row). ERPNext 16 refuses
	a GL account used twice in the form; sites set up earlier (or by import) can have it, so the validation is
	skipped here."""
	twin = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": f"{bank.bank_account.split(' - ')[0]} Twin",
			"bank": BANK,
			"account": bank.account,
			"company": COMPANY,
			"is_company_account": 1,
		}
	)
	twin.flags.ignore_validate = True
	twin.insert(ignore_permissions=True)
	name = twin.name
	frappe.get_doc(
		{
			"doctype": "PDC Account Mapping",
			"name": frappe.generate_hash(length=10),
			"parenttype": "Treasury Company Settings",
			"parentfield": "pdc_accounts",
			"parent": COMPANY,
			"idx": 99,
			**mapping_row(frappe._dict(bank, bank_account=name)),
		}
	).db_insert()
	return name


class TestBankPositions(BalanceAlertTestMixin, IntegrationTestCase):
	def test_equals_report_daily(self):
		e1 = make_alert_fixture_e1(self)
		# an overdue outgoing cheque (due T-2, still Delivered) on the EGP bank
		overdue = make_pdc_cheque(
			amount=1500, issue_date=self.day(-10), due_date=self.day(-2), bank_account=e1.bank_account
		)
		overdue.deliver(handover_date=self.day(-9))
		usd = ensure_alert_bank("USD")
		fund_bank(usd, 1000, self.day(-3), rate=USD_RATE)
		usd_cheque = make_pdc_cheque(
			amount=200,
			party=USD_SUPPLIER,
			issue_date=self.today,
			due_date=self.day(2),
			bank_account=usd.bank_account,
		)
		usd_cheque.deliver(handover_date=self.today)

		positions = {
			p.bank_account: p for p in get_bank_positions(COMPANY, 7, [e1.bank_account, usd.bank_account])
		}
		self.assertEqual(set(positions), {e1.bank_account, usd.bank_account})
		for bank_account, position in positions.items():
			with self.subTest(bank=bank_account):
				data, summary = run_report(bank_account, self.today, self.day(6))
				rows = {r.row_id: r for r in data if not r.get("indent")}
				overdue_row = rows.get("overdue")
				for index, day in enumerate(position.days):
					row = rows[f"p{index}"]
					self.assertEqual(day.closing, row.closing, day.date)
					expected_in, expected_out = row.expected_in, row.expected_out
					if index == 0 and overdue_row:
						expected_in += overdue_row.expected_in
						expected_out += overdue_row.expected_out
					self.assertAlmostEqual(day.expected_in, expected_in, 2)
					self.assertAlmostEqual(day.expected_out, expected_out, 2)
					self.assertAlmostEqual(day.actual_in, row.actual_in, 2)
					self.assertAlmostEqual(day.actual_out, row.actual_out, 2)
				self.assertEqual(position.current_balance, summary[0]["value"])
		egp = positions[e1.bank_account]
		self.assertEqual((egp.overdue_out, egp.opening, egp.currency), (1500, 100000, "EGP"))
		self.assertIn(overdue.name, [f.reference_name for f in egp.days[0].flows])
		self.assertEqual(positions[usd.bank_account].currency, "USD")
		self.assertEqual(positions[usd.bank_account].days[2].closing, 800)

	def test_horizon(self):
		bank = ensure_alert_bank()
		fund_bank(bank, 10000, self.day(-1))
		late = make_pdc_cheque(
			amount=700, issue_date=self.today, due_date=self.day(7), bank_account=bank.bank_account
		)
		late.deliver(handover_date=self.today)
		for horizon in (1, 3, 7):
			with self.subTest(horizon=horizon):
				(position,) = get_bank_positions(COMPANY, horizon, [bank.bank_account])
				self.assertEqual(len(position.days), horizon)
				self.assertEqual(position.days[0].date, self.today)
				self.assertEqual(position.days[-1].date, self.day(horizon - 1))
		(position,) = get_bank_positions(COMPANY, 7, [bank.bank_account])
		flows = [f.reference_name for day in position.days for f in day.flows]
		self.assertNotIn(late.name, flows)
		self.assertEqual(position.days[-1].closing, 10000)
		data, _summary = run_report(bank.bank_account, self.today, self.day(7))
		self.assertIn(late.name, [r.get("reference_name") for r in data])

	def test_multi_currency_no_throw(self):
		egp = ensure_alert_bank()
		usd = ensure_alert_bank("USD")
		fund_bank(usd, 300, self.day(-1), rate=USD_RATE)
		fund_bank(egp, 5000, self.day(-1))
		banks = [egp.bank_account, usd.bank_account]
		positions = {p.bank_account: p for p in get_bank_positions(COMPANY, 3, banks)}
		self.assertEqual(positions[egp.bank_account].currency, "EGP")
		self.assertEqual(positions[usd.bank_account].currency, "USD")
		self.assertEqual(positions[usd.bank_account].current_balance, 300)
		self.assertEqual(positions[egp.bank_account].current_balance, 5000)

	def test_shared_gl_account_merged(self):
		bank = ensure_alert_bank()
		twin = add_twin(bank)
		fund_bank(bank, 50000, self.day(-1))
		first = make_pdc_cheque(
			amount=1000, issue_date=self.today, due_date=self.day(1), bank_account=bank.bank_account
		)
		first.deliver(handover_date=self.today)
		second = make_pdc_cheque(amount=2000, issue_date=self.today, due_date=self.day(2), bank_account=twin)
		second.deliver(handover_date=self.today)
		set_thresholds(
			self,
			[
				{"bank_account": bank.bank_account, "warning_balance": 20000, "minimum_balance": 10000},
				{"bank_account": twin, "warning_balance": 49500, "minimum_balance": 48500},
			],
		)

		positions = [p for p in evaluate(COMPANY, [bank.bank_account, twin], horizon_days=3)]
		self.assertEqual(len(positions), 1)
		(position,) = positions
		self.assertEqual(position.bank_accounts, sorted([bank.bank_account, twin]))
		self.assertEqual(position.bank_account, sorted([bank.bank_account, twin])[0])
		self.assertEqual([d.expected_out for d in position.days], [0, 1000, 2000])
		self.assertEqual([d.closing for d in position.days], [50000, 49000, 47000])
		# the strictest thresholds of the two rows
		self.assertEqual((position.minimum, position.warning), (48500, 49500))
		self.assertEqual([d.level for d in position.days], ["OK", "Warning", "Critical"])

	def test_unassigned_flows_counted(self):
		bank = ensure_alert_bank()
		before = get_bank_positions(COMPANY, 3)[0].unassigned_flows
		# an incoming cheque without a deposit bank (an outgoing cheque always has a bank account); it can
		# only be posted to the party once it has a bank
		self.use_company_settings(incoming_party_posting="Manual")
		cheque = make_pdc_cheque(
			"Incoming", amount=900, issue_date=self.today, due_date=self.day(1), bank_account=None
		)
		cheque.receive(receipt_date=self.today)
		positions = get_bank_positions(COMPANY, 3)
		self.assertEqual(positions[0].unassigned_flows, before + 1)
		self.assertEqual({p.unassigned_flows for p in positions}, {before + 1})
		flows = [f.reference_name for p in positions for day in p.days for f in day.flows]
		self.assertNotIn(cheque.name, flows)
		self.assertIn(bank.bank_account, [p.bank_account for p in positions])

	def test_query_count_constant(self):
		banks = []
		for index in range(6):
			bank = ensure_alert_bank()
			fund_bank(bank, 10000, self.day(-1))
			cheque = make_pdc_cheque(
				amount=100 + index,
				issue_date=self.today,
				due_date=self.day(1),
				bank_account=bank.bank_account,
			)
			cheque.deliver(handover_date=self.today)
			banks.append(bank.bank_account)
		# warm the caches (currencies, meta, user permissions)
		get_bank_positions(COMPANY, 7, banks)
		baseline = count_queries(lambda: get_bank_positions(COMPANY, 7, banks[:2]))
		with self.assertQueryCount(baseline):
			positions = get_bank_positions(COMPANY, 7, banks)
		self.assertEqual(len(positions), 6)

	def test_report_unchanged(self):
		egp = ensure_alert_bank()
		usd = ensure_alert_bank("USD")
		with self.assertRaises(frappe.ValidationError) as raised:
			bank_position_forecast.execute(
				{
					"company": COMPANY,
					"bank_account": [egp.bank_account, usd.bank_account],
					"from_date": self.today,
					"to_date": self.day(6),
					"periodicity": "Daily",
					"currency_view": "Bank Currency",
				}
			)
		self.assertIn("different currencies", str(raised.exception))
		# the Company Currency view of the same banks still works, and knows nothing of `per_bank`
		_columns, data, *_rest = bank_position_forecast.execute(
			{
				"company": COMPANY,
				"bank_account": [egp.bank_account, usd.bank_account],
				"from_date": self.today,
				"to_date": self.day(6),
				"periodicity": "Daily",
				"currency_view": "Company Currency",
			}
		)
		self.assertTrue(data)
		self.assertEqual({r.currency for r in data}, {"EGP"})


def count_queries(fn) -> int:
	"""Number of SQL queries `fn()` runs (the technique of IntegrationTestCase.assertQueryCount)."""
	queries = []
	original = frappe.db.__class__.sql

	def counting(*args, **kwargs):
		queries.append(1)
		return original(*args, **kwargs)

	frappe.db.__class__.sql = counting
	try:
		fn()
	finally:
		frappe.db.__class__.sql = original
	return len(queries)

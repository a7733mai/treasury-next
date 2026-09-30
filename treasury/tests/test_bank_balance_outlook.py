"""Phase 7e: report Bank Balance Outlook and the number card "Banks Below Minimum (7 days)" (spec 7e §3.6,
§3.7, §7 E)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import fmt_money

from treasury.balance_alerts import dashboard
from treasury.pdc.report.bank_balance_outlook.bank_balance_outlook import execute
from treasury.tests.test_bank_positions import add_twin
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	USD_RATE,
	USD_SUPPLIER,
	BalanceAlertTestMixin,
	add_user_permission,
	ensure_alert_bank,
	ensure_company,
	ensure_user,
	fund_bank,
	make_alert_fixture_e1,
	make_pdc_cheque,
	set_thresholds,
)

USD_ONLY_USER = "treasury-outlook-usd@example.com"
OTHER_CO_USER = "treasury-outlook-otherco@example.com"


class TestBankBalanceOutlook(BalanceAlertTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(USD_ONLY_USER, ["Treasury User"])
		ensure_user(OTHER_CO_USER, ["Treasury User"])
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		add_user_permission(OTHER_CO_USER, "Company", OTHER_COMPANY)

	def setUp(self):
		super().setUp()
		self.e1 = make_alert_fixture_e1(self)
		self.usd = ensure_alert_bank("USD")
		fund_bank(self.usd, 1000, self.day(-1), rate=USD_RATE)
		cheque = make_pdc_cheque(
			amount=200,
			party=USD_SUPPLIER,
			issue_date=self.today,
			due_date=self.day(2),
			bank_account=self.usd.bank_account,
		)
		cheque.deliver(handover_date=self.today)
		self.banks = [self.e1.bank_account, self.usd.bank_account]

	def run_report(self, **filters):
		return execute({"company": COMPANY, "bank_account": self.banks, "horizon_days": 7, **filters})

	def test_rows(self):
		_columns, data, _message, _chart, _summary = self.run_report()
		rows = {r.bank_account: r for r in data}
		self.assertEqual(set(rows), set(self.banks))
		egp = rows[self.e1.bank_account]
		self.assertEqual(
			(
				egp.current_balance,
				egp.lowest_balance,
				egp.lowest_date,
				egp.closing_balance,
				egp.level,
				egp.first_breach,
				egp.shortfall,
				egp.minimum_balance,
				egp.warning_balance,
			),
			(100000, -10020, self.day(5), -10020, "Critical", self.day(3), 30020, 20000, 50000),
		)
		self.assertEqual((egp.currency, egp.lowest_company_currency), ("EGP", -10020))
		usd = rows[self.usd.bank_account]
		self.assertEqual((usd.currency, usd.current_balance, usd.lowest_balance), ("USD", 1000, 800))
		self.assertEqual(usd.lowest_company_currency, 800 * USD_RATE)
		self.assertEqual((usd.level, usd.shortfall, usd.first_breach), ("No Threshold", 0, None))

	def test_level_filter_and_horizon(self):
		_columns, data, *_rest = self.run_report(level="Critical")
		self.assertEqual([r.bank_account for r in data], [self.e1.bank_account])
		_columns, data, *_rest = self.run_report(level="No Threshold")
		self.assertEqual([r.bank_account for r in data], [self.usd.bank_account])
		self.assertRaisesRegex(frappe.ValidationError, "between 1 and 31", self.run_report, horizon_days=32)
		self.assertRaisesRegex(frappe.ValidationError, "between 1 and 31", self.run_report, horizon_days=0)
		# a shorter horizon ends before the first breach
		_columns, data, *_rest = self.run_report(horizon_days=3)
		egp = next(r for r in data if r.bank_account == self.e1.bank_account)
		self.assertEqual((egp.level, egp.lowest_balance, egp.closing_balance), ("OK", 70000, 80000))

	def test_chart_and_summary(self):
		columns, data, _message, chart, summary = self.run_report()
		# worst first (design 7e §6.2): the Critical bank, then the one without thresholds
		self.assertEqual([r.bank_account for r in data], self.banks)
		self.assertEqual(
			[c["fieldname"] for c in columns[:3]], ["bank_account", "level_label", "current_balance"]
		)
		self.assertEqual(chart["data"]["labels"], [r.bank_account for r in data])
		lowest, minimum = chart["data"]["datasets"]
		self.assertEqual(lowest["values"], [r.lowest_company_currency for r in data])
		# design 7e P19: bars and the minimum line (none without thresholds)
		self.assertEqual((lowest["chartType"], minimum["chartType"]), ("bar", "line"))
		self.assertEqual(minimum["values"], [20000, None])
		self.assertEqual((chart["type"], chart["options"]), ("axis-mixed", "EGP"))
		values = [card["value"] for card in summary]
		self.assertEqual(values[:3], [1, 0, 100000 + 1000 * USD_RATE])
		self.assertEqual(summary[0]["indicator"], "Red")
		# design 7e P20: the largest shortfall, in the bank's currency
		self.assertEqual(
			(summary[3]["value"], summary[3]["indicator"]),
			(f"{self.e1.bank_account} · {fmt_money(30020, currency='EGP')}", "Red"),
		)
		# no shortfall: no card
		_columns, _data, _message, _chart, summary = self.run_report(horizon_days=3)
		self.assertEqual(len(summary), 3)

	def test_user_permission_on_bank_account(self):
		add_user_permission(USD_ONLY_USER, "Bank Account", self.usd.bank_account)
		with self.set_user(USD_ONLY_USER):
			_columns, data, *_rest = execute({"company": COMPANY, "horizon_days": 7})
		self.assertEqual([r.bank_account for r in data], [self.usd.bank_account])

	def test_shared_gl_account_filter(self):
		"""Review M4: selecting one of two bank accounts on one GL account gives the same row as both: the
		sibling's flows and thresholds are kept (the GL balance covers both anyway)."""
		bank = ensure_alert_bank()
		twin = add_twin(bank)
		fund_bank(bank, 50000, self.day(-1))
		cheque = make_pdc_cheque(amount=2000, issue_date=self.today, due_date=self.day(1), bank_account=twin)
		cheque.deliver(handover_date=self.today)
		set_thresholds(self, [{"bank_account": twin, "warning_balance": 49500, "minimum_balance": 48500}])

		def row(banks):
			_columns, data, *_rest = execute({"company": COMPANY, "bank_account": banks, "horizon_days": 3})
			(only,) = data
			return (only.bank_account, only.lowest_balance, only.minimum_balance, only.level)

		both = row([bank.bank_account, twin])
		self.assertEqual(both, (sorted([bank.bank_account, twin])[0], 48000, 48500, "Critical"))
		self.assertEqual(row([bank.bank_account]), both)
		self.assertEqual(row([twin]), both)

	def test_low_balance_card(self):
		card = dashboard.get_low_balance_card({"company": COMPANY, "horizon_days": 7})
		self.assertEqual(card["value"], 1)
		self.assertEqual(card["fieldtype"], "Int")
		self.assertEqual(card["route"], ["query-report", "Bank Balance Outlook"])
		self.assertEqual(card["route_options"], {"company": COMPANY, "horizon_days": 7, "level": "Critical"})
		# JSON filters (the Number Card passes a string), default horizon
		self.assertEqual(dashboard.get_low_balance_card(frappe.as_json({"company": COMPANY}))["value"], 1)
		self.assertEqual(dashboard.get_low_balance_card({"company": COMPANY, "horizon_days": 3})["value"], 0)
		with patch.object(dashboard.erpnext, "get_default_company", return_value=None):
			self.assertEqual(dashboard.get_low_balance_card({}), {"value": 0, "fieldtype": "Int"})
		with self.set_user(OTHER_CO_USER):
			self.assertRaises(frappe.PermissionError, dashboard.get_low_balance_card, {"company": COMPANY})

	def test_standard_records(self):
		for doctype, name in (
			("Report", "Bank Balance Outlook"),
			("Number Card", "Banks Below Minimum (7 days)"),
			("Dashboard Chart", "Lowest Projected Balance per Bank"),
			("DocType", "Treasury Alert Log"),
		):
			self.assertTrue(frappe.db.exists(doctype, name), f"{doctype} {name}")
		workspace = frappe.get_doc("Workspace", "Treasury")
		self.assertIn("Banks Below Minimum (7 days)", [c.number_card_name for c in workspace.number_cards])
		self.assertIn("Lowest Projected Balance per Bank", [c.chart_name for c in workspace.charts])
		links = [link.link_to for link in workspace.links]
		self.assertIn("Bank Balance Outlook", links)
		self.assertIn("Treasury Alert Log", links)
		# design 7e §7: the card first, the red shortcut to the report (P21)
		self.assertEqual(workspace.number_cards[0].number_card_name, "Banks Below Minimum (7 days)")
		shortcut = next(s for s in workspace.shortcuts if s.label == "Bank Balance Outlook")
		self.assertEqual(
			(shortcut.type, shortcut.link_to, shortcut.color), ("Report", "Bank Balance Outlook", "Red")
		)

"""Phase 7h: the Treasury Dashboard data (treasury.pdc.overview, spec 7h §3.1 / §7 A): every figure equals the
report or card it drills down to. Fixture E1 of spec 7e §4 (dates relative to the real today): an EGP bank of
its own with the closings 100,000 · 70,000 · 80,000 · 29,980 · 29,980 · -10,020 · -10,020.

IntegrationTestCase rolls back only at the end of the class: the bank-filtered figures belong to each test's
own E1 bank, the company-wide sections are compared with their reports (or as a difference)."""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, formatdate, get_first_day, getdate

from treasury.bank_facilities.dashboard import get_facility_card
from treasury.pdc import dashboard
from treasury.pdc.overview import HORIZONS, get_overview
from treasury.pdc.page.treasury_dashboard.treasury_dashboard import get_dashboard_data
from treasury.tests.test_bank_positions import add_twin
from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	BalanceAlertTestMixin,
	bank_account_name,
	ensure_account,
	ensure_bank_account,
	ensure_charge_category,
	ensure_company,
	make_alert_fixture_e1,
	make_bank_charge,
	make_bank_facility,
	make_drawdown,
	make_letter_of_credit,
	make_letter_of_guarantee,
	make_own_bank,
	make_pdc_cheque,
	make_security_cheque,
	run_report,
)


def by_name(datasets: list) -> dict:
	return {d["name"]: d["values"] for d in datasets}


class TestTreasuryDashboard(BalanceAlertTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.fx = make_alert_fixture_e1(self)
		self.bank = self.fx.bank_account

	def overview(self, horizon=7, banks="e1"):
		return get_overview(COMPANY, [self.bank] if banks == "e1" else banks, horizon)

	# ------------------------------------------------------------------ positions

	def test_positions_match_bank_balance_outlook(self):
		for horizon in HORIZONS:
			with self.subTest(horizon=horizon):
				positions = self.overview(horizon).sections.positions
				(row,) = positions.banks
				self.assertEqual(
					(
						row.bank_account,
						row.current_balance,
						row.projected_7,
						row.lowest.date,
						row.lowest.closing,
						row.level,
						row.first_breach,
						row.closing_horizon,
						row.shortfall,
						row.minimum,
						row.warning,
					),
					(
						self.bank,
						100000,
						-10020,
						str(self.day(5)),
						-10020,
						"Critical",
						str(self.day(3)),
						-10020,
						30020,
						20000,
						50000,
					),
				)
				_columns, data, _message, _chart, summary = run_report(
					"Bank Balance Outlook",
					{"company": COMPANY, "bank_account": [self.bank], "horizon_days": horizon},
				)
				(outlook,) = data
				self.assertEqual(
					(
						outlook.current_balance,
						outlook.lowest_balance,
						str(outlook.lowest_date),
						outlook.closing_balance,
						outlook.level,
						str(outlook.first_breach),
						outlook.shortfall,
						outlook.minimum_balance,
						outlook.warning_balance,
					),
					(
						row.current_balance,
						row.lowest.closing,
						row.lowest.date,
						row.closing_horizon,
						row.level,
						row.first_breach,
						row.shortfall,
						row.minimum,
						row.warning,
					),
				)
				self.assertEqual(positions.totals.below_minimum.count, summary[0]["value"])
				self.assertEqual(positions.totals.below_warning.count, summary[1]["value"])
				self.assertEqual(positions.totals.below_minimum.route_options["level"], "Critical")
				self.assertEqual(positions.totals.below_minimum.route_options["bank_account"], [self.bank])

		# no bank selected: the whole company, as the Outlook without a bank filter
		positions = get_overview(COMPANY, None, 7).sections.positions
		_columns, data, _message, _chart, summary = run_report(
			"Bank Balance Outlook", {"company": COMPANY, "horizon_days": 7}
		)
		self.assertEqual(flt(positions.totals.current_company, 2), flt(summary[2]["value"], 2))
		self.assertEqual(
			{r.bank_account: (r.level, r.current_balance, r.lowest.closing) for r in positions.banks},
			{r.bank_account: (r.level, r.current_balance, r.lowest_balance) for r in data},
		)

	def test_charts(self):
		charts = self.overview(7).sections.positions.charts
		curve = charts.balance_curve
		self.assertEqual(curve.data["labels"], [formatdate(self.day(i)) for i in range(7)])
		self.assertEqual(
			by_name(curve.data["datasets"])[self.bank], [100000, 70000, 80000, 29980, 29980, -10020, -10020]
		)
		flows = by_name(charts.cash_flows.data["datasets"])
		self.assertEqual(flows["Inflows"], [0, 0, 10000, 0, 0, 0, 0])
		self.assertEqual(flows["Outflows"], [0, 30000, 0, 50020, 0, 40000, 0])
		self.assertEqual(charts.cash_flows.type, "axis-mixed")

		charts = self.overview(30).sections.positions.charts
		self.assertEqual(len(charts.cash_flows.data["labels"]), 5)
		flows = by_name(charts.cash_flows.data["datasets"])
		self.assertEqual((flows["Inflows"][0], flows["Outflows"][0]), (10000, 120020))
		self.assertEqual(flows["Inflows"][1:] + flows["Outflows"][1:], [0] * 8)

		maturities = charts.maturities
		self.assertEqual(len(maturities.data["labels"]), 5)  # no Overdue bucket
		values = by_name(maturities.data["datasets"])
		self.assertEqual(
			{name: v[0] for name, v in values.items()},
			{"Cheques In": 10000, "Cheques Out": -30000, "Transfers": -50020, "Installments": -40000},
		)
		self.assertTrue(all(not any(v[1:]) for v in values.values()))
		self.assertEqual(maturities.barOptions, {"stacked": 1})
		# 7h-2: the untranslated category of each dataset (fixed colours on the page), legend order kept
		self.assertEqual(maturities.keys, ["Cheques In", "Cheques Out", "Installments", "Transfers"])
		self.assertEqual(maturities.keys, [d["name"] for d in maturities.data["datasets"]])
		self.assertEqual(maturities.overdue, 0)

	def test_horizon_totals(self):
		"""Design 7h P5: "Projected in {H} Days" = the closing at the horizon, company currency; the worst
		bank's lowest in company currency for the card subtitle."""
		for horizon in HORIZONS:
			with self.subTest(horizon=horizon):
				positions = self.overview(horizon).sections.positions
				(row,) = positions.banks
				self.assertEqual(row.closing_horizon_company, -10020)
				self.assertEqual(row.lowest.closing_company, -10020)
				self.assertEqual(positions.totals.closing_horizon_company, -10020)
				route = positions.totals.horizon_route
				self.assertEqual(route.route, ["query-report", "Bank Position Forecast"])
				self.assertEqual(route.route_options["to_date"], str(self.day(horizon - 1)))
				self.assertEqual(route.route_options["bank_account"], [self.bank])

	# ------------------------------------------------------------------ cheques

	def test_cheque_kpis_match_pdc_register(self):
		cheques = self.overview().sections.cheques
		expected = {"due_out": 30000, "due_in": 10000, "issued": 30000, "in_hand": 10000}
		for key, amount in expected.items():
			with self.subTest(kpi=key):
				kpi = cheques[key]
				self.assertEqual((kpi.count, kpi.amount), (1, amount))
				self.assertEqual(kpi.route, ["query-report", "PDC Register"])
				_columns, data = run_report("PDC Register", kpi.route_options)
				total = data[-1]
				self.assertEqual((total.cheque_count, total.base_amount), (kpi.count, kpi.amount))

		cheques = get_overview(COMPANY, None, 7).sections.cheques
		for key, direction, scope in (
			("due_out", "Outgoing", "due_7_days"),
			("due_in", "Incoming", "due_7_days"),
			("issued", "Outgoing", "outstanding"),
			("in_hand", "Incoming", "outstanding"),
		):
			card = dashboard.get_cheque_total({"company": COMPANY, "direction": direction, "scope": scope})
			self.assertEqual(flt(cheques[key].amount, 2), flt(card["value"], 2), key)

	def test_bounced_this_month(self):
		cheque = make_pdc_cheque(
			"Incoming", amount=7000, issue_date=self.today, due_date=self.day(10), bank_account=self.bank
		)
		cheque.receive(receipt_date=self.today)
		cheque.bounce(bounce_date=self.today, reason="Insufficient funds")
		before_month = getdate(add_days(get_first_day(self.today), -1))
		old = make_pdc_cheque(
			"Incoming",
			amount=3000,
			issue_date=add_days(before_month, -1),
			due_date=add_days(before_month, 20),
			bank_account=self.bank,
		)
		old.receive(receipt_date=add_days(before_month, -1))
		old.bounce(bounce_date=before_month, reason="Insufficient funds")

		bounced = self.overview().sections.cheques.bounced_month
		self.assertEqual((bounced.count, bounced.amount), (1, 7000))
		self.assertEqual(bounced.route_options["bank_account"], self.bank)
		_columns, rows = run_report("Bounced and Postponed Cheques", bounced.route_options)
		self.assertEqual((len(rows), flt(sum(r.amount for r in rows), 2)), (bounced.count, bounced.amount))
		# the new Bank Account filter of the report
		_columns, rows = run_report(
			"Bounced and Postponed Cheques", {**bounced.route_options, "bank_account": bank_account_name()}
		)
		self.assertNotIn(cheque.name, [r.cheque for r in rows])

	# ------------------------------------------------------------------ transfers, charges

	def test_transfers_awaiting_confirmation(self):
		self.assertEqual(self.overview().sections.transfers.awaiting.count, 0)
		frappe.get_doc("Bank Transfer", self.fx.tr1).send_now()
		awaiting = self.overview().sections.transfers.awaiting
		self.assertEqual((awaiting.count, awaiting.amount), (1, 50000))
		self.assertEqual(awaiting.route, ["List", "Bank Transfer"])
		self.assertEqual(len(frappe.get_list("Bank Transfer", filters=awaiting.route_options)), 1)
		_columns, rows, *_rest = run_report(
			"Bank Transfer Register",
			{
				"company": COMPANY,
				"status": "Sent",
				"bank_account": self.bank,
				"from_date": "2000-01-01",
				"to_date": str(self.day(30)),
			},
		)
		self.assertEqual(len(rows), awaiting.count)

	def test_charges_match_analysis(self):
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		first = ensure_charge_category("_Test Dashboard Charge A", keywords=("dash charge a",))
		second = ensure_charge_category("_Test Dashboard Charge B", keywords=("dash charge b",))
		make_bank_charge(category=first, amount=100, posting_date=self.today, bank_account=self.bank)
		make_bank_charge(category=second, amount=50, posting_date=self.today, bank_account=self.bank)
		last_month = add_days(get_first_day(self.today), -1)
		make_bank_charge(category=first, amount=70, posting_date=last_month, bank_account=self.bank)

		charges = self.overview().sections.charges
		self.assertEqual((charges.month.net, charges.month.vouchers), (150, 2))
		result = run_report("Bank Charges Analysis", charges.month.route_options)
		summary = {card["label"]: card["value"] for card in result[4]}
		self.assertEqual(charges.month.net, summary["Net Charges"])
		groups = [row for row in result[1] if not row.get("is_total")]
		self.assertEqual(charges.by_category.data["labels"], [g.group for g in groups])
		self.assertEqual(charges.by_category.data["datasets"][0]["values"], [g.net for g in groups])
		self.assertEqual(charges.by_category.data["labels"], [first, second])

	# ------------------------------------------------------------------ company-wide sections

	def test_security_totals_match_exposure(self):
		before = get_overview(COMPANY, None, 7).sections.security
		held = make_security_cheque("Incoming", amount=5000, issue_date=self.today)
		held.receive(receipt_date=self.today)
		given = make_security_cheque("Outgoing", amount=3000, issue_date=self.today)
		given.issue(handover_date=self.today)
		blank = make_security_cheque("Incoming", amount_type="Open / Blank", issue_date=self.today)
		blank.receive(receipt_date=self.today)

		security = self.overview().sections.security
		self.assertEqual(
			(
				flt(security.held - before.held, 2),
				flt(security.given - before.given, 2),
				security.open_held - before.open_held,
				security.count_held - before.count_held,
				security.count_given - before.count_given,
			),
			(5000, 3000, 1, 2, 1),
		)
		*_rest, summary = run_report("Security Exposure", security.route_options)
		self.assertEqual(
			[card["value"] for card in summary[:4]],
			[security.held, security.given, security.open_held, security.open_given],
		)

	def test_facilities_and_installments(self):
		overview = self.overview()
		facilities = overview.sections.facilities
		*_rest, summary = run_report("Facility Utilisation", facilities.totals.route_options)
		self.assertEqual(
			(facilities.totals.limit_company, facilities.totals.utilised_company),
			(summary[0]["value"], summary[1]["value"]),
		)
		self.assertIn(self.fx.facility, [r.facility for r in facilities.rows])
		percents = [r.percent_used for r in facilities.rows]
		self.assertEqual(percents, sorted(percents, reverse=True))

		installments = overview.sections.installments
		card = get_facility_card({"company": COMPANY, "scope": "due_30"})
		self.assertEqual(installments.due_30.amount, card["value"])
		self.assertGreaterEqual(installments.due_30.amount, 40000)
		self.assertEqual(installments.due_30.route_options, card["route_options"])
		self.assertEqual(installments.next[0].due_date, str(self.day(5)))

		make_drawdown(
			self.fx.facility,
			amount=5000,
			drawdown_date=self.day(-10),
			tenor_months=1,
			frequency="Bullet",
			method="Bullet",
			first_repayment_date=self.day(-3),
		)
		installments = self.overview().sections.installments
		card = get_facility_card({"company": COMPANY, "scope": "overdue"})
		self.assertGreaterEqual(installments.overdue.count, 1)
		self.assertEqual(installments.overdue.count, card["value"])
		self.assertEqual(installments.overdue.route_options, card["route_options"])

	def test_expiring_instruments(self):
		bank = make_own_bank()
		facility = make_bank_facility(
			total_limit=2_000_000,
			limits=[
				{"limit_type": "Letter of Guarantee", "limit_amount": 1_000_000},
				{"limit_type": "Letter of Credit", "limit_amount": 1_000_000},
			],
			bank_account=bank.bank_account,
			start_date=self.day(-30),
			expiry_date=self.day(365),
		)
		before = self.overview()
		for days in (10, 60):
			make_letter_of_guarantee(facility, amount=1000, issue_date=self.today, expiry_date=self.day(days))
			make_letter_of_credit(facility, amount=1000, issue_date=self.today, expiry_date=self.day(days))
		accepted = make_letter_of_credit(
			facility, amount=1000, issue_date=self.today, expiry_date=self.day(5)
		)
		frappe.db.set_value("Letter of Credit", accepted.name, "status", "Accepted")

		after = self.overview()
		guarantees, credits = after.sections.guarantees, after.sections.credits
		self.assertEqual(guarantees.count - before.sections.guarantees.count, 1)
		self.assertEqual(credits.count - before.sections.credits.count, 1)
		self.assertNotIn(accepted.name, [c.name for c in credits.next])
		_columns, data, *_rest = run_report("LG Register", guarantees.route_options)
		self.assertEqual(len(data), guarantees.count)
		_columns, data, *_rest = run_report("LC Register", credits.route_options)
		self.assertEqual(len(data), credits.count)
		self.assertNotIn(accepted.name, [r.name for r in data])
		self.assertEqual(credits.route_options["expiring_within"], 30)

	# ------------------------------------------------------------------ filters

	def test_bank_filter(self):
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		other_gl = ensure_account(
			"Treasury Bank EGP", "Bank Accounts", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR
		)
		other_bank = ensure_bank_account("Treasury Dash Other Co", other_gl, company=OTHER_COMPANY)
		self.assertRaisesRegex(
			frappe.ValidationError, "does not belong to company", get_overview, COMPANY, [other_bank], 7
		)

		twin = add_twin(self.fx.bank)
		overview = self.overview()
		self.assertEqual(set(overview.bank_accounts), {self.bank, twin})
		self.assertEqual(overview.sections.positions.banks[0].bank_accounts, sorted([self.bank, twin]))

		base = bank_account_name()
		overview = get_overview(COMPANY, [base], 7)
		self.assertNotIn(self.bank, [b.bank_account for b in overview.sections.positions.banks])
		totals = dashboard.open_cheque_totals(COMPANY, [base, self.bank])
		self.assertEqual(
			flt(overview.sections.cheques.due_out.amount + 30000, 2),
			flt(totals["Outgoing"]["due_7_days"].amount, 2),
		)

	def test_invalid_filters(self):
		self.assertRaisesRegex(
			frappe.ValidationError, "Horizon must be 7, 14 or 30 days", get_overview, COMPANY, None, 10
		)
		self.assertRaisesRegex(frappe.ValidationError, "Company is required", get_overview, None, None, 7)
		self.assertRaises(frappe.DoesNotExistError, get_overview, "_Test No Such Company", None, 7)

	def test_endpoint_wrapper(self):
		data = get_dashboard_data(COMPANY, json.dumps([self.bank]), "7")
		self.assertEqual(json.loads(frappe.as_json(data)), json.loads(frappe.as_json(self.overview(7))))
		data = get_dashboard_data(COMPANY, None, None)
		self.assertEqual((data.horizon, data.bank_accounts), (7, []))

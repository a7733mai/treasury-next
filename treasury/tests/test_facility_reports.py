"""Bank Facilities reports, number cards, utilisation and the Bank Position Forecast integration (Phase 5a).

The reports' "today" is frozen to day(0) = 2025-07-01 (`treasury.pdc.report_utils.today` is patched). Each
test uses its own bank account (`make_own_bank`) and its own facility, so the figures it checks are its own.

Scenario (`make_scenario`): a facility (total 1,000,000; Short Term Loan 500,000; Overdraft 50,000) on its
own bank; a 120,000 drawdown on 31 May 2025, 12 monthly equal installments of 10,000 at 12% (Actual/365)
due on the month ends, with 500 upfront fees; installment 1 (30 Jun: 10,000 + 1,183.56 interest) paid with a
100 penalty.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate

from treasury.bank_facilities import dashboard
from treasury.bank_facilities.report.cost_of_financing.cost_of_financing import execute as cost_of_financing
from treasury.bank_facilities.report.facility_repayment_schedule_report.facility_repayment_schedule_report import (
	execute as schedule_report,
)
from treasury.bank_facilities.report.facility_utilisation.facility_utilisation import (
	execute as utilisation_report,
)
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import BankPositionForecast
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	acc,
	add_user_permission,
	day,
	ensure_company,
	ensure_facility_accounts,
	ensure_user,
	make_bank_facility,
	make_drawdown,
	make_own_bank,
	make_repayment,
)

T = day(0)  # 2025-07-01, the reports' "today"
D = getdate
RESTRICTED = "treasury-facrpt-other-co@example.com"


def spend(bank, amount: float, posting_date):
	"""Money out of the bank GL (e.g. an overdraft use) against an expense."""
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": COMPANY,
			"posting_date": posting_date,
			"accounts": [
				{
					"account": acc("Treasury Bank Charges"),
					"cost_center": acc("Main"),
					"debit_in_account_currency": amount,
				},
				{"account": bank.bank_gl, "credit_in_account_currency": amount},
			],
		}
	).insert(ignore_permissions=True)
	je.submit()
	return je.name


class FacilityReportTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0)
		patcher = patch("treasury.pdc.report_utils.today", return_value=str(T))
		patcher.start()
		self.addCleanup(patcher.stop)

	def make_scenario(self) -> frappe._dict:
		bank = make_own_bank()
		facility = make_bank_facility(
			total_limit=1_000_000,
			limits=[
				{"limit_type": "Short Term Loan", "limit_amount": 500_000, "interest_rate": 12},
				{"limit_type": "Overdraft", "limit_amount": 50_000},
			],
			bank_account=bank.bank_account,
		)
		drawdown = make_drawdown(
			facility,
			drawdown_date="2025-05-31",
			bank_account=bank.bank_account,
			upfront_fees=500,
			fees_account=self.accounts.fees,
		)
		repayment = make_repayment(
			drawdown, "2025-06-30", penalty=100, penalty_account=self.accounts.penalty
		)
		return frappe._dict(bank=bank, facility=facility, drawdown=drawdown, repayment=repayment)


class TestUtilisation(FacilityReportTestCase):
	def test_overdraft_uses_the_negative_bank_balance(self):
		bank = make_own_bank()
		facility = make_bank_facility(
			total_limit=100_000,
			limits=[{"limit_type": "Overdraft", "limit_amount": 50_000}],
			bank_account=bank.bank_account,
		)
		self.assertEqual(get_utilisation(facility.name).utilised, 0)
		spend(bank, 30_000, "2025-06-15")
		utilisation = get_utilisation(facility.name, "Overdraft")
		self.assertEqual((utilisation.limits[0].utilised, utilisation.limits[0].available), (30_000, 20_000))
		self.assertEqual(utilisation.limits[0].percent_used, 60)
		self.assertEqual(get_utilisation(facility.name, as_on="2025-06-14").utilised, 0)

	def test_repayment_restores_the_limit(self):
		scenario = self.make_scenario()
		utilisation = get_utilisation(scenario.facility.name)
		loan = next(r for r in utilisation.limits if r.limit_type == "Short Term Loan")
		overdraft = next(r for r in utilisation.limits if r.limit_type == "Overdraft")
		self.assertEqual((loan.utilised, loan.available), (110_000, 390_000))
		self.assertEqual((overdraft.utilised, overdraft.available), (0, 50_000))  # the bank is in credit
		self.assertEqual(utilisation.available, 890_000)
		# as on the drawdown date: before the repayment
		self.assertEqual(get_utilisation(scenario.facility.name, as_on="2025-06-29").utilised, 120_000)

	def test_utilisation_hook(self):
		scenario = self.make_scenario()
		scenario.facility.reload()
		scenario.facility.append("limits", {"limit_type": "Letter of Guarantee", "limit_amount": 100_000})
		scenario.facility.save()
		source = "treasury.tests.test_facility_reports.fake_lg_source"
		with self.patch_hooks({"treasury_facility_utilisation_sources": [source]}):
			utilisation = get_utilisation(scenario.facility.name, "Letter of Guarantee")
		self.assertEqual((utilisation.limits[0].utilised, utilisation.utilised), (25_000, 135_000))


def fake_lg_source(facility, as_on=None):
	"""A `treasury_facility_utilisation_sources` hook (Letters of Guarantee come with Phase 5b)."""
	return [{"limit_type": "Letter of Guarantee", "amount": 25_000}]


class TestBankPositionForecastIntegration(FacilityReportTestCase):
	def run_report(self, bank, **filters):
		report = BankPositionForecast(
			{
				"company": COMPANY,
				"bank_account": [bank.bank_account],
				"from_date": T,
				"to_date": add_days(T, 61),  # 31 Aug
				"periodicity": "Monthly",
				"currency_view": "Company Currency",
				"show_details": 1,
				**filters,
			}
		)
		columns, data, _message, _chart, summary = report.run()
		return report, data, summary

	def test_unpaid_installments_are_outflows(self):
		scenario = self.make_scenario()
		_report, data, summary = self.run_report(scenario.bank)
		flows = [r for r in data if r.get("indent") and r.reference_name == scenario.drawdown.name]
		self.assertEqual(
			[(r.due_date, r.expected_out, r.parent_row_id) for r in flows],
			[
				(D("2025-07-31"), 11121.10, "p0"),  # 10,000 + 110,000 x 12% x 31 / 365
				(D("2025-08-31"), 11019.18, "p1"),  # 10,000 + 100,000 x 12% x 31 / 365
			],
		)
		self.assertTrue(all(r.expected_in == 0 for r in flows))
		# installment 1 was paid: not a flow (it is an actual movement of 30 Jun)
		self.assertNotIn(D("2025-06-30"), [r.due_date for r in flows])
		cards = {c["label"]: c["value"] for c in summary}
		self.assertEqual(cards["Available Overdraft"], 50_000)

		# a partly paid installment shows what is left
		make_repayment(scenario.drawdown, "2025-07-15", principal_paid=4000, interest_paid=1121.10)
		_report, data, _summary = self.run_report(scenario.bank)
		flows = [r for r in data if r.get("indent") and r.reference_name == scenario.drawdown.name]
		self.assertEqual(flows[0].expected_out, 6000)

	def test_other_banks_and_overdue(self):
		scenario = self.make_scenario()
		other = make_own_bank()
		_report, data, summary = self.run_report(other)
		self.assertFalse([r for r in data if r.get("reference_name") == scenario.drawdown.name])
		self.assertNotIn("Available Overdraft", [c["label"] for c in summary])

		# an unpaid installment due before today is overdue
		overdue = make_drawdown(
			scenario.facility,
			amount=3000,
			drawdown_date="2025-05-31",
			tenor_months=3,
			bank_account=scenario.bank.bank_account,
		)
		_report, data, _summary = self.run_report(scenario.bank)
		rows = [r for r in data if r.get("reference_name") == overdue.name]
		# installment 1 (30 Jun): 1,000 + 3,000 x 12% x 30 / 365
		self.assertEqual((rows[0].parent_row_id, rows[0].expected_out), ("overdue", 1029.59))

	def test_overdraft_summary_in_use(self):
		scenario = self.make_scenario()
		spend(scenario.bank, 150_000, "2025-06-30")  # balance: 120,000 - 500 - 11,283.56 - 150,000
		_report, _data, summary = self.run_report(scenario.bank)
		cards = {c["label"]: c["value"] for c in summary}
		self.assertEqual(cards["Available Overdraft"], flt(50_000 - 41_783.56, 2))


class TestFacilityReports(FacilityReportTestCase):
	def test_facility_utilisation_report(self):
		scenario = self.make_scenario()
		_columns, data, _m, _c, summary = utilisation_report(
			{"company": COMPANY, "facility": scenario.facility.name}
		)
		self.assertEqual([r.indent for r in data], [0, 1, 2, 2])
		bank_row, facility_row, loan, overdraft = data
		self.assertEqual(
			(facility_row.limit_amount, facility_row.utilised, facility_row.available, facility_row.percent_used),
			(1_000_000, 110_000, 890_000, 11),
		)
		self.assertEqual(facility_row.days_to_expiry, (D("2026-12-31") - T).days)
		self.assertEqual((loan.limit_type, loan.utilised, loan.available), ("Short Term Loan", 110_000, 390_000))
		self.assertEqual((overdraft.utilised, overdraft.available), (0, 50_000))
		self.assertEqual((bank_row.limit_company, bank_row.utilised_company), (1_000_000, 110_000))
		cards = {c["label"]: c["value"] for c in summary}
		self.assertEqual(cards["Total Utilised"], 110_000)

		_columns, data, _m, _c, _s = utilisation_report(
			{"company": COMPANY, "facility": scenario.facility.name, "limit_type": "Overdraft"}
		)
		self.assertEqual([r.get("limit_type") for r in data if r.indent == 2], ["Overdraft"])
		self.assertEqual(data[0].utilised_company, 0)

	def test_usd_facility_in_company_currency(self):
		facility = make_bank_facility(currency="USD", total_limit=10_000)
		make_drawdown(facility, amount=1000)
		_columns, data, _m, _c, _s = utilisation_report({"company": COMPANY, "facility": facility.name})
		facility_row = data[1]
		self.assertEqual((facility_row.currency, facility_row.utilised), ("USD", 1000))
		self.assertEqual((facility_row.utilised_company, facility_row.limit_company), (50_000, 500_000))

	def test_repayment_schedule_report(self):
		scenario = self.make_scenario()
		name = scenario.drawdown.name
		filters = {"company": COMPANY, "from_date": T, "to_date": "2025-08-31", "drawdown": name}
		_columns, data, _m, _c, _s = schedule_report(filters)
		self.assertEqual([(r.installment_no, r.status) for r in data], [(2, "Pending"), (3, "Pending")])
		self.assertEqual((data[0].unpaid_principal, data[0].unpaid_interest), (10_000, 1121.10))

		with_paid = {**filters, "from_date": "2025-06-01", "include_paid": 1}
		_columns, data, _m, _c, _s = schedule_report(with_paid)
		self.assertEqual(
			[(r.installment_no, r.status) for r in data], [(1, "Paid"), (2, "Pending"), (3, "Pending")]
		)

		overdue = make_drawdown(scenario.facility, amount=3000, drawdown_date="2025-05-31", tenor_months=3)
		_columns, data, _m, _c, summary = schedule_report({**filters, "drawdown": overdue.name})
		self.assertEqual(
			[(r.installment_no, r.status) for r in data], [(1, "Overdue"), (2, "Pending"), (3, "Pending")]
		)
		cards = {c["label"]: c["value"] for c in summary}
		self.assertEqual(cards["Overdue Installments"], 1)
		without_overdue = {**filters, "drawdown": overdue.name, "include_overdue": 0}
		_columns, data, _m, _c, _s = schedule_report(without_overdue)
		self.assertEqual([r.installment_no for r in data], [2, 3])

	def test_cost_of_financing(self):
		scenario = self.make_scenario()
		filters = {"company": COMPANY, "from_date": "2025-01-01", "to_date": "2025-12-31"}
		_columns, data, _m, _c, _s = cost_of_financing({**filters, "facility": scenario.facility.name})
		parent, *children = data
		self.assertEqual(
			(parent.interest, parent.fees, parent.penalty, parent.commission, parent.total),
			(1183.56, 500, 100, 0, 1783.56),
		)
		self.assertEqual(
			[(c.period_start, c.interest, c.fees, c.penalty) for c in children],
			[(D("2025-05-01"), 0, 500, 0), (D("2025-06-01"), 1183.56, 0, 100)],
		)
		_columns, data, _m, _c, _s = cost_of_financing(
			{**filters, "facility": scenario.facility.name, "periodicity": "Quarterly"}
		)
		self.assertEqual([c.period_start for c in data[1:]], [D("2025-04-01")])
		# a cancelled repayment is no cost
		scenario.repayment.cancel()
		_columns, data, _m, _c, _s = cost_of_financing({**filters, "facility": scenario.facility.name})
		self.assertEqual((data[0].interest, data[0].penalty, data[0].total), (0, 0, 500))

	def test_number_cards(self):
		def card(scope):
			return dashboard.get_facility_card({"company": COMPANY, "scope": scope})["value"]

		before = {scope: card(scope) for scope in ("limits", "utilised", "due_30", "overdue")}
		scenario = self.make_scenario()
		make_drawdown(scenario.facility, amount=3000, drawdown_date="2025-05-31", tenor_months=3)
		self.assertEqual(flt(card("limits") - before["limits"], 2), 1_000_000)
		self.assertEqual(flt(card("utilised") - before["utilised"], 2), 113_000)
		# due from 1 Jul to 31 Jul: installment 2 of the loan (11,121.10) + installment 2 of the small one
		small = flt(1000 + flt(2000 * 0.12 * 31 / 365, 2), 2)
		self.assertEqual(flt(card("due_30") - before["due_30"], 2), flt(11121.10 + small, 2))
		self.assertEqual(card("overdue") - before["overdue"], 1)
		self.assertRaises(frappe.ValidationError, dashboard.get_facility_card, {"company": COMPANY, "scope": "x"})


class TestFacilityReportPermissions(FacilityReportTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(RESTRICTED, ["Treasury User"])
		add_user_permission(RESTRICTED, "Company", OTHER_COMPANY)

	def test_forbidden_company(self):
		company = {"company": COMPANY}
		with self.set_user(RESTRICTED):
			for report in (utilisation_report, schedule_report, cost_of_financing):
				self.assertRaises(frappe.PermissionError, report, company)
			for scope in ("limits", "overdue"):
				self.assertRaises(
					frappe.PermissionError, dashboard.get_facility_card, {**company, "scope": scope}
				)

	def test_user_without_access(self):
		user = ensure_user("treasury-facrpt-noperm@example.com", [])
		with self.set_user(user):
			self.assertRaises(frappe.PermissionError, utilisation_report, {"company": COMPANY})

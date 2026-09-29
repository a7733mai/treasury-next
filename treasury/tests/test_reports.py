"""PDC reports, number card source and dashboard helpers (Phase 2).

Every scenario uses fixed dates around BASE_DATE (`day`) and the reports' "today" is frozen to day(0)
(`treasury.pdc.report_utils.today` is patched). Each test isolates its data through its own bank account
(`make_bank`) or its own party, so tests neither depend on each other nor on other test files.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, today

from treasury.pdc import dashboard
from treasury.pdc.doctype.cheque_book.cheque_book import cancel_leaf
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import BankPositionForecast
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import execute as bank_position
from treasury.pdc.report.bounced_and_postponed_cheques.bounced_and_postponed_cheques import (
	execute as bounced_and_postponed,
)
from treasury.pdc.report.cheque_book_usage.cheque_book_usage import execute as cheque_book_usage
from treasury.pdc.report.pdc_maturity.pdc_maturity import execute as pdc_maturity
from treasury.pdc.report.pdc_register.pdc_register import execute as pdc_register
from treasury.pdc.report.projected_party_statement.projected_party_statement import (
	ProjectedPartyStatement,
)
from treasury.pdc.report.projected_party_statement.projected_party_statement import (
	execute as projected_party_statement,
)
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	CUSTOMER_GROUP,
	DEFAULT_PDC_SETTINGS,
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	SUPPLIER_GROUP,
	TERRITORY,
	TreasuryTestMixin,
	acc,
	add_user_permission,
	day,
	ensure_account,
	ensure_bank_account,
	ensure_company,
	ensure_user,
	make_cheque_book,
	make_pdc_cheque,
	make_purchase_invoice,
	mapping_row,
)

T = day(0)  # the reports' "today"
RESTRICTED_USER = "treasury-rpt-other-co@example.com"
ALLOWED_USER = "treasury-rpt-own-co@example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bank(currency: str = "EGP") -> frappe._dict:
	"""A fresh company bank account with its own GL and PDC accounts and a PDC Account Mapping row."""
	tag = frappe.generate_hash(length=6)
	cur = None if currency == "EGP" else currency
	bank_gl = ensure_account(f"Rpt Bank {currency} {tag}", "Bank Accounts", "Bank", cur)
	bank = frappe._dict(
		currency=currency,
		bank_gl=bank_gl,
		bank_account=ensure_bank_account(f"Rpt {currency} {tag}", bank_gl),
		pdc_payable=ensure_account(f"Rpt PDC Payable {tag}", "Current Liabilities", "Bank", cur),
		pdc_receivable=ensure_account(f"Rpt PDC Receivable {tag}", "Current Assets", "Bank", cur),
		under_collection=ensure_account(f"Rpt Under Collection {tag}", "Current Assets", "Bank", cur),
		bank_charges=ensure_account("Treasury Bank Charges", "Indirect Expenses"),
	)
	settings = frappe.get_doc("Treasury Company Settings", COMPANY)
	settings.append("pdc_accounts", mapping_row(bank))
	settings.save(ignore_permissions=True)
	return bank


def fund_bank(bank: frappe._dict, amount: float, posting_date, rate: float = 1.0) -> str:
	"""Opening money on the bank GL (amount in the bank currency) against Sales."""
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": COMPANY,
			"posting_date": posting_date,
			"multi_currency": 1 if bank.currency != "EGP" else 0,
			"accounts": [
				{
					"account": bank.bank_gl,
					"debit_in_account_currency": amount,
					"exchange_rate": rate,
				},
				{
					"account": acc("Sales"),
					"cost_center": acc("Main"),
					"credit_in_account_currency": flt(amount * rate, 2),
					"exchange_rate": 1,
				},
			],
		}
	)
	je.insert(ignore_permissions=True)
	je.submit()
	return je.name


def make_supplier() -> str:
	name = f"_Test Rpt Supplier {frappe.generate_hash(length=6)}"
	return (
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": name,
				"supplier_group": SUPPLIER_GROUP,
				"supplier_type": "Company",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def make_customer() -> str:
	name = f"_Test Rpt Customer {frappe.generate_hash(length=6)}"
	return (
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_group": CUSTOMER_GROUP,
				"territory": TERRITORY,
				"customer_type": "Company",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def outgoing(bank, amount, due_offset, deliver=True, **values):
	cheque = make_pdc_cheque(
		amount=amount,
		bank_account=bank.bank_account,
		issue_date=values.pop("issue_date", day(-40)),
		due_date=day(due_offset),
		**values,
	)
	if deliver:
		cheque.deliver(handover_date=day(-39))
	return cheque


def incoming(bank, amount, due_offset, **values):
	cheque = make_pdc_cheque(
		"Incoming",
		amount=amount,
		bank_account=bank.bank_account,
		issue_date=values.pop("issue_date", day(-40)),
		due_date=day(due_offset),
		**values,
	)
	cheque.receive(receipt_date=day(-39))
	return cheque


def period_rows(data: list[dict]) -> list[dict]:
	return [row for row in data if not row.get("indent")]


def fake_forecast_source(filters):
	"""A `treasury_forecast_sources` hook (Bank Facilities will register real ones)."""
	return [
		{
			"date": day(1),
			"direction": "out",
			"bank_account": filters.bank_accounts[0],
			"amount": 1234,
			"amount_in_account_currency": 1234,
			"reference_doctype": None,
			"reference_name": "FACILITY-1",
			"description": "Instalment",
		}
	]


class ReportTestCase(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		patcher = patch("treasury.pdc.report_utils.today", return_value=str(T))
		patcher.start()
		self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Bank Position Forecast
# ---------------------------------------------------------------------------


class TestBankPositionForecast(ReportTestCase):
	def make_scenario(self) -> frappe._dict:
		"""Opening 100,000; outgoing / incoming cheques at several due dates (see the expected rows below)."""
		bank = make_bank()
		fund_bank(bank, 100000, day(-30))
		cheques = frappe._dict(
			o1=outgoing(bank, 10000, 3),
			o2=outgoing(bank, 20000, 10),
			i1=incoming(bank, 15000, 5),
			i2_overdue=incoming(bank, 8000, -5),
			o3_cleared=outgoing(bank, 5000, -10),
			o4_cleared_in_range=outgoing(bank, 3000, 2),
			c1_created=outgoing(bank, 7000, 12, deliver=False),
		)
		cheques.o2.postpone(new_due_date=day(17), reason="Supplier agreed")
		cheques.o3_cleared.clear(clearance_date=day(-10))  # before From Date: part of the opening
		cheques.o4_cleared_in_range.clear(clearance_date=day(2))  # bank posted inside the range
		bank.cheques = cheques
		return bank

	def run_report(self, bank, **filters):
		report = BankPositionForecast(
			{
				"company": COMPANY,
				"bank_account": [bank.bank_account] if isinstance(bank, frappe._dict) else bank,
				"from_date": T,
				"to_date": add_days(T, 27),
				"periodicity": "Weekly",
				"currency_view": "Company Currency",
				**filters,
			}
		)
		columns, data, _message, chart, summary = report.run()
		return report, columns, data, chart, summary

	@staticmethod
	def figures(rows) -> list[tuple]:
		return [
			(r.opening, r.actual_in, r.actual_out, r.expected_in, r.expected_out, r.net, r.closing)
			for r in rows
		]

	def test_weekly_forecast(self):
		bank = self.make_scenario()
		report, columns, data, chart, summary = self.run_report(bank)
		rows = period_rows(data)

		self.assertEqual([r.row_id for r in rows], ["overdue", "p0", "p1", "p2", "p3"])
		self.assertEqual(
			self.figures(rows),
			[
				(95000, 0, 0, 8000, 0, 8000, 103000),  # overdue incoming, counted as expected today
				(103000, 0, 3000, 15000, 10000, 2000, 105000),  # week 1: o4 actual, i1 / o1 expected
				(105000, 0, 0, 0, 0, 0, 105000),  # week 2: o2's old date and the Created c1 are not counted
				(105000, 0, 0, 0, 20000, -20000, 85000),  # week 3: o2 on its postponed date
				(85000, 0, 0, 0, 0, 0, 85000),
			],
		)
		self.assertEqual((rows[1].period_start, rows[1].period_end), (T, getdate(add_days(T, 6))))

		values = report.summary_values
		self.assertEqual(values.current_balance, 95000)  # o4's bank posting (day 2) is after today
		self.assertEqual((values.total_expected_in, values.total_expected_out), (23000, 30000))
		self.assertEqual((values.lowest_balance, values.lowest_date), (85000, getdate(add_days(T, 20))))
		self.assertEqual(values.closing, 85000)
		self.assertEqual(len(summary), 5)

		self.assertEqual(chart["data"]["datasets"][0]["values"], [103000, 105000, 105000, 85000, 85000])
		self.assertIn("closing", [c["fieldname"] for c in columns])

	def test_bank_posted_cheques_only_in_actual(self):
		bank = self.make_scenario()
		_report, _columns, data, _chart, _summary = self.run_report(bank, show_details=1)
		details = [r for r in data if r.get("indent")]
		names = {r.reference_name for r in details}
		self.assertNotIn(bank.cheques.o3_cleared.name, names)
		self.assertNotIn(bank.cheques.o4_cleared_in_range.name, names)
		self.assertNotIn(bank.cheques.c1_created.name, names)
		self.assertEqual(
			{r.reference_name for r in details if r.parent_row_id == "p0"},
			{bank.cheques.o1.name, bank.cheques.i1.name},
		)
		# the postponed cheque appears only on its new date
		postponed = [r for r in details if r.reference_name == bank.cheques.o2.name]
		self.assertEqual([(r.parent_row_id, r.due_date) for r in postponed], [("p2", day(17))])
		self.assertEqual(
			[r.reference_name for r in details if r.parent_row_id == "overdue"], [bank.cheques.i2_overdue.name]
		)

	def test_include_created_and_overdue_toggles(self):
		bank = self.make_scenario()
		report, _c, data, _ch, _s = self.run_report(bank, include_created=1)
		rows = period_rows(data)
		self.assertEqual(rows[2].expected_out, 7000)  # c1 (Created) in week 2
		self.assertEqual([r.closing for r in rows], [103000, 105000, 98000, 78000, 78000])
		self.assertEqual(report.summary_values.lowest_balance, 78000)

		report, _c, data, _ch, _s = self.run_report(bank, include_overdue=0)
		rows = period_rows(data)
		self.assertEqual([r.row_id for r in rows], ["p0", "p1", "p2", "p3"])
		self.assertEqual([r.closing for r in rows], [97000, 97000, 77000, 77000])
		self.assertEqual(report.summary_values.total_expected_in, 15000)

	def test_daily_periods_sum_to_weekly(self):
		bank = self.make_scenario()
		_r, _c, weekly, _ch, _s = self.run_report(bank)
		_r, _c, daily, _ch, _s = self.run_report(bank, periodicity="Daily")
		weekly, daily = period_rows(weekly), period_rows(daily)
		self.assertEqual(len(daily), 1 + 28)  # overdue + one row per day
		first_week = daily[1:8]
		for field in ("actual_out", "expected_in", "expected_out"):
			self.assertEqual(sum(r[field] for r in first_week), weekly[1][field], field)
		self.assertEqual(daily[-1].closing, weekly[-1].closing)
		self.assertEqual(daily[3].actual_out, 3000)  # day 2: the in-range bank posting
		self.assertEqual(daily[4].expected_out, 10000)  # day 3: o1

		_r, _c, monthly, _ch, _s = self.run_report(bank, periodicity="Monthly")
		monthly = period_rows(monthly)
		self.assertEqual(monthly[-1].closing, weekly[-1].closing)

	def test_multi_bank_company_currency(self):
		egp = make_bank()
		usd = make_bank("USD")
		fund_bank(egp, 10000, day(-30))
		fund_bank(usd, 100, day(-30), rate=50)
		outgoing(usd, 20, 3, currency="USD", exchange_rate=52)
		incoming(egp, 500, 4)
		banks = [egp.bank_account, usd.bank_account]

		report, _c, data, _ch, _s = self.run_report(banks, to_date=add_days(T, 6))
		rows = period_rows(data)
		self.assertEqual(self.figures(rows), [(15000, 0, 0, 500, 1040, -540, 14460)])
		self.assertEqual(report.currency, "EGP")

		# bank currency view: one currency only
		self.assertRaises(
			frappe.ValidationError, self.run_report, banks, to_date=add_days(T, 6), currency_view="Bank Currency"
		)
		report, _c, data, _ch, _s = self.run_report(
			usd, to_date=add_days(T, 6), currency_view="Bank Currency"
		)
		self.assertEqual(report.currency, "USD")
		self.assertEqual(self.figures(period_rows(data)), [(100, 0, 0, 0, 20, -20, 80)])

	def test_forecast_source_hook(self):
		bank = self.make_scenario()
		with self.patch_hooks({"treasury_forecast_sources": ["treasury.tests.test_reports.fake_forecast_source"]}):
			_r, _c, data, _ch, _s = self.run_report(bank, show_details=1)
		rows = period_rows(data)
		self.assertEqual(rows[1].expected_out, 10000 + 1234)
		self.assertIn("FACILITY-1", {r.reference_name for r in data if r.get("indent")})

	def test_horizon_weeks_and_default_filters(self):
		bank = make_bank()
		report = BankPositionForecast({"company": COMPANY, "bank_account": [bank.bank_account], "horizon_weeks": 12})
		self.assertEqual(report.to_date, getdate(add_days(T, 83)))
		# defaults: today .. today + 90, weekly, company currency, every bank
		columns, data, _m, chart, summary = bank_position({"company": COMPANY})
		self.assertTrue(columns and data and chart and summary)
		self.assertRaises(frappe.ValidationError, bank_position, {"company": COMPANY, "periodicity": "Hourly"})


# ---------------------------------------------------------------------------
# Projected Party Statement
# ---------------------------------------------------------------------------


class TestProjectedPartyStatement(ReportTestCase):
	def run_report(self, supplier, **filters):
		report = ProjectedPartyStatement(
			{
				"company": COMPANY,
				"party_type": "Supplier",
				"party": supplier,
				"from_date": day(-90),
				"to_date": day(60),
				**filters,
			}
		)
		columns, data, _m, _chart, summary = report.run()
		return report, data, summary

	@staticmethod
	def rows(data, row_type):
		return [r for r in data if r.row_type == row_type]

	def test_on_cheque_date_delivered_not_posted(self):
		self.use_company_settings(outgoing_party_posting="On Cheque Date")
		supplier = make_supplier()
		pi = make_purchase_invoice(10000, posting_date=day(-60), supplier=supplier)
		later = make_pdc_cheque(amount=4000, party=supplier, issue_date=day(-30), due_date=day(20))
		overdue = make_pdc_cheque(amount=3000, party=supplier, issue_date=day(-30), due_date=day(-2))
		created = make_pdc_cheque(amount=1000, party=supplier, issue_date=day(-30), due_date=day(30))
		for cheque in (later, overdue):
			cheque.deliver(handover_date=day(-10))
			self.assertEqual(
				frappe.db.get_value("PDC Cheque", cheque.name, "accounting_status"), "Not Posted"
			)

		report, data, summary = self.run_report(supplier)
		self.assertEqual(self.rows(data, "opening")[0].balance, 0)
		self.assertEqual(
			[(r.voucher_no, r.debit, r.credit, r.balance) for r in self.rows(data, "actual")],
			[(pi.name, 0, 10000, -10000)],
		)
		self.assertEqual(self.rows(data, "actual_closing")[0].balance, -10000)
		self.assertEqual(
			[(r.voucher_no, r.posting_date, r.debit, r.projected_balance) for r in self.rows(data, "projected")],
			[(overdue.name, day(-2), 3000, -7000), (later.name, day(20), 4000, -3000)],
		)
		self.assertEqual(self.rows(data, "projected_closing")[0].projected_balance, -3000)

		values = report.summary_values
		self.assertEqual(values.actual_balance, -10000)
		self.assertEqual(values.unposted_cheques, 7000)
		self.assertEqual(values.posted_not_cleared, 0)
		self.assertEqual(values.balance_before_uncleared, -10000)
		self.assertEqual(values.net_after_cheques, -3000)
		self.assertEqual(len(summary), 5)

		# Created outgoing cheques on request
		report, data, _summary = self.run_report(supplier, include_created_outgoing=1)
		self.assertIn(created.name, [r.voucher_no for r in self.rows(data, "projected")])
		self.assertEqual(report.summary_values.net_after_cheques, -2000)

	def test_on_handover_posted_not_cleared(self):
		supplier = make_supplier()
		pi = make_purchase_invoice(10000, posting_date=day(-60), supplier=supplier)
		cleared = make_pdc_cheque(amount=2500, party=supplier, issue_date=day(-50), due_date=day(-5))
		cleared.deliver(handover_date=day(-40))
		cleared.clear(clearance_date=day(-5))
		posted = make_pdc_cheque(amount=4000, party=supplier, issue_date=day(-30), due_date=day(20))
		posted.deliver(handover_date=day(-10))

		report, data, _summary = self.run_report(supplier)
		cleared_pe = frappe.db.get_value("PDC Cheque", cleared.name, "party_payment_entry")
		posted_pe = frappe.db.get_value("PDC Cheque", posted.name, "party_payment_entry")
		self.assertEqual(
			[(r.posting_date, r.voucher_no, r.debit, r.credit, r.balance) for r in self.rows(data, "actual")],
			[
				(day(-60), pi.name, 0, 10000, -10000),
				(day(-40), cleared_pe, 2500, 0, -7500),
				(day(-10), posted_pe, 4000, 0, -3500),
			],
		)
		self.assertFalse(self.rows(data, "projected"))
		self.assertEqual(self.rows(data, "projected_closing")[0].projected_balance, -3500)

		values = report.summary_values
		self.assertEqual(values.actual_balance, -3500)
		self.assertEqual(values.unposted_cheques, 0)
		self.assertEqual(values.posted_not_cleared, 4000)  # the cleared cheque is not counted
		self.assertEqual(values.balance_before_uncleared, -7500)
		self.assertEqual(values.net_after_cheques, -3500)

	def test_opening_balance_and_validation(self):
		supplier = make_supplier()
		make_purchase_invoice(600, posting_date=day(-120), supplier=supplier)
		_report, data, _summary = self.run_report(supplier)
		self.assertEqual(self.rows(data, "opening")[0].balance, -600)
		self.assertFalse(self.rows(data, "actual"))

		columns, data, *_rest = projected_party_statement(
			{"company": COMPANY, "party_type": "Supplier", "party": supplier}
		)
		self.assertTrue(columns and data)
		self.assertRaises(
			frappe.ValidationError, projected_party_statement, {"company": COMPANY, "party_type": "Supplier"}
		)
		self.assertRaises(
			frappe.ValidationError,
			projected_party_statement,
			{"company": COMPANY, "party_type": "Employee", "party": supplier},
		)


# ---------------------------------------------------------------------------
# PDC Register
# ---------------------------------------------------------------------------


class TestPDCRegister(ReportTestCase):
	def make_cheques(self) -> frappe._dict:
		bank = make_bank()
		bank.r1 = outgoing(bank, 1000, 5)
		bank.r2 = outgoing(bank, 2000, 15)
		bank.r2.postpone(new_due_date=day(25), reason="Supplier agreed")
		bank.r3 = make_pdc_cheque(
			amount=3000, bank_account=bank.bank_account, issue_date=day(-10), due_date=day(-3)
		)
		bank.r4 = incoming(bank, 4000, 8)
		return bank

	def run_report(self, bank, **filters):
		columns, data = pdc_register({"company": COMPANY, "bank_account": bank.bank_account, **filters})
		cheques = [r for r in data if r.get("cheque")]
		return columns, data, cheques

	def test_filters(self):
		bank = self.make_cheques()
		_columns, data, cheques = self.run_report(bank)
		self.assertEqual({r.cheque for r in cheques}, {bank.r1.name, bank.r2.name, bank.r3.name, bank.r4.name})
		total = data[-1]
		self.assertEqual((total.is_total, total.cheque_count, total.base_amount), (1, 4, 10000))

		def names(**filters):
			return [r.cheque for r in self.run_report(bank, **filters)[2]]

		self.assertEqual(names(direction="Outgoing"), [bank.r3.name, bank.r1.name, bank.r2.name])
		self.assertEqual(names(postponed_only=1), [bank.r2.name])
		self.assertEqual(names(operational_status=["Created"]), [bank.r3.name])
		self.assertEqual(names(operational_status='["Received"]'), [bank.r4.name])
		self.assertEqual(
			set(names(accounting_status="Party Posted")), {bank.r1.name, bank.r2.name, bank.r4.name}
		)
		self.assertEqual(names(party_type="Customer", party=CUSTOMER), [bank.r4.name])
		# the original vs the effective (postponed) due date
		window = {"from_date": day(10), "to_date": day(20)}
		self.assertEqual(names(date_based_on="Original Due Date", **window), [bank.r2.name])
		self.assertEqual(names(date_based_on="Effective Due Date", **window), [])
		self.assertEqual(names(date_based_on="Issue Date", from_date=day(-10), to_date=day(-10)), [bank.r3.name])
		# the Bank Position Forecast drill-down
		self.assertEqual(set(names(direction="Outgoing", expected_only=1)), {bank.r1.name, bank.r2.name})
		self.assertEqual(
			set(names(direction="Outgoing", expected_only=1, include_created=1)),
			{bank.r1.name, bank.r2.name, bank.r3.name},
		)
		self.assertRaises(frappe.ValidationError, names, operational_status=["Lost"])

	def test_row_values(self):
		bank = self.make_cheques()
		rows = {r.cheque: r for r in self.run_report(bank)[2]}
		r2 = rows[bank.r2.name]
		self.assertEqual((r2.original_due_date, r2.effective_due_date), (day(15), day(25)))
		self.assertEqual((r2.days_to_due, r2.postponement_count), (25, 1))
		self.assertEqual(rows[bank.r3.name].days_to_due, -3)  # overdue
		self.assertIn(frappe.db.get_value("PDC Cheque", bank.r1.name, "party_payment_entry"), rows[bank.r1.name].vouchers)
		self.assertEqual(rows[bank.r4.name].base_amount, 4000)

	def test_group_by(self):
		bank = self.make_cheques()
		columns, data, _cheques = self.run_report(bank, group_by="Party")
		self.assertEqual(columns[0]["fieldname"], "group_label")
		groups = [r for r in data if r.get("is_group")]
		self.assertEqual(
			[(g.group_value, g.cheque_count, g.base_amount) for g in groups],
			[(CUSTOMER, 1, 4000), (SUPPLIER, 3, 6000)],
		)
		self.assertTrue(all(r.indent == 1 for r in data if r.get("cheque")))

		_columns, data, _cheques = self.run_report(bank, group_by="Operational Status")
		groups = {g.group_value: g.cheque_count for g in data if g.get("is_group")}
		self.assertEqual(groups, {"Created": 1, "Delivered": 2, "Received": 1})

	def test_default_filters(self):
		columns, data = pdc_register({"company": COMPANY})
		self.assertTrue(columns)
		self.assertTrue(data[-1].is_total)


# ---------------------------------------------------------------------------
# PDC Maturity
# ---------------------------------------------------------------------------


class TestPDCMaturity(ReportTestCase):
	def test_bucket_boundaries(self):
		bank = make_bank()
		# (days from As On to the effective due date, amount)
		for offset, amount in ((-1, 100), (0, 200), (7, 400), (8, 800), (30, 1600), (31, 3200), (61, 6400), (91, 12800)):
			make_pdc_cheque(amount=amount, bank_account=bank.bank_account, issue_date=day(-10), due_date=day(offset))
		# not open: bank posted
		outgoing(bank, 99999, -5).clear(clearance_date=day(-5))

		columns, data, _message, chart = pdc_maturity(
			{"company": COMPANY, "direction": "Outgoing", "bank_account": bank.bank_account, "as_on": T}
		)
		self.assertEqual(len(data), 2)  # the bank row + the total row
		row = data[0]
		self.assertEqual(row.group_label, bank.bank_account)
		self.assertEqual(
			(row.overdue, row.days_0_7, row.days_8_30, row.days_31_60, row.days_61_90, row.days_90_plus),
			(100, 600, 2400, 3200, 6400, 12800),
		)
		self.assertEqual((row.total, row.cheque_count), (25500, 8))
		self.assertEqual((data[1].is_total, data[1].total), (1, 25500))
		self.assertEqual(len(chart["data"]["datasets"]), 6)
		self.assertIn("overdue", [c["fieldname"] for c in columns])

		# one day later: 0 days -> overdue, 8 -> 7 (0-7), 31 -> 30 (8-30), 61 -> 60, 91 -> 90
		_c, data, _m, _ch = pdc_maturity(
			{"company": COMPANY, "direction": "Outgoing", "bank_account": bank.bank_account, "as_on": day(1)}
		)
		row = data[0]
		self.assertEqual(
			(row.overdue, row.days_0_7, row.days_8_30, row.days_31_60, row.days_61_90, row.days_90_plus),
			(300, 400 + 800, 1600 + 3200, 6400, 12800, 0),
		)

		# grouped by party
		_c, data, _m, _ch = pdc_maturity(
			{
				"company": COMPANY,
				"direction": "Outgoing",
				"bank_account": bank.bank_account,
				"as_on": T,
				"group_by": "Party",
			}
		)
		self.assertEqual((data[0].group_label, data[0].total), (SUPPLIER, 25500))

	def test_default_filters(self):
		columns, _data, _m, _chart = pdc_maturity({"company": COMPANY})
		self.assertTrue(columns)


# ---------------------------------------------------------------------------
# Bounced and Postponed Cheques
# ---------------------------------------------------------------------------


class TestBouncedAndPostponed(ReportTestCase):
	def test_bounced_view(self):
		self.use_company_settings(bounce_charges_account=self.data.egp.bank_charges)
		customer = make_customer()
		first = make_pdc_cheque("Incoming", amount=5000, party=customer, issue_date=day(-50), due_date=day(-20))
		first.receive(receipt_date=day(-40))
		first.bounce(bounce_date=day(-15), reason="Insufficient funds", charges=50)
		first.represent(represent_date=day(-12))
		second = make_pdc_cheque("Incoming", amount=2000, party=customer, issue_date=day(-50), due_date=day(-6))
		second.receive(receipt_date=day(-40))
		second.bounce(bounce_date=day(-5), reason="Signature mismatch")

		filters = {"company": COMPANY, "view": "Bounced", "party_type": "Customer", "party": customer}
		_columns, rows = bounced_and_postponed({**filters, "from_date": day(-30), "to_date": T})
		self.assertEqual(
			[(r.cheque, r.bounce_date, r.reason, r.charges, r.represented) for r in rows],
			[
				(first.name, day(-15), "Insufficient funds", 50, 1),
				(second.name, day(-5), "Signature mismatch", 0, 0),
			],
		)
		_columns, rows = bounced_and_postponed({**filters, "from_date": day(-10), "to_date": T})
		self.assertEqual([r.cheque for r in rows], [second.name])

	def test_postponed_view(self):
		customer = make_customer()
		first = make_pdc_cheque("Incoming", amount=1000, party=customer, issue_date=day(-20), due_date=day(10))
		first.receive(receipt_date=day(-19))
		first.postpone(new_due_date=day(20), reason="Customer request", contact_person="Mr X")
		first.postpone(new_due_date=day(30), reason="Customer request again")
		second = make_pdc_cheque("Incoming", amount=2000, party=customer, issue_date=day(-20), due_date=day(12))
		second.receive(receipt_date=day(-19))
		second.postpone(new_due_date=day(15), reason="Holiday")

		# postponements are dated when they were recorded (the real today)
		columns, rows = bounced_and_postponed(
			{
				"company": COMPANY,
				"view": "Postponed",
				"party_type": "Customer",
				"party": customer,
				"from_date": today(),
				"to_date": today(),
			}
		)
		self.assertCountEqual(
			[(r.cheque, r.old_due_date, r.new_due_date, r.days_postponed, r.reason) for r in rows],
			[
				(first.name, day(10), day(20), 10, "Customer request"),
				(first.name, day(20), day(30), 10, "Customer request again"),
				(second.name, day(12), day(15), 3, "Holiday"),
			],
		)
		self.assertEqual({r.party_postponements for r in rows}, {3})
		self.assertIn("Mr X", {r.contact_person for r in rows})
		self.assertIn("party_postponements", [c["fieldname"] for c in columns])

	def test_default_filters(self):
		for view in ("Bounced", "Postponed"):
			columns, _rows = bounced_and_postponed({"company": COMPANY, "view": view})
			self.assertTrue(columns)
		self.assertRaises(frappe.ValidationError, bounced_and_postponed, {"company": COMPANY, "view": "Lost"})


# ---------------------------------------------------------------------------
# Cheque Book Usage
# ---------------------------------------------------------------------------


class TestChequeBookUsage(ReportTestCase):
	def test_usage_numbers(self):
		bank = make_bank()
		book = make_cheque_book(1, 20, bank_account=bank.bank_account)
		cheques = [
			make_pdc_cheque(
				amount=100, bank_account=bank.bank_account, cheque_book=book.name, issue_date=day(-5)
			)
			for _i in range(3)
		]
		self.assertEqual([c.cheque_no for c in cheques], ["000001", "000002", "000003"])
		cancel_leaf(book.name, "5", "Spoilt")
		cheques[1].cancel_cheque(reason="Written wrongly")  # its leaf becomes a cancelled leaf

		columns, data = cheque_book_usage({"company": COMPANY, "bank_account": bank.bank_account})
		self.assertEqual(len(data), 1)
		row = data[0]
		self.assertEqual(row.cheque_book, book.name)
		self.assertEqual(row.leaf_range, "000001 - 000020")
		self.assertEqual(
			(row.total_leaves, row.used_leaves, row.cancelled_leaves, row.remaining_leaves),
			(20, 2, 2, 16),
		)
		self.assertEqual((row.next_no, row.status, row.used_percent), ("000004", "Active", 20.0))
		self.assertIn("used_percent", [c["fieldname"] for c in columns])

		_columns, data = cheque_book_usage(
			{"company": COMPANY, "bank_account": bank.bank_account, "status": "Closed"}
		)
		self.assertEqual(data, [])
		self.assertRaises(frappe.ValidationError, cheque_book_usage, {"company": COMPANY, "status": "Lost"})

	def test_default_filters(self):
		columns, _data = cheque_book_usage({"company": COMPANY})
		self.assertTrue(columns)


# ---------------------------------------------------------------------------
# Number card source
# ---------------------------------------------------------------------------


class TestDashboardCards(ReportTestCase):
	def test_cheque_total(self):
		def total(direction, scope):
			return dashboard.get_cheque_total({"company": COMPANY, "direction": direction, "scope": scope})

		due_before = total("Outgoing", "due_7_days")["value"]
		outstanding_before = total("Outgoing", "outstanding")["value"]
		receivable_before = total("Incoming", "outstanding")["value"]

		bank = make_bank()
		outgoing(bank, 700, 7)  # inside the 7-day window
		outgoing(bank, 300, 8)  # outside it
		make_pdc_cheque(amount=50, bank_account=bank.bank_account, issue_date=day(-5), due_date=day(1))
		incoming(bank, 900, 30)

		result = total("Outgoing", "due_7_days")
		self.assertEqual(flt(result["value"] - due_before, 2), 750)
		self.assertEqual(result["route"], ["query-report", "PDC Register"])
		self.assertEqual(result["route_options"]["to_date"], str(day(7)))
		self.assertEqual(flt(total("Outgoing", "outstanding")["value"] - outstanding_before, 2), 1050)
		self.assertEqual(flt(total("Incoming", "outstanding")["value"] - receivable_before, 2), 900)

		self.assertRaises(frappe.ValidationError, total, "Sideways", "outstanding")
		self.assertRaises(frappe.ValidationError, total, "Outgoing", "forever")

	def test_standard_records_exist(self):
		for doctype, names in (
			(
				"Number Card",
				(
					"Outgoing Cheques Due Today",
					"Incoming Cheques Due Today",
					"Overdue Outgoing Cheques",
					"Overdue Incoming Cheques",
					"Outgoing Cheques Due in 7 Days",
					"Incoming Cheques Due in 7 Days",
					"Outstanding PDC Payable",
					"Outstanding PDC Receivable",
				),
			),
			("Dashboard Chart", ("Expected Cash Flow", "PDC by Status")),
			("Workspace", ("Treasury",)),
			(
				"Report",
				(
					"Bank Position Forecast",
					"Projected Party Statement",
					"PDC Register",
					"PDC Maturity",
					"Bounced and Postponed Cheques",
					"Cheque Book Usage",
				),
			),
		):
			for name in names:
				self.assertTrue(frappe.db.exists(doctype, name), f"{doctype} {name}")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestReportPermissions(ReportTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(RESTRICTED_USER, ["Treasury User"])
		add_user_permission(RESTRICTED_USER, "Company", OTHER_COMPANY)
		ensure_user(ALLOWED_USER, ["Treasury User"])
		add_user_permission(ALLOWED_USER, "Company", COMPANY)

	def reports(self, supplier):
		company = {"company": COMPANY}
		return (
			(bank_position, company),
			(pdc_register, company),
			(pdc_maturity, company),
			(bounced_and_postponed, company),
			(cheque_book_usage, company),
			(projected_party_statement, {**company, "party_type": "Supplier", "party": supplier}),
			(dashboard.get_cheque_total, {"filters": {**company, "direction": "Outgoing", "scope": "outstanding"}}),
		)

	def test_forbidden_company(self):
		bank = make_bank()
		outgoing(bank, 1000, 5)
		with self.set_user(RESTRICTED_USER):
			for report, filters in self.reports(SUPPLIER):
				if "filters" in filters:
					self.assertRaises(frappe.PermissionError, report, **filters)
				else:
					self.assertRaises(frappe.PermissionError, report, filters)

	def test_permitted_company(self):
		bank = make_bank()
		cheque = outgoing(bank, 1000, 5)
		with self.set_user(ALLOWED_USER):
			_columns, data = pdc_register({"company": COMPANY, "bank_account": bank.bank_account})
			self.assertEqual([r.cheque for r in data if r.get("cheque")], [cheque.name])

	def test_user_without_access(self):
		user = ensure_user("treasury-rpt-noperm@example.com", [])
		with self.set_user(user):
			self.assertRaises(frappe.PermissionError, pdc_register, {"company": COMPANY})

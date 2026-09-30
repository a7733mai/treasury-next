"""Bank Facilities (Phase 5a, PLAN.md §8): facility & limits, drawdowns, repayment schedules, repayments.

Fixed dates in 2025 (a fiscal year `ensure_company` creates). Every test creates its own facility, so the
utilisation it checks (computed from the ledger through the Treasury source links) is its own; tests that
need a bank GL balance of their own use `make_own_bank`.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.bank_facilities import accounting, schedule
from treasury.bank_facilities import facility as lifecycle
from treasury.bank_facilities.repayment import lock_drawdown, refresh_repayment_status, row_status
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.tests.utils import (
	FACILITY_JE,
	TreasuryTestMixin,
	assert_gl_entries,
	bank_account_name,
	ensure_facility_accounts,
	ensure_user,
	get_voucher_count,
	make_bank_facility,
	make_drawdown,
	make_repayment,
)

D = getdate  # shorthand for fixed dates
USER = "treasury-facility-user@example.com"


def month_ends_2025():
	return [
		D(d)
		for d in (
			"2025-02-28",
			"2025-03-31",
			"2025-04-30",
			"2025-05-31",
			"2025-06-30",
			"2025-07-31",
			"2025-08-31",
			"2025-09-30",
			"2025-10-31",
			"2025-11-30",
			"2025-12-31",
			"2026-01-31",
		)
	]


class FacilityTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.egp_bank_gl = cls.data.egp.bank_gl
		cls.usd_bank_gl = cls.data.usd.bank_gl

	def setUp(self):
		super().setUp()
		# the 1.0 Journal Entry vouchers (the Payment Entry mode: test_facility_payment_entries)
		self.use_company_settings(accrue_facility_interest=0, **FACILITY_JE)


# ---------------------------------------------------------------------------
# Schedule arithmetic (pure functions)
# ---------------------------------------------------------------------------


class TestRepaymentSchedule(IntegrationTestCase):
	def build(self, **kwargs):
		values = dict(
			amount=120000,
			start_date="2025-01-31",
			tenor_months=12,
			frequency="Monthly",
			method=schedule.EQUAL_PRINCIPAL,
			rate=12,
			day_count="Actual/365",
		)
		values.update(kwargs)
		return schedule.build_schedule(**values)

	def test_equal_principal_and_month_end_dates(self):
		rows = self.build()
		self.assertEqual([r.due_date for r in rows], month_ends_2025())
		self.assertEqual({r.principal for r in rows}, {10000})
		self.assertEqual(rows[0].interest, 1104.66)  # 120,000 x 12% x 28 / 365
		self.assertEqual(rows[1].interest, 1121.10)  # 110,000 x 12% x 31 / 365
		self.assertEqual(rows[0].total, 11104.66)
		self.assertEqual(rows[-1].outstanding_after, 0)
		self.assertEqual(flt(sum(r.principal for r in rows), 2), 120000)

	def test_annuity(self):
		expected = flt(120000 * 0.01 / (1 - 1.01**-12), 2)
		self.assertEqual(expected, 10661.85)
		self.assertEqual(schedule.annuity_installment(120000, 12, 1, 12, 2), 10661.85)
		rows = self.build(method=schedule.ANNUITY)
		self.assertEqual({r.total for r in rows[:-1]}, {10661.85})
		self.assertEqual(flt(sum(r.principal for r in rows), 2), 120000)
		self.assertEqual(rows[-1].outstanding_after, 0)
		# principal of a row = installment - its day-count interest
		self.assertEqual(rows[0].principal, flt(10661.85 - 1104.66, 2))

	def test_rounding_is_exact(self):
		rows = self.build(amount=100000, tenor_months=3, rate=0)
		self.assertEqual([r.principal for r in rows], [33333.33, 33333.33, 33333.34])
		self.assertEqual({r.interest for r in rows}, {0})

	def test_bullet(self):
		rows = self.build(amount=100000, start_date="2025-01-15", tenor_months=6, frequency="Bullet", rate=10)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].due_date, D("2025-07-15"))
		self.assertEqual(rows[0].principal, 100000)
		self.assertEqual(rows[0].interest, 4958.90)  # 100,000 x 10% x 181 / 365

		rows = self.build(amount=100000, tenor_months=6, method=schedule.BULLET)
		self.assertEqual([r.principal for r in rows], [0, 0, 0, 0, 0, 100000])
		self.assertTrue(all(r.interest > 0 for r in rows))

	def test_grace_months(self):
		rows = self.build(grace_months=3)
		self.assertEqual([r.principal for r in rows[:3]], [0, 0, 0])
		self.assertEqual([r.principal for r in rows[3:-1]], [13333.33] * 8)
		self.assertEqual(rows[-1].principal, 13333.36)
		self.assertEqual(rows[0].interest, 1104.66)
		self.assertEqual(rows[2].outstanding_after, 120000)
		self.assertRaises(frappe.ValidationError, self.build, frequency="Quarterly", grace_months=2)
		self.assertRaises(frappe.ValidationError, self.build, grace_months=12)

	def test_murabaha_fixed_profit(self):
		rows = self.build(rate=0, profit_amount=12000, method=schedule.ANNUITY)
		self.assertEqual({r.interest for r in rows}, {1000})
		self.assertEqual({r.principal for r in rows}, {10000})
		rows = self.build(amount=90000, tenor_months=3, rate=0, profit_amount=1000)
		self.assertEqual([r.interest for r in rows], [333.33, 333.33, 333.34])

	def test_dates(self):
		rows = self.build(tenor_months=3, first_repayment_date="2025-04-30")
		self.assertEqual([r.due_date for r in rows], [D("2025-04-30"), D("2025-05-31"), D("2025-06-30")])
		rows = self.build(tenor_months=3, first_repayment_date="2025-01-30", start_date="2025-01-10")
		self.assertEqual([r.due_date for r in rows], [D("2025-01-30"), D("2025-02-28"), D("2025-03-30")])
		rows = self.build(tenor_months=6, frequency="Quarterly")
		self.assertEqual([r.due_date for r in rows], [D("2025-04-30"), D("2025-07-31")])
		self.assertRaises(frappe.ValidationError, self.build, tenor_months=10, frequency="Quarterly")
		self.assertRaises(frappe.ValidationError, self.build, first_repayment_date="2025-01-31")

	def test_actual_360(self):
		rows = self.build(day_count="Actual/360")
		self.assertEqual(rows[0].interest, flt(14400 * 28 / 360, 2))

	def test_annuity_last_row_actual_360(self):
		"""The installment uses the nominal monthly rate (1%), the rows' interest Actual/360 (more than 1% in a
		31-day month): the rows repay less principal and the last row, repaying the rest, is larger."""
		rows = self.build(method=schedule.ANNUITY, day_count="Actual/360")
		outstanding, previous, expected = 120000, D("2025-01-31"), []
		for index, due_date in enumerate(month_ends_2025()):
			interest = flt(flt(outstanding) * 12 / 100 * (due_date - previous).days / 360, 2)
			principal = outstanding if index == 11 else flt(10661.85 - interest, 2)
			expected.append((due_date, principal, interest))
			outstanding, previous = flt(outstanding - principal, 2), due_date
		self.assertEqual([(r.due_date, r.principal, r.interest) for r in rows], expected)
		self.assertEqual({r.total for r in rows[:-1]}, {10661.85})
		last = rows[-1]
		self.assertEqual(last.interest, flt(last.principal * 12 / 100 * 31 / 360, 2))
		self.assertEqual(last.total, flt(last.principal + last.interest, 2))
		self.assertGreater(last.total, 10661.85)
		self.assertEqual(flt(sum(r.principal for r in rows), 2), 120000)


# ---------------------------------------------------------------------------
# Bank Facility
# ---------------------------------------------------------------------------


class TestBankFacility(FacilityTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(USER, ["Treasury User"])

	def test_validations(self):
		loan = self.accounts.loan_egp
		cases = [
			# a sub-limit above the total
			dict(total_limit=100, limits=[{"limit_type": "Short Term Loan", "limit_amount": 200}]),
			# the same sub-limit twice
			dict(
				limits=[
					{"limit_type": "Short Term Loan", "limit_amount": 10},
					{"limit_type": "Short Term Loan", "limit_amount": 20},
				]
			),
			# a loan sub-limit without liability account
			dict(limits=[{"limit_type": "Murabaha", "limit_amount": 10, "liability_account": None}]),
			# a liability account in another currency
			dict(
				limits=[
					{
						"limit_type": "Short Term Loan",
						"limit_amount": 10,
						"liability_account": self.accounts.loan_usd,
					}
				]
			),
			# an overdraft without bank account
			dict(limits=[{"limit_type": "Overdraft", "limit_amount": 10}], default_bank_account=None),
			# a bank account in another currency
			dict(bank_account=bank_account_name("USD")),
			# expiry before start
			dict(start_date="2025-06-01", expiry_date="2025-05-01"),
			# an expense account as accrued interest account
			dict(accrued_interest_account=self.accounts.interest),
			# the accrued interest account is a sub-limit's liability account
			dict(accrued_interest_account=loan),
		]
		for values in cases:
			with self.subTest(values=values):
				self.assertRaises(frappe.ValidationError, make_bank_facility, activate=False, **values)

		# the sum of the sub-limits may exceed the total
		facility = make_bank_facility(
			total_limit=100,
			limits=[
				{"limit_type": "Short Term Loan", "limit_amount": 100, "liability_account": loan},
				{"limit_type": "Letter of Guarantee", "limit_amount": 100},
			],
			activate=False,
		)
		self.assertEqual(facility.status, "Draft")
		self.assertEqual([r.event for r in facility.status_log], ["Created"])

	def test_actions_and_status_log(self):
		facility = make_bank_facility(activate=False, start_date="2025-01-01", expiry_date="2025-12-31")
		facility.activate(activation_date="2025-01-05")
		facility.reload()
		self.assertEqual(facility.status, "Active")
		self.assertRaises(frappe.ValidationError, facility.activate, activation_date="2025-01-06")

		self.assertRaises(frappe.ValidationError, facility.suspend, suspension_date="2025-02-01")  # no reason
		self.assertRaises(
			frappe.ValidationError, facility.suspend, suspension_date="2025-01-02", reason="before activation"
		)
		facility.suspend(suspension_date="2025-02-01", reason="Covenant breach")
		facility.reload()
		self.assertEqual(facility.status, "Suspended")
		self.assertRaises(frappe.ValidationError, make_drawdown, facility, drawdown_date="2025-02-10")

		facility.activate(activation_date="2025-02-15")
		facility.renew(
			new_expiry_date="2026-12-31",
			renewal_date="2025-03-01",
			new_total_limit=2_000_000,
			new_limits={"Short Term Loan": 1_500_000},
		)
		facility.reload()
		self.assertEqual(facility.status, "Active")
		self.assertEqual(facility.expiry_date, D("2026-12-31"))
		self.assertEqual(facility.renewal_date, D("2025-03-01"))
		self.assertEqual((facility.total_limit, facility.limits[0].limit_amount), (2_000_000, 1_500_000))
		self.assertEqual(
			[(r.event, r.from_status, r.to_status, r.event_date) for r in facility.status_log],
			[
				("Created", None, "Draft", None),
				("Activated", "Draft", "Active", D("2025-01-05")),
				("Suspended", "Active", "Suspended", D("2025-02-01")),
				("Reactivated", "Suspended", "Active", D("2025-02-15")),
				("Renewed", "Active", "Active", D("2025-03-01")),
			],
		)
		# the new expiry must be after the current one
		self.assertRaises(frappe.ValidationError, facility.renew, new_expiry_date="2026-06-30")

		facility.close(closing_date="2025-03-02")
		facility.reload()
		self.assertEqual(facility.status, "Closed")

	def test_protected_and_locked_fields(self):
		facility = make_bank_facility()
		facility.status = "Closed"
		self.assertRaises(frappe.ValidationError, facility.save)
		facility.reload()
		facility.total_limit = 5
		self.assertRaises(frappe.ValidationError, facility.save)
		facility.reload()
		facility.limits[0].limit_amount = 5
		self.assertRaises(frappe.ValidationError, facility.save)
		facility.reload()
		# a new sub-limit and the notes may change
		facility.append("limits", {"limit_type": "Letter of Guarantee", "limit_amount": 1000})
		facility.collateral_notes = "Pledge of deposits"
		facility.save()
		# a client cannot set the status of a new facility
		new = make_bank_facility(activate=False, status="Active")
		self.assertEqual(new.status, "Draft")

	def test_accrued_interest_account_locked_once_drawn(self):
		facility = make_bank_facility()
		# no drawdown yet: the account may still change
		facility.accrued_interest_account = self.accounts.loan_egp_2
		facility.save()
		facility.accrued_interest_account = self.accounts.accrued_egp
		facility.save()
		drawdown = make_drawdown(facility)
		facility.reload()
		facility.accrued_interest_account = self.accounts.loan_egp_2
		self.assertRaises(frappe.ValidationError, facility.save)
		facility.reload()
		facility.accrued_interest_account = None
		self.assertRaises(frappe.ValidationError, facility.save)
		# once the drawdown is cancelled it is free again
		drawdown.reload()
		drawdown.cancel()
		facility.reload()
		facility.accrued_interest_account = self.accounts.loan_egp_2
		facility.save()

	def test_close_requires_nothing_outstanding(self):
		facility = make_bank_facility()
		drawdown = make_drawdown(facility, amount=1200, tenor_months=1)
		self.assertRaises(frappe.ValidationError, facility.close, closing_date="2025-02-01")
		make_repayment(drawdown, "2025-02-28")
		facility.close(closing_date="2025-03-01")
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "status"), "Closed")

	def test_treasury_user_cannot_run_actions(self):
		facility = make_bank_facility(activate=False)
		with self.set_user(USER):
			self.assertRaises(frappe.PermissionError, lifecycle.activate, facility.name, "2025-01-02")
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "status"), "Draft")

	def test_expiry_job_and_renewal(self):
		from treasury.bank_facilities.scheduler import refresh_facilities

		facility = make_bank_facility(expiry_date="2025-06-30")
		self.assertRaises(frappe.ValidationError, make_drawdown, facility, drawdown_date="2025-07-15")
		result = refresh_facilities(as_of="2025-07-01")
		self.assertIn(facility.name, result["expired"])
		facility.reload()
		self.assertEqual(facility.status, "Expired")
		self.assertEqual(facility.status_log[-1].event_date, D("2025-06-30"))
		self.assertNotIn(facility.name, refresh_facilities(as_of="2025-07-02")["expired"])

		facility.renew(new_expiry_date="2025-12-31", renewal_date="2025-07-10")
		facility.reload()
		self.assertEqual(facility.status, "Active")
		make_drawdown(facility, drawdown_date="2025-07-15")


# ---------------------------------------------------------------------------
# Drawdowns
# ---------------------------------------------------------------------------


class TestFacilityDrawdown(FacilityTestCase):
	def test_submit_posts_principal_and_fees(self):
		facility = make_bank_facility()
		drawdown = make_drawdown(facility, amount=100000, upfront_fees=1000, fees_account=self.accounts.fees)
		self.assertEqual(drawdown.bank_account, bank_account_name("EGP"))
		self.assertEqual(drawdown.liability_account, self.accounts.loan_egp)
		self.assertEqual(drawdown.interest_rate, 12)  # from the sub-limit
		self.assertEqual(len(drawdown.repayment_schedule), 12)
		self.assertEqual(drawdown.total_principal, 100000)
		date = D("2025-01-31")
		assert_gl_entries(
			self,
			"Journal Entry",
			drawdown.journal_entry,
			[(self.egp_bank_gl, 100000, 0, None, date), (self.accounts.loan_egp, 0, 100000, None, date)],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			drawdown.fees_journal_entry,
			[(self.accounts.fees, 1000, 0, None, date), (self.egp_bank_gl, 0, 1000, None, date)],
		)
		# the bank moved the money on the drawdown date: cleared, not left for bank reconciliation
		for journal in (drawdown.journal_entry, drawdown.fees_journal_entry):
			self.assertEqual(frappe.db.get_value("Journal Entry", journal, "clearance_date"), date)
		utilisation = get_utilisation(facility.name)
		self.assertEqual((utilisation.utilised, utilisation.available), (100000, 900000))
		self.assertEqual(utilisation.limits[0].available, 900000)
		self.assertEqual(utilisation.percent_used, 10)
		# display fields refreshed
		self.assertEqual(frappe.db.get_value("Bank Facility", facility.name, "total_utilised"), 100000)

		# idempotent
		vouchers = accounting.post_drawdown(drawdown)
		self.assertEqual(
			(vouchers.journal_entry, vouchers.fees_journal_entry, vouchers.payment_entry),
			(drawdown.journal_entry, drawdown.fees_journal_entry, None),
		)
		self.assertEqual(get_voucher_count("Facility Drawdown", drawdown.name), 2)

		drawdown.cancel()
		self.assertEqual(get_voucher_count("Facility Drawdown", drawdown.name, docstatus=1), 0)
		self.assertEqual(get_utilisation(facility.name).utilised, 0)

	def test_other_postings_on_the_liability_do_not_count(self):
		facility = make_bank_facility()
		make_drawdown(facility, amount=5000)
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": facility.company,
				"posting_date": "2025-02-01",
				"accounts": [
					{"account": self.egp_bank_gl, "debit_in_account_currency": 777},
					{"account": self.accounts.loan_egp, "credit_in_account_currency": 777},
				],
			}
		).insert(ignore_permissions=True)
		je.submit()
		self.assertEqual(get_utilisation(facility.name).utilised, 5000)

	def test_limit_checks(self):
		facility = make_bank_facility(
			total_limit=200000,
			limits=[
				{"limit_type": "Short Term Loan", "limit_amount": 150000},
				{"limit_type": "Long Term Loan", "limit_amount": 150000},
			],
		)
		make_drawdown(facility, amount=120000)
		# the sub-limit shares the total: 200,000 - 120,000 left
		self.assertRaises(
			frappe.ValidationError, make_drawdown, facility, amount=100000, limit_type="Long Term Loan"
		)
		self.assertRaises(frappe.ValidationError, make_drawdown, facility, amount=40000)  # 30,000 left
		make_drawdown(facility, amount=80000, limit_type="Long Term Loan")
		utilisation = get_utilisation(facility.name)
		self.assertEqual((utilisation.utilised, utilisation.available), (200000, 0))
		# no such sub-limit / not drawable through a drawdown
		self.assertRaises(frappe.ValidationError, make_drawdown, facility, amount=10, limit_type="Murabaha")
		self.assertRaises(frappe.ValidationError, make_drawdown, facility, amount=10, limit_type="Overdraft")

	def test_facility_period_and_status(self):
		facility = make_bank_facility(expiry_date="2025-06-30")
		self.assertRaises(frappe.ValidationError, make_drawdown, facility, drawdown_date="2025-07-01")
		self.assertRaises(frappe.ValidationError, make_drawdown, facility, drawdown_date="2024-12-31")
		draft = make_bank_facility(activate=False)
		self.assertRaises(frappe.ValidationError, make_drawdown, draft)

	def test_generate_schedule_and_manual_rows(self):
		facility = make_bank_facility()
		drawdown = make_drawdown(facility, amount=90000, tenor_months=3, submit=False)
		drawdown.repayment_schedule[0].principal = 50000
		drawdown.repayment_schedule[1].principal = 20000
		drawdown.repayment_schedule[2].principal = 20000
		drawdown.save()
		self.assertEqual(drawdown.repayment_schedule[0].outstanding_after, 40000)
		drawdown.repayment_schedule[2].principal = 1
		self.assertRaises(frappe.ValidationError, drawdown.save)  # no longer adds up to the amount
		drawdown.reload()
		drawdown.tenor_months = 6
		drawdown.generate_schedule()
		self.assertEqual([r.principal for r in drawdown.repayment_schedule], [15000] * 6)
		drawdown.save()
		drawdown.submit()
		self.assertRaises(frappe.ValidationError, drawdown.generate_schedule)

	def test_term_change_requires_a_new_schedule(self):
		facility = make_bank_facility()
		drawdown = make_drawdown(facility, submit=False)
		self.assertEqual(drawdown.repayment_schedule[0].interest, 1104.66)
		for fieldname, value in (
			("interest_rate", 20),
			("tenor_months", 6),
			("day_count", "Actual/360"),
			("first_repayment_date", D("2025-03-31")),
		):
			with self.subTest(fieldname=fieldname):
				drawdown.reload()
				drawdown.set(fieldname, value)
				self.assertRaises(frappe.ValidationError, drawdown.save)
		# the form clears the schedule on a term change: an empty schedule is rebuilt from the new terms
		drawdown.reload()
		drawdown.interest_rate = 20
		drawdown.set("repayment_schedule", [])
		drawdown.save()
		self.assertEqual(drawdown.repayment_schedule[0].interest, 1841.10)  # 120,000 x 20% x 28 / 365
		# a term changed on submit is refused as well
		drawdown.interest_rate = 15
		self.assertRaises(frappe.ValidationError, drawdown.submit)
		drawdown.reload()
		drawdown.submit()
		self.assertEqual(
			drawdown.total_interest, flt(sum(r.interest for r in drawdown.repayment_schedule), 2)
		)

	def test_system_fields_sent_on_submit_are_ignored(self):
		facility = make_bank_facility()
		drawdown = make_drawdown(facility, submit=False)
		drawdown.last_accrual_date = D("2025-06-30")
		drawdown.repayment_status = "Repaid"
		drawdown.schedule_version = 7
		row = drawdown.repayment_schedule[0]
		row.paid_principal, row.paid_interest, row.status, row.is_prepayment = 10000, 1104.66, "Paid", 1
		drawdown.submit()
		stored = frappe.db.get_value(
			"Facility Drawdown",
			drawdown.name,
			["last_accrual_date", "repayment_status", "schedule_version"],
			as_dict=True,
		)
		self.assertEqual(
			(stored.last_accrual_date, stored.repayment_status, stored.schedule_version), (None, "Open", 0)
		)
		row = frappe.get_all(
			"Facility Repayment Schedule",
			filters={"parent": drawdown.name, "installment_no": 1},
			fields=["paid_principal", "paid_interest", "status", "is_prepayment"],
		)[0]
		self.assertEqual(
			(row.paid_principal, row.paid_interest, row.status, row.is_prepayment), (0, 0, "Pending", 0)
		)

		# the repayment's computed fields as well
		repayment = make_repayment(drawdown, "2025-02-28", submit=False)
		repayment.submission_seq = 99
		repayment.accrued_interest_cleared = 1104.66
		repayment.prepayment_amount = 5000
		repayment.submit()
		stored = frappe.db.get_value(
			"Facility Repayment",
			repayment.name,
			["submission_seq", "accrued_interest_cleared", "interest_expensed", "prepayment_amount"],
			as_dict=True,
		)
		self.assertEqual(
			(
				stored.submission_seq,
				stored.accrued_interest_cleared,
				stored.interest_expensed,
				stored.prepayment_amount,
			),
			(1, 0, 1104.66, 0),
		)

	def test_usd_facility_on_usd_bank(self):
		facility = make_bank_facility(currency="USD", total_limit=100000)
		drawdown = make_drawdown(facility, amount=10000, upfront_fees=100, fees_account=self.accounts.fees)
		self.assertEqual(drawdown.exchange_rate, 50)
		assert_gl_entries(
			self,
			"Journal Entry",
			drawdown.journal_entry,
			[(self.usd_bank_gl, 500000, 0), (self.accounts.loan_usd, 0, 500000)],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			drawdown.fees_journal_entry,
			[(self.accounts.fees, 5000, 0), (self.usd_bank_gl, 0, 5000)],
		)
		self.assertEqual(get_utilisation(facility.name).utilised, 10000)  # facility currency

		# repayment of installment 1 at another rate: 833.33 principal + 92.05 interest (10,000 x 12% x 28 / 365)
		repayment = make_repayment(drawdown, "2025-02-28", exchange_rate=52)
		self.assertEqual((repayment.principal_paid, repayment.interest_paid), (833.33, 92.05))
		assert_gl_entries(
			self,
			"Journal Entry",
			repayment.journal_entry,
			[
				(self.accounts.loan_usd, 43333.16, 0),
				(self.accounts.interest, 4786.60, 0),
				(self.usd_bank_gl, 0, 48119.76),
			],
		)
		self.assertEqual(get_utilisation(facility.name).utilised, 9166.67)


# ---------------------------------------------------------------------------
# Repayments
# ---------------------------------------------------------------------------


class TestFacilityRepayment(FacilityTestCase):
	def make_loan(self, **values):
		facility = make_bank_facility()
		return facility, make_drawdown(facility, **values)

	def rows(self, drawdown):
		drawdown.reload()
		return sorted(drawdown.repayment_schedule, key=lambda r: (r.due_date, -r.is_prepayment))

	def test_repayment_without_accrual(self):
		facility, drawdown = self.make_loan()
		repayment = make_repayment(drawdown, "2025-02-28")  # nothing entered: the next installment
		self.assertEqual((repayment.principal_paid, repayment.interest_paid), (10000, 1104.66))
		self.assertEqual((repayment.accrued_interest_cleared, repayment.interest_expensed), (0, 1104.66))
		self.assertEqual(repayment.total_paid, 11104.66)
		date = D("2025-02-28")
		assert_gl_entries(
			self,
			"Journal Entry",
			repayment.journal_entry,
			[
				(self.accounts.loan_egp, 10000, 0, None, date),
				(self.accounts.interest, 1104.66, 0, None, date),
				(self.egp_bank_gl, 0, 11104.66, None, date),
			],
		)
		self.assertEqual(
			frappe.db.get_value("Journal Entry", repayment.journal_entry, "clearance_date"), date
		)
		first = self.rows(drawdown)[0]
		self.assertEqual((first.paid_principal, first.paid_interest, first.status), (10000, 1104.66, "Paid"))
		self.assertEqual(first.payment_journal, repayment.journal_entry)
		self.assertEqual(get_utilisation(facility.name).utilised, 110000)
		# idempotent
		self.assertEqual(
			accounting.post_repayment(repayment, drawdown, facility), ("Journal Entry", repayment.journal_entry)
		)
		self.assertEqual(get_voucher_count("Facility Repayment", repayment.name), 1)

	def test_partial_payment_and_status(self):
		facility, drawdown = self.make_loan()
		make_repayment(drawdown, "2025-02-20", principal_paid=4000, interest_paid=1104.66)
		refresh_repayment_status(drawdown.name, as_of="2025-02-25")
		self.assertEqual(self.rows(drawdown)[0].status, "Partially Paid")
		refresh_repayment_status(drawdown.name, as_of="2025-03-05")
		self.assertEqual(self.rows(drawdown)[0].status, "Overdue")
		self.assertEqual(row_status(self.rows(drawdown)[1], "2025-03-05"), "Pending")

		make_repayment(drawdown, "2025-03-01", principal_paid=6000, interest_paid=0)
		self.assertEqual(self.rows(drawdown)[0].status, "Paid")
		self.assertEqual(get_utilisation(facility.name).utilised, 110000)

		# more principal than unpaid without Prepayment; a back-dated repayment
		self.assertRaises(
			frappe.ValidationError, make_repayment, drawdown, "2025-03-02", principal_paid=500000
		)
		self.assertRaises(frappe.ValidationError, make_repayment, drawdown, "2025-02-27", principal_paid=1)

	def test_selected_row_and_penalty(self):
		_facility, drawdown = self.make_loan()
		repayment = make_repayment(
			drawdown,
			"2025-03-31",
			allocation_mode="Selected Row",
			installment_no=2,
			penalty=250,
			penalty_account=self.accounts.penalty,
		)
		self.assertEqual((repayment.principal_paid, repayment.interest_paid), (10000, 1121.10))
		self.assertEqual([a.installment_no for a in repayment.allocations], [2])
		assert_gl_entries(
			self,
			"Journal Entry",
			repayment.journal_entry,
			[
				(self.accounts.loan_egp, 10000, 0),
				(self.accounts.interest, 1121.10, 0),
				(self.accounts.penalty, 250, 0),
				(self.egp_bank_gl, 0, 11371.10),
			],
		)
		rows = self.rows(drawdown)
		self.assertEqual((rows[0].paid_principal, rows[1].status), (0, "Paid"))

	def test_prepayment_reduces_later_installments(self):
		facility, drawdown = self.make_loan()
		repayment = make_repayment(
			drawdown, "2025-02-28", principal_paid=40000, interest_paid=1104.66, prepayment=1
		)
		self.assertEqual(repayment.prepayment_amount, 30000)
		assert_gl_entries(
			self,
			"Journal Entry",
			repayment.journal_entry,
			[
				(self.accounts.loan_egp, 40000, 0),
				(self.accounts.interest, 1104.66, 0),
				(self.egp_bank_gl, 0, 41104.66),
			],
		)
		rows = self.rows(drawdown)
		self.assertEqual(len(rows), 13)
		prepayment = [r for r in rows if r.is_prepayment]
		self.assertEqual(
			[(r.principal, r.paid_principal, r.status) for r in prepayment], [(30000, 30000, "Paid")]
		)
		installments = [r for r in rows if not r.is_prepayment]
		self.assertEqual(installments[0].principal, 10000)
		self.assertEqual([r.principal for r in installments[1:-1]], [7272.73] * 10)
		self.assertEqual(installments[-1].principal, 7272.70)
		self.assertEqual(installments[1].interest, 815.34)  # 80,000 x 12% x 31 / 365
		self.assertEqual(installments[2].interest, 717.31)  # 72,727.27 x 12% x 30 / 365
		self.assertEqual(installments[-1].due_date, D("2026-01-31"))  # the tenor is kept
		self.assertEqual(flt(sum(r.principal for r in rows), 2), 120000)
		self.assertEqual(rows[-1].outstanding_after, 0)
		self.assertEqual(get_utilisation(facility.name).utilised, 80000)

		# cancel restores the schedule
		repayment.cancel()
		rows = self.rows(drawdown)
		self.assertEqual(len(rows), 12)
		self.assertEqual({r.principal for r in rows}, {10000})
		self.assertEqual(rows[1].interest, 1121.10)
		self.assertEqual({(r.paid_principal, r.paid_interest) for r in rows}, {(0, 0)})
		self.assertEqual({r.repayment for r in rows}, {None})
		self.assertEqual(get_voucher_count("Facility Repayment", repayment.name, docstatus=1), 0)
		self.assertEqual(get_utilisation(facility.name).utilised, 120000)

	def test_prepayment_beyond_the_schedule_is_refused(self):
		_facility, drawdown = self.make_loan(amount=20000, tenor_months=2)
		self.assertRaises(
			frappe.ValidationError,
			make_repayment,
			drawdown,
			"2025-02-28",
			principal_paid=30000,
			interest_paid=0,
			prepayment=1,
		)

	def test_cancel_order_and_drawdown_cancel(self):
		_facility, drawdown = self.make_loan()
		first = make_repayment(drawdown, "2025-02-28")
		second = make_repayment(drawdown, "2025-03-31")
		self.assertRaises(frappe.ValidationError, drawdown.cancel)
		self.assertRaises(frappe.ValidationError, first.cancel)
		second.cancel()
		rows = self.rows(drawdown)
		self.assertEqual((rows[0].status, rows[1].paid_principal), ("Paid", 0))
		self.assertEqual(rows[0].repayment, first.name)
		first.reload()
		first.cancel()
		self.assertEqual(self.rows(drawdown)[0].paid_principal, 0)
		drawdown.reload()
		drawdown.cancel()

	def test_cancel_order_follows_submission_not_creation(self):
		_facility, drawdown = self.make_loan()
		# drafts prepared in advance, submitted in the reverse order, on the same date
		created_first = make_repayment(
			drawdown, "2025-02-28", submit=False, principal_paid=10000, interest_paid=0
		)
		created_second = make_repayment(
			drawdown, "2025-02-28", submit=False, principal_paid=5000, interest_paid=0
		)
		created_second.submit()
		created_first.submit()
		self.assertEqual((created_second.submission_seq, created_first.submission_seq), (1, 2))
		rows = self.rows(drawdown)
		self.assertEqual((rows[0].paid_principal, rows[1].paid_principal), (10000, 5000))

		# the one submitted first cannot be cancelled while the later one is submitted
		self.assertRaises(frappe.ValidationError, created_second.cancel)
		rows = self.rows(drawdown)
		self.assertEqual((rows[0].paid_principal, rows[1].paid_principal), (10000, 5000))

		created_first.cancel()
		rows = self.rows(drawdown)
		self.assertEqual((rows[0].paid_principal, rows[1].paid_principal), (5000, 0))
		self.assertEqual(rows[0].repayment, created_second.name)
		created_second.reload()
		created_second.cancel()
		rows = self.rows(drawdown)
		self.assertEqual({(r.paid_principal, r.paid_interest) for r in rows}, {(0, 0)})
		self.assertEqual(get_utilisation(drawdown.facility).utilised, 120000)

		# a later submission gets the next number, cancelled ones included
		third = make_repayment(drawdown, "2025-02-28")
		self.assertEqual(third.submission_seq, 3)
		self.assertEqual(frappe.db.get_value("Facility Drawdown", drawdown.name, "schedule_version"), 5)

	def test_stale_reads_are_detected_under_the_lock(self):
		"""What was read before the lock (snapshot) differs from the locking read: another transaction
		committed a repayment / accrual (drawdown) or a drawdown / action (facility) meanwhile."""
		original = frappe.db.get_value

		def reads(snapshot, current):
			"""The two reads of the check return `snapshot` then `current`; other reads are real."""
			queue = [snapshot, current]
			return lambda *args, **kwargs: queue.pop(0) if queue else original(*args, **kwargs)

		facility, drawdown = self.make_loan()
		stale = reads(
			frappe._dict(schedule_version=1, last_accrual_date=None),
			frappe._dict(schedule_version=2, last_accrual_date=None),
		)
		with patch.object(frappe.db, "get_value", side_effect=stale):
			self.assertRaises(frappe.ValidationError, lock_drawdown, drawdown.name)
		lock_drawdown(drawdown.name)  # consistent: no error

		draft = make_drawdown(facility, amount=1000, submit=False)
		stale = reads(frappe._dict(modified=1, status="Active"), frappe._dict(modified=2, status="Active"))
		with patch.object(frappe.db, "get_value", side_effect=stale):
			self.assertRaises(frappe.ValidationError, draft.lock_facility)
		draft.lock_facility()

	def test_full_repayment_marks_the_drawdown_repaid(self):
		_facility, drawdown = self.make_loan(amount=2400, tenor_months=2)
		make_repayment(drawdown, "2025-02-28")
		self.assertEqual(frappe.db.get_value("Facility Drawdown", drawdown.name, "repayment_status"), "Open")
		make_repayment(drawdown, "2025-03-31")
		self.assertEqual(
			frappe.db.get_value("Facility Drawdown", drawdown.name, "repayment_status"), "Repaid"
		)

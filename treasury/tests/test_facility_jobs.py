"""Bank Facilities jobs (Phase 5a): month-end interest accrual, daily refresh and the alert digest.

Fixed dates in 2025. Each test creates its own facility / drawdowns and checks its own vouchers; the jobs
run over every drawdown of the company, so assertions only look at the test's own documents. Digests are
sent once per company, recipient and as-of day: every alert test uses its own as-of date.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.bank_facilities import interest, scheduler
from treasury.tests.utils import (
	COMPANY,
	TreasuryTestMixin,
	assert_gl_entries,
	ensure_facility_accounts,
	ensure_user,
	make_bank_facility,
	make_drawdown,
	make_repayment,
)

D = getdate
MANAGER = "treasury-facalert-manager@example.com"
USER = "treasury-facalert-user@example.com"
OUTSIDER = "treasury-facalert-outsider@example.com"


def accrual_journals(drawdown: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Journal Entry",
		filters={
			"treasury_source_doctype": "Facility Drawdown",
			"treasury_source_name": drawdown,
			"treasury_event": ["like", "interest_accrual:%"],
			"docstatus": 1,
		},
		fields=["name", "posting_date", "treasury_event", "total_debit"],
		order_by="posting_date asc",
	)


class FacilityJobTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=1)


class TestInterestAccrual(FacilityJobTestCase):
	def loan(self, facility=None, **values):
		facility = facility or make_bank_facility()
		return make_drawdown(facility, drawdown_date="2025-01-15", **values)

	def test_month_end_accrual_actual_365(self):
		drawdown = self.loan()
		# installment 1: 120,000 x 12% x 31 / 365 = 1,223.01, of which 16 of 31 days by 31 Jan
		self.assertEqual(drawdown.repayment_schedule[0].interest, 1223.01)
		posted = interest.accrue_drawdown(drawdown.name, "2025-02-01")
		self.assertEqual(len(posted), 1)
		date = D("2025-01-31")
		assert_gl_entries(
			self,
			"Journal Entry",
			posted[0],
			[
				(self.accounts.interest, 631.23, 0, None, date),
				(self.accounts.accrued_egp, 0, 631.23, None, date),
			],
		)
		event = frappe.db.get_value("Journal Entry", posted[0], "treasury_event")
		self.assertEqual(event, "interest_accrual:2025-01")
		self.assertEqual(frappe.db.get_value("Facility Drawdown", drawdown.name, "last_accrual_date"), date)
		# idempotent: a second run (and a run forced from the drawdown date again) posts nothing
		self.assertEqual(interest.accrue_drawdown(drawdown.name, "2025-02-01"), [])
		frappe.db.set_value("Facility Drawdown", drawdown.name, "last_accrual_date", None)
		self.assertEqual(interest.accrue_drawdown(drawdown.name, "2025-02-01"), [])
		self.assertEqual(len(accrual_journals(drawdown.name)), 1)

	def test_month_end_accrual_actual_360(self):
		drawdown = self.loan(day_count="Actual/360")
		self.assertEqual(drawdown.repayment_schedule[0].interest, 1240)  # 14,400 x 31 / 360
		posted = interest.accrue_drawdown(drawdown.name, "2025-02-01")
		assert_gl_entries(
			self,
			"Journal Entry",
			posted[0],
			[(self.accounts.interest, 640, 0), (self.accounts.accrued_egp, 0, 640)],  # 1,240 x 16 / 31
		)

	def test_catch_up_missed_months(self):
		drawdown = self.loan()
		posted = interest.accrue_drawdown(drawdown.name, "2025-03-05")
		self.assertEqual(
			[(j.posting_date, j.total_debit) for j in accrual_journals(drawdown.name)],
			# February: 1,223.01 + 1,012.60 x 13 / 28 earned by 28 Feb, less January's 631.23
			[(D("2025-01-31"), 631.23), (D("2025-02-28"), 1061.92)],
		)
		self.assertEqual(len(posted), 2)

	def test_repayment_clears_accrued_interest_first(self):
		drawdown = self.loan()
		interest.accrue_drawdown(drawdown.name, "2025-02-01")
		repayment = make_repayment(drawdown, "2025-02-15")
		self.assertEqual((repayment.principal_paid, repayment.interest_paid), (10000, 1223.01))
		self.assertEqual((repayment.accrued_interest_cleared, repayment.interest_expensed), (631.23, 591.78))
		assert_gl_entries(
			self,
			"Journal Entry",
			repayment.journal_entry,
			[
				(self.accounts.loan_egp, 10000, 0),
				(self.accounts.accrued_egp, 631.23, 0),
				(self.accounts.interest, 591.78, 0),
				(self.data.egp.bank_gl, 0, 11223.01),
			],
		)
		drawdown.reload()
		self.assertEqual(interest.accrued_balance(drawdown, "2025-02-15"), 0)
		# February: what was earned and not yet recognised (the expensed part counts as recognised)
		posted = interest.accrue_drawdown(drawdown.name, "2025-03-01")
		self.assertEqual(frappe.db.get_value("Journal Entry", posted[0], "total_debit"), 470.14)

	def test_job_isolation_and_setting(self):
		good = self.loan()
		broken = self.loan(facility=make_bank_facility(accrued=False))  # no accrued interest account
		result = interest.post_interest_accruals(as_of="2025-02-01")
		self.assertIn(broken.name, result["failed"])
		self.assertEqual(len(accrual_journals(good.name)), 1)
		self.assertEqual(accrual_journals(broken.name), [])
		self.assertIsNone(frappe.db.get_value("Facility Drawdown", broken.name, "last_accrual_date"))
		error = {"reference_doctype": "Facility Drawdown", "reference_name": broken.name}
		self.assertTrue(frappe.db.exists("Error Log", error))

		self.use_company_settings(accrue_facility_interest=0)
		other = self.loan()
		result = interest.post_interest_accruals(as_of="2025-03-01")
		self.assertEqual(result, {"posted": [], "failed": []})
		self.assertEqual(accrual_journals(other.name), [])

	def test_cancelling_the_drawdown_cancels_its_accruals(self):
		drawdown = self.loan()
		interest.accrue_drawdown(drawdown.name, "2025-02-01")
		drawdown.reload()
		drawdown.cancel()
		self.assertEqual(accrual_journals(drawdown.name), [])


class TestFacilityAlerts(FacilityJobTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(USER, ["Treasury User"])
		ensure_user(OUTSIDER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0)
		previous = frappe.db.get_single_value("Treasury Settings", "facility_alert_days")
		frappe.db.set_single_value("Treasury Settings", "facility_alert_days", 7)
		self.addCleanup(frappe.db.set_single_value, "Treasury Settings", "facility_alert_days", previous)

	def test_digest_content_and_recipients(self):
		facility = make_bank_facility(expiry_date="2025-03-20")
		drawdown = make_drawdown(facility, drawdown_date="2025-01-31")  # installments on month ends
		result = scheduler.send_facility_alerts(as_of="2025-02-25")
		digest = result[COMPANY]
		self.assertIn((drawdown.name, 1), digest["installments"])  # due 28 Feb
		self.assertNotIn((drawdown.name, 2), digest["installments"])  # due 31 Mar
		self.assertIn(facility.name, digest["facilities"])  # expires within 30 days
		self.assertIn(MANAGER, digest["recipients"])
		self.assertIn(USER, digest["recipients"])
		self.assertNotIn(OUTSIDER, digest["recipients"])

		content = frappe.get_all(
			"Notification Log",
			filters={"for_user": MANAGER, "document_type": "Facility Drawdown"},
			pluck="email_content",
			order_by="creation desc",
			limit=1,
		)[0]
		self.assertIn(drawdown.name, content)
		self.assertIn(facility.name, content)
		self.assertIn("Installments Due Soon", content)

		# the same day again: nothing new
		second = scheduler.send_facility_alerts(as_of="2025-02-25")
		self.assertEqual(second[COMPANY]["recipients"], [])

	def test_overdue_installments_and_paid_ones(self):
		facility = make_bank_facility()
		drawdown = make_drawdown(facility, drawdown_date="2025-01-31")
		make_repayment(drawdown, "2025-02-28")
		result = scheduler.send_facility_alerts(as_of="2025-04-02")
		installments = result[COMPANY]["installments"]
		self.assertNotIn((drawdown.name, 1), installments)  # paid
		self.assertIn((drawdown.name, 2), installments)  # overdue since 31 Mar
		self.assertNotIn((drawdown.name, 3), installments)  # due 30 Apr: after the 7 days

	def test_email_only_when_outgoing_email_is_set_up(self):
		facility = make_bank_facility()
		make_drawdown(facility, drawdown_date="2025-01-31")
		with (
			patch.object(scheduler.pdc_scheduler, "outgoing_email_enabled", return_value=True),
			patch("frappe.sendmail") as sendmail,
		):
			result = scheduler.send_facility_alerts(as_of="2025-02-24")
		emailed = [email for call in sendmail.call_args_list for email in call.kwargs["recipients"]]
		self.assertEqual(sorted(emailed), sorted(result[COMPANY]["recipients"]))

	def test_alert_days_default(self):
		frappe.db.delete("Singles", {"doctype": "Treasury Settings", "field": "facility_alert_days"})
		frappe.clear_document_cache("Treasury Settings", "Treasury Settings")
		self.assertEqual(scheduler.alert_days(), scheduler.DEFAULT_ALERT_DAYS)

	def test_daily_refresh_marks_overdue_rows(self):
		facility = make_bank_facility()
		drawdown = make_drawdown(facility, drawdown_date="2025-01-31")
		scheduler.refresh_facilities(as_of="2025-03-01")
		statuses = frappe.get_all(
			"Facility Repayment Schedule",
			filters={"parent": drawdown.name},
			pluck="status",
			order_by="due_date asc",
		)
		self.assertEqual(statuses[:2], ["Overdue", "Pending"])

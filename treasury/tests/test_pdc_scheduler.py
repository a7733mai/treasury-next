"""Daily PDC jobs: `On Cheque Date` party postings and due alerts (PLAN.md §4 Scheduler, §12)."""

import importlib.util
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.pdc import lifecycle, scheduler
from treasury.tests.utils import (
	COMPANY,
	DEFAULT_PDC_SETTINGS,
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	TreasuryTestMixin,
	assert_gl_entries,
	day,
	ensure_company,
	ensure_user,
	get_voucher_count,
	make_pdc_cheque,
)

DOCTYPE = "PDC Cheque"
MANAGER = "treasury-sched-manager@example.com"
USER = "treasury-sched-user@example.com"
OUTSIDER = "treasury-sched-outsider@example.com"
RESTRICTED = "treasury-sched-restricted@example.com"


class TestPDCScheduledPosting(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		self.use_company_settings(outgoing_party_posting="On Cheque Date")

	# ------------------------------------------------------------------ helpers

	def delivered(self, due: int, amount=1000, issue=-90, handover=-89):
		cheque = make_pdc_cheque(amount=amount, issue_date=day(issue), due_date=day(due))
		cheque.deliver(handover_date=day(handover))
		cheque.reload()
		return cheque

	def status(self, cheque) -> tuple[str, str]:
		cheque.reload()
		return cheque.operational_status, cheque.accounting_status

	def party_posting_date(self, cheque):
		cheque.reload()
		return getdate(frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "posting_date"))

	# ------------------------------------------------------------------ tests

	def test_posts_only_due_cheques(self):
		due = self.delivered(due=-5)
		not_due = self.delivered(due=5)
		self.assertEqual(self.status(due), ("Delivered", "Not Posted"))

		result = scheduler.post_due_party_postings(as_of=day(0))

		self.assertIn(due.name, result["posted"])
		self.assertNotIn(not_due.name, result["posted"] + result["failed"])
		self.assertEqual(self.status(due), ("Delivered", "Party Posted"))
		self.assertEqual(getdate(due.party_posting_date), day(-5))
		assert_gl_entries(
			self,
			"Payment Entry",
			due.party_payment_entry,
			[
				(self.data.payable, 1000, 0, SUPPLIER, day(-5)),
				(self.data.egp.pdc_payable, 0, 1000, None, day(-5)),
			],
		)
		log = due.status_log[-1]
		self.assertEqual((log.event, log.voucher_no), ("Party Posted", due.party_payment_entry))
		self.assertEqual(self.status(not_due), ("Delivered", "Not Posted"))
		self.assertEqual(get_voucher_count(DOCTYPE, not_due.name), 0)

	def test_postponed_cheque_waits_for_the_new_date(self):
		cheque = self.delivered(due=2)
		cheque.postpone(new_due_date=day(10), reason="Supplier agreed")

		scheduler.post_due_party_postings(as_of=day(5))
		self.assertEqual(self.status(cheque), ("Delivered", "Not Posted"))
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 0)

		scheduler.post_due_party_postings(as_of=day(10))
		self.assertEqual(self.status(cheque), ("Delivered", "Party Posted"))
		self.assertEqual(self.party_posting_date(cheque), day(10))

	def test_catch_up_posts_each_cheque_on_its_own_due_date(self):
		cheques = {offset: self.delivered(due=offset) for offset in (-3, -2, -1)}

		# the job did not run on the three due dates: one run catches them all up
		result = scheduler.post_due_party_postings(as_of=day(0))

		for offset, cheque in cheques.items():
			self.assertIn(cheque.name, result["posted"])
			self.assertEqual(self.status(cheque), ("Delivered", "Party Posted"))
			self.assertEqual(self.party_posting_date(cheque), day(offset))

	def test_cheque_handed_over_after_its_due_date_posts_on_the_handover(self):
		cheque = make_pdc_cheque(issue_date=day(-30), due_date=day(-20))
		cheque.deliver(handover_date=day(-10))

		scheduler.post_due_party_postings(as_of=day(0))

		self.assertEqual(self.status(cheque), ("Delivered", "Party Posted"))
		self.assertEqual(self.party_posting_date(cheque), day(-10))

	def test_incoming_cheque_date_mode(self):
		self.use_company_settings(
			incoming_party_posting="On Cheque Date", incoming_under_collection="Disabled"
		)
		received = make_pdc_cheque("Incoming", amount=700, issue_date=day(-40), due_date=day(-2))
		received.receive(receipt_date=day(-39))
		deposited = make_pdc_cheque("Incoming", amount=800, issue_date=day(-40), due_date=day(-1))
		deposited.receive(receipt_date=day(-39))
		deposited.deposit(deposit_date=day(-10))
		self.assertEqual(self.status(deposited), ("Deposited", "Not Posted"))

		scheduler.post_due_party_postings(as_of=day(0))

		self.assertEqual(self.status(received), ("Received", "Party Posted"))
		self.assertEqual(self.party_posting_date(received), day(-2))
		self.assertEqual(self.status(deposited), ("Deposited", "Party Posted"))
		self.assertEqual(self.party_posting_date(deposited), day(-1))

	def test_other_modes_are_untouched(self):
		self.use_company_settings(outgoing_party_posting="Manual")
		manual = self.delivered(due=-5)
		created = make_pdc_cheque(issue_date=day(-30), due_date=day(-5))  # never handed over
		# a company that switched to On Handover after the cheque was delivered unposted
		self.use_company_settings(outgoing_party_posting="On Handover")

		result = scheduler.post_due_party_postings(as_of=day(0))

		for cheque in (manual, created):
			self.assertNotIn(cheque.name, result["posted"] + result["failed"])
			self.assertEqual(self.status(cheque)[1], "Not Posted")
			self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 0)

	def test_one_failing_cheque_does_not_stop_the_others(self):
		bad = self.delivered(due=-4)
		good = self.delivered(due=-3)
		real_post_party = lifecycle._post_party

		def post_then_fail(doc, posting_date):
			real_post_party(doc, posting_date)  # the Payment Entry exists when it fails
			if doc.name == bad.name:
				frappe.throw("Simulated posting failure")

		with patch.object(lifecycle, "_post_party", side_effect=post_then_fail):
			result = scheduler.post_due_party_postings(as_of=day(0))

		self.assertIn(bad.name, result["failed"])
		self.assertIn(good.name, result["posted"])
		# the failing cheque is rolled back completely (its Payment Entry included)
		self.assertEqual(self.status(bad), ("Delivered", "Not Posted"))
		self.assertEqual(get_voucher_count(DOCTYPE, bad.name), 0)
		self.assertEqual(self.status(good), ("Delivered", "Party Posted"))
		self.assertTrue(
			frappe.db.exists("Error Log", {"method": ["like", f"%{bad.name}%"], "reference_name": bad.name})
		)

		# the next run posts it once the problem is gone
		scheduler.post_due_party_postings(as_of=day(0))
		self.assertEqual(self.status(bad), ("Delivered", "Party Posted"))
		self.assertEqual(self.party_posting_date(bad), day(-4))

	def test_running_twice_creates_nothing_new(self):
		cheque = self.delivered(due=-1)
		scheduler.post_due_party_postings(as_of=day(0))
		log_rows = len(frappe.get_doc(DOCTYPE, cheque.name).status_log)

		result = scheduler.post_due_party_postings(as_of=day(0))

		self.assertNotIn(cheque.name, result["posted"] + result["failed"])
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 1)
		self.assertEqual(len(frappe.get_doc(DOCTYPE, cheque.name).status_log), log_rows)

	def test_system_entry_point_skips_only_the_user_permission(self):
		cheque = self.delivered(due=-1)
		user = ensure_user("treasury-sched-noperm@example.com", [])
		frappe.set_user(user)
		try:
			self.assertRaises(frappe.PermissionError, lifecycle.post_party, cheque.name)
			# runs as Administrator whoever the session user is, and restores the session user
			lifecycle.post_party(cheque.name, system=True)
			self.assertEqual(frappe.session.user, user)
			# business rules still apply to the system entry point
			self.assertRaises(frappe.ValidationError, lifecycle.post_party, cheque.name, system=True)
			self.assertEqual(frappe.session.user, user)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.status(cheque), ("Delivered", "Party Posted"))
		cheque.reload()
		self.assertEqual(cheque.status_log[-1].event, "Party Posted")
		self.assertEqual(cheque.status_log[-1].user, "Administrator")

	def test_default_date_is_today(self):
		if not importlib.util.find_spec("freezegun"):
			self.skipTest("freezegun is not installed")
		from frappe.tests.classes.context_managers import freeze_time

		cheque = self.delivered(due=-1)
		with freeze_time(day(0)):
			scheduler.post_due_party_postings()
		self.assertEqual(self.status(cheque), ("Delivered", "Party Posted"))
		self.assertEqual(self.party_posting_date(cheque), day(-1))


class TestPDCDueAlerts(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(USER, ["Treasury User"])
		ensure_user(OUTSIDER, ["Accounts User"])
		ensure_user(RESTRICTED, ["Treasury User"])
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		if not frappe.db.exists("User Permission", {"user": RESTRICTED, "allow": "Company"}):
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": RESTRICTED,
					"allow": "Company",
					"for_value": OTHER_COMPANY,
				}
			).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		previous = frappe.db.get_single_value("Treasury Settings", "default_alert_days")
		frappe.db.set_single_value("Treasury Settings", "default_alert_days", 3)
		self.addCleanup(frappe.db.set_single_value, "Treasury Settings", "default_alert_days", previous)

	def test_digest_content_and_recipients(self):
		soon = make_pdc_cheque(amount=1111, issue_date=day(-30), due_date=day(2))
		soon.deliver(handover_date=day(-29))
		overdue = make_pdc_cheque("Incoming", amount=2222, issue_date=day(-30), due_date=day(-5))
		overdue.receive(receipt_date=day(-29))
		later = make_pdc_cheque(amount=3333, issue_date=day(-30), due_date=day(10))
		settled = make_pdc_cheque(amount=4444, issue_date=day(-30), due_date=day(-2))
		settled.deliver(handover_date=day(-29))
		settled.clear(clearance_date=day(-2))

		result = scheduler.send_due_alerts(as_of=day(0))

		digest = result[COMPANY]
		self.assertIn(soon.name, digest["cheques"])
		self.assertIn(overdue.name, digest["cheques"])
		self.assertNotIn(later.name, digest["cheques"])
		self.assertNotIn(settled.name, digest["cheques"])
		self.assertIn(MANAGER, digest["recipients"])
		self.assertIn(USER, digest["recipients"])
		self.assertNotIn(OUTSIDER, digest["recipients"])
		self.assertNotIn(RESTRICTED, digest["recipients"])

		logs = frappe.get_all(
			"Notification Log",
			filters={"for_user": MANAGER, "document_type": DOCTYPE},
			fields=["subject", "email_content", "type"],
			order_by="creation desc",
			limit=1,
		)
		self.assertTrue(logs)
		self.assertEqual(logs[0].type, "Alert")
		self.assertIn(COMPANY, logs[0].subject)
		content = logs[0].email_content
		for cheque in (soon, overdue):
			self.assertIn(cheque.cheque_no, content)
		self.assertNotIn(later.cheque_no, content)
		self.assertNotIn(settled.cheque_no, content)
		self.assertFalse(frappe.db.exists("Notification Log", {"for_user": OUTSIDER, "document_type": DOCTYPE}))

	def test_digest_groups_and_overdue_flag(self):
		soon = make_pdc_cheque(amount=1212, issue_date=day(-30), due_date=day(1))
		overdue = make_pdc_cheque("Incoming", amount=2323, issue_date=day(-30), due_date=day(-3))
		cheques = [
			frappe._dict(c)
			for c in frappe.get_all(
				DOCTYPE,
				filters={"name": ["in", [soon.name, overdue.name]]},
				fields=[
					"name",
					"direction",
					"cheque_no",
					"party",
					"party_name",
					"bank_account",
					"drawer_bank",
					"amount",
					"currency",
					"effective_due_date",
					"operational_status",
				],
			)
		]

		html = scheduler.render_digest(COMPANY, cheques, day(0))

		self.assertIn("Outgoing Cheques", html)
		self.assertIn("Incoming Cheques", html)
		self.assertLess(html.index("Outgoing Cheques"), html.index(soon.cheque_no))
		self.assertLess(html.index("Incoming Cheques"), html.index(overdue.cheque_no))
		overdue_row = html[html.index(overdue.cheque_no) :]
		self.assertIn("Overdue", overdue_row.split("</tr>")[0])
		soon_row = html[html.index(soon.cheque_no) :]
		self.assertNotIn("Overdue", soon_row.split("</tr>")[0])

	def test_no_digest_without_due_cheques(self):
		frappe.db.set_single_value("Treasury Settings", "default_alert_days", 0)
		# an as-of date before every fixture date: nothing is due yet
		result = scheduler.send_due_alerts(as_of=day(-3650))
		self.assertNotIn(COMPANY, result)

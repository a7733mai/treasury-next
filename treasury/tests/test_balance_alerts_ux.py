"""Phase 7e-2: the e-mail layouts (design 7e §3), the manager tools (P1 preview / test send / alert check, P7
recipients preview) and the thresholds helper of Treasury Company Settings (P3).

Dates are relative to the real today (treasury.tests.utils.make_alert_fixture_e1). E-mail is mocked
(`notify.outgoing_email_enabled`, `frappe.sendmail`) and `frappe.enqueue` is recorded, not queued.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.translate import print_language
from frappe.utils import fmt_money, formatdate, get_datetime, get_fullname, get_url, getdate

from treasury.balance_alerts import notify, recipients, scheduler
from treasury.balance_alerts.positions import apply_thresholds, evaluate
from treasury.tests.utils import (
	COMPANY,
	BalanceAlertTestMixin,
	add_user_permission,
	alert_logs,
	ensure_alert_bank,
	ensure_user,
	fund_bank,
	make_alert_fixture_e1,
	make_pdc_cheque,
	set_recipients,
)

MANAGER = "treasury-alerts-ux-mgr@example.com"
USER = "treasury-alerts-ux-user@example.com"
NO_ROLE_USER = "treasury-alerts-ux-norole@example.com"  # no roles: a Website User
# a System User without read on PDC Cheque
DESK_USER = "treasury-alerts-ux-desk@example.com"
# a Treasury Manager limited to one bank account (User Permission for the bank position reports)
LIMITED_MANAGER = "treasury-alerts-ux-limited@example.com"
DISABLED_USER = "treasury-alerts-ux-disabled@example.com"
LEVEL_CTX = "Bank Balance Outlook"


class TestBalanceAlertsUX(BalanceAlertTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(USER, ["Treasury User"])
		ensure_user(NO_ROLE_USER, [])
		ensure_user(DESK_USER, ["Stock User"])
		ensure_user(LIMITED_MANAGER, ["Treasury Manager"])
		ensure_user(DISABLED_USER, ["Treasury Manager"])
		frappe.db.set_value("User", DISABLED_USER, "enabled", 0)

	def setUp(self):
		super().setUp()
		set_recipients(self, [{"user": MANAGER}])

	# ------------------------------------------------------------------ helpers

	@contextmanager
	def mail(self, outgoing: bool = True):
		with (
			patch("treasury.balance_alerts.notify.outgoing_email_enabled", return_value=outgoing),
			patch.object(notify.frappe, "sendmail") as sendmail,
		):
			yield sendmail

	def run_company(self, hour: int = 10, digest: bool = False):
		now = get_datetime(f"{self.today} {hour:02d}:00:00")
		with self.mail() as sendmail:
			result = scheduler.evaluate_company(COMPANY, now, send_digest=digest)
		return result, sendmail

	def mail_to(self, sendmail, user: str) -> list[str]:
		email = frappe.db.get_value("User", user, "email")
		return [c.kwargs["message"] for c in sendmail.call_args_list if c.kwargs.get("recipients") == [email]]

	def limit_to_bank(self, bank_account: str):
		"""LIMITED_MANAGER may see only `bank_account` in the bank position reports (and nothing else of the
		previous tests of the class)."""
		for name in frappe.get_all("User Permission", filters={"user": LIMITED_MANAGER}, pluck="name"):
			frappe.delete_doc("User Permission", name, ignore_permissions=True)
		add_user_permission(LIMITED_MANAGER, "Bank Account", bank_account, applicable_for="PDC Cheque")

	# ------------------------------------------------------------------ digest

	def test_digest_subject_variants(self):
		# design 7e P10: a calm subject for a calm day
		self.assertEqual(
			notify.digest_subject(COMPANY, self.today, 0, 0),
			_("Bank position for {0} on {1}: all banks above their thresholds").format(
				COMPANY, formatdate(self.today)
			),
		)
		self.assertEqual(
			notify.digest_subject(COMPANY, self.today, 1, 2),
			_("Bank position for {0} on {1}: {2} critical, {3} warning").format(
				COMPANY, formatdate(self.today), 1, 2
			),
		)

	def test_digest_layout(self):
		e1 = make_alert_fixture_e1(self)
		plain = ensure_alert_bank()
		fund_bank(plain, 500, self.day(-1))
		positions = evaluate(COMPANY, [e1.bank_account, plain.bank_account])
		html = notify.render_digest(COMPANY, positions, self.today, 7, receive_as="Treasury Manager")

		# pills: the counts that are not zero
		self.assertIn(_("{0} Critical").format(1), html)
		self.assertIn(_("{0} No Threshold").format(1), html)
		self.assertNotIn(_("{0} Warning").format(0), html)
		# the worst bank first; a bank without thresholds is one line, without a day table (P12)
		self.assertLess(html.index(e1.bank_account), html.index(plain.bank_account))
		self.assertEqual(html.count("<table"), 1)
		money = fmt_money(500, currency="EGP")
		self.assertIn(
			_("Today {0} · lowest {1} on {2}").format(money, money, notify.email_date(self.today)), html
		)
		# P11: the lowest balance of each bank, P9: dates with their weekday
		lowest = fmt_money(-10020, currency="EGP")
		self.assertIn(_("Lowest: {0} on {1}").format(lowest, notify.email_date(self.day(5))), html)
		self.assertIn(notify.email_date(self.day(1)), html)
		self.assertNotEqual(notify.email_date(self.day(1)), formatdate(self.day(1)))
		# empty In / Out cells stay blank
		section = html[html.index(e1.bank_account) : html.index("</table>")]
		self.assertNotIn(fmt_money(0, currency="EGP"), section)
		# P13: the company links to Bank Balance Outlook; the footer says why the user receives it
		self.assertIn(get_url(notify.outlook_route(COMPANY)), html)
		self.assertIn(_("You receive this as {0}.").format("Treasury Manager"), html)
		self.assertIn('dir="ltr"', html)
		self.assertIn("→", html)

		with print_language("ar"):
			arabic = notify.render_digest(COMPANY, positions, self.today, 7)
		self.assertIn('dir="rtl"', arabic)
		self.assertIn("←", arabic)

	def test_digest_escapes_values(self):
		position = synthetic_position("<b>Bank & Co</b>")
		html = notify.render_digest("<i>Co</i>", [position], self.today, 1)
		self.assertNotIn("<b>Bank", html)
		self.assertNotIn("<i>Co", html)
		self.assertIn("&lt;b&gt;Bank &amp; Co&lt;/b&gt;", html)
		summary = notify.digest_summary([position])
		self.assertNotIn("<b>Bank", summary)

	# ------------------------------------------------------------------ alert

	def test_alert_content(self):
		e1 = make_alert_fixture_e1(self)
		_result, sendmail = self.run_company()
		(html,) = self.mail_to(sendmail, MANAGER)
		(position,) = evaluate(COMPANY, [e1.bank_account])
		critical = next(b for b in position.breaches if b.level == "Critical")

		self.assertIn(notify.key_sentence(position, critical), html)
		self.assertIn(_("Shortfall {0}").format(fmt_money(30020, currency="EGP")), html)
		self.assertEqual(html.count('class="treasury-level-'), 4)
		# the largest payments up to the first due breach (T+3), with their documents
		first_breach = notify.email_date(self.day(3), long=True)
		self.assertIn(_("Largest payments up to {0}").format(first_breach), html)
		self.assertIn(notify.form_url("Bank Transfer", e1.tr1), html)
		# design 7e P14: a cheque's party Payment Entry, the paper-filing key
		payment_entry = frappe.db.get_value("PDC Cheque", e1.c1, "party_payment_entry")
		self.assertTrue(payment_entry)
		self.assertIn(_("PE {0}").format(payment_entry), html)
		self.assertIn("treasury-forecast-link", html)

		# the bell: the key sentence, one line per due breach, the largest payment
		(content,) = frappe.get_all(
			"Notification Log",
			filters={"for_user": MANAGER, "document_type": "Bank Account", "document_name": e1.bank_account},
			pluck="email_content",
		)
		self.assertIn(notify.key_sentence(position, critical), content)
		self.assertEqual(content.count("<br>"), 5)
		self.assertIn(
			_("Largest payment: {0} {1}").format(
				f"{_('Bank Transfer')} {e1.tr1}", fmt_money(50020, currency="EGP")
			),
			content,
		)

	def test_escalation_marker(self):
		e1 = make_alert_fixture_e1(self)
		self.run_company()
		cheque = make_pdc_cheque(
			amount=5000, issue_date=self.today, due_date=self.day(5), bank_account=e1.bank_account
		)
		cheque.deliver(handover_date=self.today)
		_result, sendmail = self.run_company(hour=11)
		(html,) = self.mail_to(sendmail, MANAGER)
		# only the worse days, each with its previous balance
		self.assertEqual(html.count('class="treasury-level-'), 2)
		self.assertEqual(html.count("▼"), 2)
		self.assertIn(_("was {0}").format(fmt_money(-10020, currency="EGP")), html)
		self.assertIn(_("Worse: {0}").format(_("Critical", context=LEVEL_CTX)), html)

	# ------------------------------------------------------------------ manager tools (P1)

	def test_preview_digest(self):
		e1 = make_alert_fixture_e1(self)
		with self.mail() as sendmail, self.set_user(MANAGER):
			preview = notify.preview_digest(COMPANY, MANAGER)
			hidden = notify.preview_digest(COMPANY, DESK_USER)
		sendmail.assert_not_called()
		self.assertIn(e1.bank_account, preview["html"])
		self.assertEqual(preview["subject"], notify.digest_subject(COMPANY, self.today, 1, 0))
		# read only: no log, no notification
		self.assertFalse(alert_logs(company=COMPANY))
		self.assertFalse(
			frappe.db.exists("Notification Log", {"for_user": MANAGER, "document_type": "Bank Account"})
		)
		# a user who may not see any bank
		self.assertIsNone(hidden["html"])
		self.assertEqual(hidden["message"], _("{0} may not see any bank of {1}.").format(DESK_USER, COMPANY))
		# managers only
		with self.set_user(USER):
			self.assertRaises(frappe.PermissionError, notify.preview_digest, COMPANY, USER)

	def test_preview_limited_to_manager_banks(self):
		"""Security Major 2 / review M1: a manager's preview of another user never shows a bank outside the
		manager's own User Permissions; only enabled System Users can be previewed."""
		e1 = make_alert_fixture_e1(self)
		usd = ensure_alert_bank("USD")
		self.limit_to_bank(usd.bank_account)
		with self.mail(), self.set_user(MANAGER):
			full = notify.preview_digest(COMPANY, MANAGER)
		self.assertIn(e1.bank_account, full["html"])
		self.assertIn(usd.bank_account, full["html"])
		with self.mail(), self.set_user(LIMITED_MANAGER):
			# MANAGER sees every bank; the limited manager only gets the bank they may see
			limited = notify.preview_digest(COMPANY, MANAGER)
			own = notify.preview_digest(COMPANY)
		self.assertIn(usd.bank_account, limited["html"])
		self.assertNotIn(e1.bank_account, limited["html"])
		self.assertIn(usd.bank_account, own["html"])
		self.assertNotIn(e1.bank_account, own["html"])

		with self.set_user(MANAGER):
			for user in (DISABLED_USER, NO_ROLE_USER):
				with self.subTest(user=user):
					self.assertRaisesRegex(
						frappe.ValidationError,
						_("{0} is not an enabled System User.").format(user),
						notify.preview_digest,
						COMPANY,
						user,
					)
			self.assertRaises(
				frappe.DoesNotExistError, notify.preview_digest, COMPANY, "nobody-7e@example.com"
			)

	def test_send_test_digest(self):
		e1 = make_alert_fixture_e1(self)
		with self.set_user(MANAGER):
			with self.mail(outgoing=False) as sendmail:
				result = notify.send_test_digest(COMPANY)
			self.assertEqual(
				(result["sent"], result["message"]), (False, _("No default outgoing e-mail account."))
			)
			sendmail.assert_not_called()
			with self.mail() as sendmail:
				result = notify.send_test_digest(COMPANY)
		self.assertTrue(result["sent"])
		(call,) = sendmail.call_args_list
		self.assertEqual(call.kwargs["recipients"], [frappe.db.get_value("User", MANAGER, "email")])
		self.assertTrue(call.kwargs["subject"].startswith(_("[Test]") + " "), call.kwargs["subject"])
		self.assertIn(e1.bank_account, call.kwargs["message"])
		# no log: the real digest of the day still goes out
		self.assertFalse(alert_logs(alert_type="Digest", company=COMPANY))
		result, _sendmail = self.run_company(hour=8, digest=True)
		self.assertEqual(result["digest"], [MANAGER])
		with self.set_user(USER):
			self.assertRaises(frappe.PermissionError, notify.send_test_digest, COMPANY)

	def test_run_alerts_now(self):
		with self.set_user(MANAGER):
			result = notify.run_alerts_now(COMPANY)
		self.assertTrue(result["queued"])
		(job,) = self.balance_jobs()
		self.assertEqual(job.kwargs["job_id"], f"treasury-balance-alerts-{COMPANY}")
		self.assertIs(job.kwargs["deduplicate"], True)
		with self.set_user(USER):
			self.assertRaises(frappe.PermissionError, notify.run_alerts_now, COMPANY)

	def test_manager_tools_are_post_only(self):
		for method in (notify.preview_digest, notify.send_test_digest, notify.run_alerts_now):
			with self.subTest(method=method.__name__):
				self.assertIn(method, frappe.whitelisted)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])
		# the recipients preview only reads
		self.assertIn(recipients.preview_recipients, frappe.whitelisted)

	# ------------------------------------------------------------------ recipients (P7)

	def test_preview_recipients(self):
		set_recipients(self, [{"role": "Treasury Manager"}, {"user": USER}])
		with self.set_user(MANAGER):
			preview = recipients.preview_recipients()
		count = len(recipients.get_recipients())
		self.assertEqual(preview["count"], count)
		self.assertGreaterEqual(count, 2)
		self.assertEqual(len(preview["names"]), min(count, recipients.PREVIEW_NAMES))
		self.assertEqual(preview["more"], max(count - recipients.PREVIEW_NAMES, 0))
		with self.set_user(USER):
			self.assertRaises(frappe.PermissionError, recipients.preview_recipients)

	def test_recipient_reason(self):
		set_recipients(self, [{"role": "Treasury Manager"}, {"user": USER}])
		self.assertEqual(recipients.recipient_reason(MANAGER), _("Treasury Manager"))
		self.assertEqual(recipients.recipient_reason(USER), get_fullname(USER))

	# ------------------------------------------------------------------ thresholds helper (P3)

	def test_threshold_banks(self):
		usd = ensure_alert_bank("USD")
		settings = frappe.get_doc("Treasury Company Settings", COMPANY)
		banks = settings.get_threshold_banks()
		row = next(b for b in banks if b["bank_account"] == usd.bank_account)
		self.assertEqual(row["currency"], "USD")
		self.assertEqual(
			{frappe.db.get_value("Bank Account", b["bank_account"], "company") for b in banks}, {COMPANY}
		)
		self.assertIn(type(settings).get_threshold_banks, frappe.whitelisted)
		with self.set_user(NO_ROLE_USER):
			self.assertRaises(frappe.PermissionError, settings.get_threshold_banks)
		# security Minor 4: the User Permissions on Bank Account apply
		self.limit_to_bank(usd.bank_account)
		with self.set_user(LIMITED_MANAGER):
			limited = frappe.get_doc("Treasury Company Settings", COMPANY).get_threshold_banks()
		self.assertEqual([b["bank_account"] for b in limited], [usd.bank_account])
		self.assertGreater(len(banks), 1)


def synthetic_position(bank_account: str) -> frappe._dict:
	"""A one-day position without thresholds (for rendering tests)."""
	day = frappe._dict(
		date=getdate(),
		actual_in=0,
		actual_out=0,
		expected_in=0,
		expected_out=0,
		net=0,
		closing=100,
		flows=[],
	)
	position = frappe._dict(
		bank_account=bank_account,
		bank_accounts=[bank_account],
		currency="EGP",
		current_balance=100,
		opening=100,
		overdue_in=0,
		overdue_out=0,
		unassigned_flows=0,
		days=[day],
	)
	apply_thresholds(position, [])
	return position

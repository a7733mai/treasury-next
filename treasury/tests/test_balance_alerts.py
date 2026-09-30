"""Phase 7e: levels, the hourly digest / alerts, dedupe and escalation, channels, language and recipients
(spec 7e §3.2–3.4, §7 C).

The E1 fixture (treasury.tests.utils.make_alert_fixture_e1) is built on a bank of its own with dates relative
to the real today. `evaluate_company` is called directly with a `now` of the day under test; e-mail is mocked
(`notify.outgoing_email_enabled`, `frappe.sendmail`) and `frappe.enqueue` is recorded, not queued.
"""

from contextlib import contextmanager
from itertools import product
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote_plus

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.translate import print_language
from frappe.utils import fmt_money, formatdate, get_datetime

from treasury.balance_alerts import notify, recipients, scheduler
from treasury.balance_alerts.positions import evaluate, top_movements
from treasury.tests.utils import (
	ALERT_LOG_DOCTYPE,
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	USD_RATE,
	BalanceAlertTestMixin,
	add_user_permission,
	alert_logs,
	ensure_alert_bank,
	ensure_company,
	ensure_user,
	fund_bank,
	make_alert_fixture_e1,
	make_pdc_cheque,
	set_alert_settings,
	set_recipients,
	set_thresholds,
)

MANAGER = "treasury-alerts-mgr@example.com"
USER = "treasury-alerts-user@example.com"
ACCOUNTS_USER = "treasury-alerts-accounts@example.com"
OTHER_CO_USER = "treasury-alerts-otherco@example.com"
USD_ONLY_USER = "treasury-alerts-usd@example.com"
NO_ROLE_USER = "treasury-alerts-norole@example.com"
DISABLED_USER = "treasury-alerts-disabled@example.com"
ARABIC_USER = "treasury-alerts-ar@example.com"
ENGLISH_USER = "treasury-alerts-en@example.com"


def ensure_language_user(email: str, language: str) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"language": language,
				"send_welcome_email": 0,
				"roles": [{"role": "Treasury Manager"}],
			}
		).insert(ignore_permissions=True)
	frappe.cache.hdel("lang", email)
	return email


class TestBalanceAlerts(BalanceAlertTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(USER, ["Treasury User"])
		ensure_user(ACCOUNTS_USER, ["Accounts User"])
		ensure_user(OTHER_CO_USER, ["Treasury User"])
		ensure_user(USD_ONLY_USER, ["Treasury User"])
		ensure_user(NO_ROLE_USER, [])
		ensure_user(DISABLED_USER, ["Treasury Manager"])
		frappe.db.set_value("User", DISABLED_USER, "enabled", 0)
		ensure_language_user(ARABIC_USER, "ar")
		ensure_language_user(ENGLISH_USER, "en")
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		add_user_permission(OTHER_CO_USER, "Company", OTHER_COMPANY)

	def setUp(self):
		super().setUp()
		set_recipients(self, [{"user": MANAGER}])

	# ------------------------------------------------------------------ helpers

	def at(self, offset: int = 0, hour: int = 10, minute: int = 0):
		"""A `now` on the real today + `offset` days."""
		return get_datetime(f"{self.day(offset)} {hour:02d}:{minute:02d}:00")

	@contextmanager
	def mail(self, outgoing: bool = True):
		with (
			patch("treasury.balance_alerts.notify.outgoing_email_enabled", return_value=outgoing),
			patch.object(notify.frappe, "sendmail") as sendmail,
		):
			yield sendmail

	def run_company(self, offset: int = 0, hour: int = 10, digest: bool = False, outgoing: bool = True):
		with self.mail(outgoing) as sendmail:
			result = scheduler.evaluate_company(COMPANY, self.at(offset, hour), send_digest=digest)
		return result, sendmail

	def notifications(self, user: str, **filters) -> list:
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": user, "document_type": "Bank Account", **filters},
			fields=["name", "subject", "type", "document_name", "email_content"],
			order_by="creation asc",
		)

	def deliver(self, bank_account: str, amount: float, due: int):
		"""An outgoing cheque on `bank_account` due today + `due`, delivered today."""
		cheque = make_pdc_cheque(
			amount=amount, issue_date=self.today, due_date=self.day(due), bank_account=bank_account
		)
		cheque.deliver(handover_date=self.today)
		return cheque

	def mail_to(self, sendmail, user: str) -> list[str]:
		email = frappe.db.get_value("User", user, "email")
		return [c.kwargs["message"] for c in sendmail.call_args_list if c.kwargs.get("recipients") == [email]]

	# ------------------------------------------------------------------ tests

	def test_levels_and_breaches(self):
		e1 = make_alert_fixture_e1(self)
		(position,) = evaluate(COMPANY, [e1.bank_account])
		self.assertEqual(
			[d.closing for d in position.days], [100000, 70000, 80000, 29980, 29980, -10020, -10020]
		)
		self.assertEqual(
			[d.level for d in position.days],
			["OK", "OK", "OK", "Warning", "Warning", "Critical", "Critical"],
		)
		self.assertEqual((position.level, position.minimum, position.warning), ("Critical", 20000, 50000))
		self.assertEqual(position.first_breach, self.day(3))
		self.assertEqual((position.lowest.date, position.lowest.closing), (self.day(5), -10020))
		self.assertEqual(
			[(b.date, b.level, b.closing, b.threshold) for b in position.breaches],
			[
				(self.day(3), "Warning", 29980, 50000),
				(self.day(4), "Warning", 29980, 50000),
				(self.day(5), "Critical", -10020, 20000),
				(self.day(6), "Critical", -10020, 20000),
			],
		)
		self.assertEqual(
			[(m.amount, m.reference_name) for m in top_movements(position, self.day(5))],
			[(50020, e1.tr1), (40000, e1.drawdown), (30000, e1.c1)],
		)
		self.assertEqual(
			[(m.amount, m.reference_name) for m in top_movements(position, self.day(3))],
			[(50020, e1.tr1), (30000, e1.c1)],
		)

	def test_alert_sent_once_per_day(self):
		e1 = make_alert_fixture_e1(self)
		result, _sendmail = self.run_company(outgoing=False)
		logs = alert_logs(alert_type="Balance Alert", bank_account=e1.bank_account)
		self.assertEqual(sorted(result["alerts"]), sorted(log.name for log in logs))
		self.assertEqual(
			sorted((log.projected_date, log.level, log.balance, log.is_escalation) for log in logs),
			[
				(self.day(3), "Warning", 29980, 0),
				(self.day(4), "Warning", 29980, 0),
				(self.day(5), "Critical", -10020, 0),
				(self.day(6), "Critical", -10020, 0),
			],
		)
		self.assertEqual(
			{(log.alert_date, log.recipients, log.notified) for log in logs}, {(self.today, MANAGER, 1)}
		)
		notes = self.notifications(MANAGER, document_name=e1.bank_account)
		self.assertEqual(len(notes), 1)
		self.assertTrue(notes[0].subject.startswith("Critical: "), notes[0].subject)
		self.assertIn(e1.bank_account, notes[0].subject)
		self.assertEqual(notes[0].type, "Alert")

		result, _sendmail = self.run_company(hour=11, outgoing=False)
		self.assertEqual(result["alerts"], [])
		self.assertEqual(len(alert_logs(alert_type="Balance Alert", bank_account=e1.bank_account)), 4)
		self.assertEqual(len(self.notifications(MANAGER, document_name=e1.bank_account)), 1)

	def test_escalation(self):
		e1 = make_alert_fixture_e1(self)
		self.run_company(outgoing=False)
		self.deliver(e1.bank_account, 5000, due=5)

		result, _sendmail = self.run_company(hour=11, outgoing=False)
		new = [log for log in alert_logs(bank_account=e1.bank_account) if log.name in result["alerts"]]
		self.assertEqual(
			sorted((log.projected_date, log.level, log.balance, log.is_escalation) for log in new),
			[(self.day(5), "Critical", -15020, 1), (self.day(6), "Critical", -15020, 1)],
		)
		notes = self.notifications(MANAGER, document_name=e1.bank_account)
		self.assertEqual(len(notes), 2)
		self.assertTrue(notes[-1].subject.startswith("Worse: Critical: "), notes[-1].subject)

		result, _sendmail = self.run_company(hour=12, outgoing=False)
		self.assertEqual(result["alerts"], [])
		self.assertEqual(len(self.notifications(MANAGER, document_name=e1.bank_account)), 2)

		# a Warning day turning Critical is a new key: sent at once, not as "Worse"
		self.deliver(e1.bank_account, 10000, due=3)
		result, _sendmail = self.run_company(hour=13, outgoing=False)
		new = {
			(log.projected_date, log.level): log
			for log in alert_logs(bank_account=e1.bank_account)
			if log.name in result["alerts"]
		}
		self.assertEqual(new[(self.day(3), "Critical")].is_escalation, 0)
		self.assertEqual(new[(self.day(3), "Critical")].balance, 19980)
		self.assertEqual(new[(self.day(5), "Critical")].is_escalation, 1)
		notes = self.notifications(MANAGER, document_name=e1.bank_account)
		self.assertEqual(len(notes), 3)
		self.assertTrue(notes[-1].subject.startswith("Critical: "), notes[-1].subject)

	def test_next_day_resends(self):
		e1 = make_alert_fixture_e1(self)
		self.run_company(outgoing=False)
		result, _sendmail = self.run_company(offset=1, outgoing=False)
		logs = [log for log in alert_logs(bank_account=e1.bank_account) if log.name in result["alerts"]]
		self.assertEqual({log.alert_date for log in logs}, {self.day(1)})
		# the next day's horizon: T+1 .. T+7
		self.assertEqual(
			sorted((log.projected_date, log.level) for log in logs),
			[
				(self.day(3), "Warning"),
				(self.day(4), "Warning"),
				(self.day(5), "Critical"),
				(self.day(6), "Critical"),
				(self.day(7), "Critical"),
			],
		)
		(position,) = evaluate(COMPANY, [e1.bank_account], today=self.day(1))
		self.assertEqual(position.days[0].date, self.day(1))
		self.assertEqual(position.days[-1].date, self.day(7))

	def test_no_threshold_no_alert(self):
		e1 = make_alert_fixture_e1(self)
		plain = ensure_alert_bank()
		fund_bank(plain, 100, self.day(-1))
		self.deliver(plain.bank_account, 1000, due=1)

		result, sendmail = self.run_company(digest=True)
		self.assertEqual(result["digest"], [MANAGER])
		self.assertFalse(alert_logs(alert_type="Balance Alert", bank_account=plain.bank_account))
		self.assertTrue(alert_logs(alert_type="Balance Alert", bank_account=e1.bank_account))
		(digest,) = self.mail_to(sendmail, MANAGER)[:1]
		self.assertIn(plain.bank_account, digest)
		(position,) = evaluate(COMPANY, [plain.bank_account])
		self.assertEqual((position.level, position.breaches, position.minimum), ("No Threshold", [], None))

	def test_digest_timing(self):
		e1 = make_alert_fixture_e1(self)
		result, _sendmail = self.run_company(hour=6, digest=True)
		self.assertEqual(result["digest"], [])
		self.assertTrue(result["alerts"], "alerts are evaluated before the digest hour")
		with self.mail():
			result = scheduler.evaluate_company(COMPANY, self.at(0, 6, 59), send_digest=True)
		self.assertEqual(result["digest"], [])

		result, _sendmail = self.run_company(hour=7, digest=True)
		self.assertEqual(result["digest"], [MANAGER])
		(log,) = alert_logs(alert_type="Digest", company=COMPANY, user=MANAGER, alert_date=self.today)
		self.assertTrue(log.subject)
		result, _sendmail = self.run_company(hour=9, digest=True)
		self.assertEqual(result["digest"], [])
		# the event-triggered evaluation never sends the digest
		result, _sendmail = self.run_company(offset=1, hour=9, digest=False)
		self.assertEqual(result["digest"], [])
		set_alert_settings(self, send_balance_digest=0)
		result, _sendmail = self.run_company(offset=1, hour=10, digest=True)
		self.assertEqual(result["digest"], [])
		self.assertTrue(alert_logs(alert_type="Balance Alert", bank_account=e1.bank_account))

	def test_digest_content(self):
		e1 = make_alert_fixture_e1(self)
		result, sendmail = self.run_company(hour=8, digest=True)
		self.assertEqual(result["digest"], [MANAGER])
		(html,) = self.mail_to(sendmail, MANAGER)[:1]
		positions = evaluate(COMPANY)
		self.assertEqual(html.count('class="treasury-bank"'), len(positions))
		# Critical banks first: E1's is the only one, so its table is the first
		self.assertLess(html.index(e1.bank_account), html.index("</table>"))
		section = bank_section(html, e1.bank_account)
		self.assertIn(fmt_money(100000, currency="EGP"), section.split("</th>")[0])
		self.assertIn(_("Actual balance today (GL)"), section)
		self.assertEqual(section.count('class="treasury-day'), 7)
		self.assertEqual(section.count("treasury-level-critical"), 2)
		self.assertEqual(section.count("treasury-level-warning"), 2)
		self.assertEqual(section.count("#FDECEC"), 2)
		self.assertEqual(section.count("#FFF4E0"), 2)
		link = html[html.index("treasury-forecast-link", html.index(e1.bank_account)) :].split('"')[2]
		self.assertIn("periodicity=Daily", link)
		self.assertIn(f"bank_account={quote_plus(e1.bank_account)}", link)
		self.assertIn("currency_view=Bank+Currency", link)

		# days without movements left out, except today and the last day
		(position,) = evaluate(COMPANY, [e1.bank_account])
		compact = notify.render_digest(COMPANY, [position], self.today, 7, zero_days=False)
		self.assertEqual(compact.count('class="treasury-day'), 6)
		self.assertNotIn(formatdate(self.day(4)), compact)
		for offset in (0, 1, 2, 3, 5, 6):
			self.assertIn(formatdate(self.day(offset)), compact)
		full = notify.render_digest(COMPANY, [position], self.today, 7, zero_days=True)
		self.assertEqual(full.count('class="treasury-day'), 7)

	def test_channels(self):
		# F1: a Notification Log of type Alert sends no e-mail of its own
		self.assertIn("Alert", frappe.get_hooks("notification_skip_email_types"))
		make_alert_fixture_e1(self)
		for notification, email in product((0, 1), (0, 1)):
			with self.subTest(notification=notification, email=email):
				# a fresh day for the dedupe keys
				frappe.db.delete(ALERT_LOG_DOCTYPE, {"company": COMPANY})
				set_alert_settings(self, balance_alert_notification=notification, balance_alert_email=email)
				before = len(self.notifications(MANAGER))
				result, sendmail = self.run_company(hour=8, digest=True)
				# the digest and one alert (E1 has one bank with breaches)
				self.assertEqual(len(self.notifications(MANAGER)) - before, 2 if notification else 0)
				self.assertEqual(sendmail.call_count, 2 if email else 0)
				logs = alert_logs(company=COMPANY)
				if notification or email:
					self.assertEqual(result["digest"], [MANAGER])
					self.assertEqual(
						{(log.notified, log.emailed) for log in logs}, {(notification, email)}
					)
				else:
					self.assertEqual(result, {"digest": [], "alerts": []})
					self.assertEqual(logs, [])
				new = self.notifications(MANAGER)[before:]
				self.assertEqual({n.type for n in new}, {"Alert"} if notification else set())

	def test_email_needs_an_outgoing_account(self):
		make_alert_fixture_e1(self)
		set_alert_settings(self, balance_alert_notification=1, balance_alert_email=1)
		result, sendmail = self.run_company(hour=8, digest=True, outgoing=False)
		self.assertEqual(result["digest"], [MANAGER])
		sendmail.assert_not_called()
		logs = alert_logs(alert_type="Balance Alert", company=COMPANY)
		self.assertTrue(logs)
		self.assertEqual({(log.notified, log.emailed) for log in logs}, {(1, 0)})

	def test_language(self):
		e1 = make_alert_fixture_e1(self)
		set_recipients(self, [{"user": ARABIC_USER}, {"user": ENGLISH_USER}])
		self.run_company(hour=8, digest=True, outgoing=False)
		(arabic,) = self.notifications(ARABIC_USER, document_name=e1.bank_account)
		(english,) = self.notifications(ENGLISH_USER, document_name=e1.bank_account)
		self.assertTrue(english.subject.startswith("Critical: "), english.subject)
		with print_language("ar"):
			prefix = _("Critical: {0} projected {1} on {2} (minimum {3})").split("{0}")[0]
			digest_prefix = _("Bank position for {0} on {1}: {2} critical, {3} warning").split("{0}")[0]
		self.assertTrue(arabic.subject.startswith(prefix), arabic.subject)
		if prefix != "Critical: ":
			# the compiled catalogue of the site (bench build) holds ar.po's translation
			self.assertTrue(arabic.subject.startswith("حرج: "), arabic.subject)
			self.assertNotEqual(arabic.subject, english.subject)
		po = (Path(frappe.get_app_path("treasury")) / "locale" / "ar.po").read_text(encoding="utf-8")
		self.assertIn('msgid "Critical: {0} projected {1} on {2} (minimum {3})"\nmsgstr "حرج:', po)
		digests = [
			n.subject
			for n in frappe.get_all(
				"Notification Log",
				filters={"for_user": ARABIC_USER, "document_type": "Bank Account"},
				fields=["subject"],
			)
			if n.subject.startswith(digest_prefix)
		]
		self.assertEqual(len(digests), 1)

	def test_recipients_by_permission(self):
		e1 = make_alert_fixture_e1(self)
		usd = ensure_alert_bank("USD")
		fund_bank(usd, 1000, self.day(-1), rate=USD_RATE)
		add_user_permission(USD_ONLY_USER, "Bank Account", usd.bank_account)
		set_thresholds(
			self,
			[
				{"bank_account": e1.bank_account, "warning_balance": 50000, "minimum_balance": 20000},
				{"bank_account": usd.bank_account, "warning_balance": 0, "minimum_balance": 0},
			],
		)

		set_recipients(self, [{"role": "Treasury User"}, {"user": ACCOUNTS_USER}])
		resolved = recipients.get_recipients()
		self.assertIn(USER, resolved)
		self.assertIn(ACCOUNTS_USER, resolved)

		set_recipients(
			self,
			[
				{"user": MANAGER},
				{"user": OTHER_CO_USER},
				{"user": USD_ONLY_USER},
				{"user": NO_ROLE_USER},
				{"user": DISABLED_USER},
			],
		)
		self.assertNotIn(DISABLED_USER, recipients.get_recipients())
		result, sendmail = self.run_company(hour=8, digest=True)
		self.assertEqual(sorted(result["digest"]), sorted([MANAGER, USD_ONLY_USER]))
		for user in (OTHER_CO_USER, NO_ROLE_USER, DISABLED_USER):
			self.assertEqual(self.mail_to(sendmail, user), [], user)
			self.assertFalse(self.notifications(user), user)
		(usd_digest,) = self.mail_to(sendmail, USD_ONLY_USER)
		self.assertIn(usd.bank_account, usd_digest)
		self.assertNotIn(e1.bank_account, usd_digest)
		self.assertFalse(self.notifications(USD_ONLY_USER, document_name=e1.bank_account))
		(log,) = alert_logs(
			alert_type="Balance Alert", bank_account=e1.bank_account, projected_date=self.day(5)
		)
		self.assertEqual(log.recipients, MANAGER)
		banks = [e1.bank_account, usd.bank_account]
		self.assertEqual(recipients.visible_banks(USD_ONLY_USER, COMPANY, banks), [usd.bank_account])
		self.assertEqual(recipients.visible_banks(OTHER_CO_USER, COMPANY, banks), [])
		self.assertEqual(recipients.visible_banks(NO_ROLE_USER, COMPANY, banks), [])
		self.assertEqual(recipients.visible_banks(MANAGER, COMPANY, banks), banks)

	def test_event_trigger_enqueued(self):
		e1 = make_alert_fixture_e1(self)
		self.assertEqual(self.balance_jobs(), [], "no trigger without thresholds (the fixture sets them last)")
		for mode in ("Ignore", "Warn", "Block"):
			with self.subTest(mode=mode):
				self.use_company_settings(on_low_balance=mode)
				self.enqueue.reset_mock()
				# due beyond the horizon: nothing is lowered, even Block lets it through
				cheque = make_pdc_cheque(
					amount=100, issue_date=self.today, due_date=self.day(10), bank_account=e1.bank_account
				)
				cheque.deliver(handover_date=self.today)
				(job,) = self.balance_jobs()
				self.assertEqual(job.kwargs["company"], COMPANY)
				self.assertEqual(job.kwargs["job_id"], f"treasury-balance-alerts-{COMPANY}")
				self.assertEqual(job.kwargs["queue"], "short")
				self.assertIs(job.kwargs["deduplicate"], True)
				self.assertIs(job.kwargs["enqueue_after_commit"], True)

	def test_run_hourly_isolated(self):
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		if not frappe.db.exists("Treasury Company Settings", OTHER_COMPANY):
			frappe.get_doc({"doctype": "Treasury Company Settings", "company": OTHER_COMPANY}).insert(
				ignore_permissions=True
			)
		title = _("Bank position alerts failed for company {0}").format(COMPANY)
		frappe.db.delete("Error Log", {"reference_doctype": "Company", "reference_name": COMPANY})
		real = scheduler.evaluate_company

		def evaluate_or_fail(company, now=None, send_digest=False):
			if company == COMPANY:
				raise frappe.ValidationError("boom")
			return real(company, now, send_digest)

		with self.mail(), patch.object(scheduler, "evaluate_company", side_effect=evaluate_or_fail):
			result = scheduler.run_hourly(self.at(0, 8))
			self.assertNotIn(COMPANY, result)
			self.assertIn(OTHER_COMPANY, result)
			scheduler.run_hourly(self.at(0, 9))
		self.assertEqual(
			frappe.db.count(
				"Error Log", {"reference_doctype": "Company", "reference_name": COMPANY, "method": title}
			),
			1,
		)

		# both channels off, or both jobs off: nothing is evaluated
		set_alert_settings(self, balance_alert_notification=0, balance_alert_email=0)
		self.assertEqual(scheduler.run_hourly(self.at(0, 10)), {})
		set_alert_settings(self, balance_alert_notification=1, send_balance_digest=0, send_balance_alerts=0)
		self.assertEqual(scheduler.run_hourly(self.at(0, 10)), {})


def bank_section(html: str, bank_account: str) -> str:
	"""The digest table of one bank (from its header to the end of its table)."""
	start = html.index(bank_account)
	return html[start : html.index("</table>", start)]

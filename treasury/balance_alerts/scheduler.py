"""Hourly bank position job and event-driven evaluation (spec 7e §3.4).

`run_hourly` (hooks.py `scheduler_events["hourly"]`) evaluates every company of Treasury Company Settings in
its own savepoint (committed per company; a failure is logged once per company and day) and sends:
- the daily digest, once per recipient, company and day, from the digest hour on (system time zone);
- the critical / warning alerts: one alert per bank position and run, for its breaches not alerted yet today,
  or alerted with a higher projected balance ("Worse:"). A Warning day turning Critical is a new key, so it is
  sent at once; a day that recovers sends nothing (nor does a Critical day improving to Warning the same day).
  Each recipient's message lists only the payments they may read. An alert nobody received writes no log.
`evaluate_company` (also the job the entry-time guard enqueues after a guarded action, digest excluded) locks
the company's Treasury Company Settings row first, so the hourly job and the event jobs of one company never
send the same alert twice. Idempotency keys (Treasury Alert Log): Digest (company, user, alert_date); Balance
Alert (company, bank account, projected date, level, alert_date).
"""

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.utils import cint, flt, get_datetime, now_datetime, today

from treasury.balance_alerts import notify, recipients
from treasury.balance_alerts.positions import (
	CRITICAL,
	WARNING,
	alert_settings,
	epsilon,
	evaluate,
	horizon,
)
from treasury.pdc.report_utils import get_precision
from treasury.utils.context import SYSTEM_USER, service_context

LOG_DOCTYPE = "Treasury Alert Log"
SETTINGS = "Treasury Company Settings"
DIGEST, BALANCE_ALERT, OVERRIDE = "Digest", "Balance Alert", "Override"
SAVEPOINT = "treasury_balance_alerts"


def channels_enabled(settings) -> bool:
	return bool(settings.balance_alert_notification or settings.balance_alert_email)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def run_hourly(now=None) -> dict:
	"""{company: {"digest": [users], "alerts": [Treasury Alert Logs]}} of the companies evaluated."""
	now = get_datetime(now) if now else now_datetime()
	settings = alert_settings()
	if not channels_enabled(settings) or not (settings.send_balance_digest or settings.send_balance_alerts):
		return {}
	result = {}
	for company in frappe.get_all(SETTINGS, pluck="company", order_by="company asc"):
		value = _isolated(company, lambda company=company: evaluate_company(company, now, send_digest=True))
		if value is not None:
			result[company] = value
	return result


def evaluate_company(company: str, now=None, send_digest: bool = False) -> dict:
	"""One evaluation of the company's positions (a single engine call) shared by the digest and the alerts.
	`now` (default: the current system time) gives the day evaluated and the alert day."""
	now = get_datetime(now) if now else now_datetime()
	result = {"digest": [], "alerts": []}
	settings = alert_settings()
	if not channels_enabled(settings) or not frappe.db.exists(SETTINGS, company):
		return result
	# serialises the hourly job and the event jobs of one company
	frappe.db.get_value(SETTINGS, company, "name", for_update=True)
	with service_context(actor=SYSTEM_USER):
		positions = evaluate(company, today=now.date(), system=True)
	digest_hour = cint(settings.balance_digest_hour)
	if send_digest and settings.send_balance_digest and now.hour >= digest_hour:
		result["digest"] = send_digest_if_due(company, positions, now)
	if settings.send_balance_alerts:
		result["alerts"] = send_balance_alerts(company, positions, now)
	return result


def enqueue_evaluation(company: str):
	"""The event trigger of a guarded action: evaluate the company after the commit (alerts only)."""
	# `deduplicate` also drops a job enqueued while the same job is *running*: a breach committed during that
	# run is then reported by the next hourly job (accepted, spec 7e §8.1)
	# `frappe.enqueue` records the session user as the job's user: the job runs as Administrator, never as the
	# user whose action happened to enqueue it (the dedup job_id lets one enqueue stand for the whole company).
	# Only the recorded user is swapped, for the duration of the call (`frappe.set_user` would reset the request).
	# PINNED to frappe v16: `frappe.utils.background_jobs.enqueue` reads `frappe.session.user` at call time
	# (`kwargs["user"]`), not when the job is queued after the commit; re-verify on a frappe upgrade.
	session = frappe.local.session
	acting = session.user
	session.user = SYSTEM_USER
	try:
		frappe.enqueue(
			"treasury.balance_alerts.scheduler.evaluate_company",
			queue="short",
			company=company,
			job_id=f"treasury-balance-alerts-{company}",
			deduplicate=True,
			enqueue_after_commit=True,
		)
	finally:
		session.user = acting


def _isolated(company: str, fn):
	"""Run `fn` in a savepoint, commit its result, log a failure (once per company and day)."""
	frappe.db.savepoint(SAVEPOINT)
	try:
		value = fn()
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.clear_messages()
		_log_failure(company)
		value = None
	else:
		frappe.db.release_savepoint(SAVEPOINT)
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: allowed in scheduler jobs
	return value


def _log_failure(company: str):
	title = _("Bank position alerts failed for company {0}").format(company)
	if frappe.db.exists(
		"Error Log",
		{
			"reference_doctype": "Company",
			"reference_name": company,
			"method": title,
			"creation": [">=", today()],
		},
	):
		return
	frappe.log_error(title=title, reference_doctype="Company", reference_name=company)


# ---------------------------------------------------------------------------
# Treasury Alert Log
# ---------------------------------------------------------------------------


def insert_log(**values) -> str:
	doc = frappe.get_doc({"doctype": LOG_DOCTYPE, **values})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _locked(conditions) -> list[frappe._dict]:
	"""The latest log matching `conditions`, read with a lock (the latest committed rows, see the module)."""
	log = frappe.qb.DocType(LOG_DOCTYPE)
	query = frappe.qb.from_(log).select(log.name, log.balance).orderby(log.creation, order=Order.desc)
	for field, value in conditions.items():
		query = query.where(getattr(log, field) == value)
	return query.limit(1).for_update().run(as_dict=True)


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def send_digest_if_due(company: str, positions: list, now) -> list[str]:
	"""Send the digest of `alert_date` to every recipient who may see a bank and has not got it yet."""
	if not positions:
		return []
	now = get_datetime(now)
	alert_date = now.date()
	days = horizon()
	zero_days = bool(alert_settings().digest_zero_days)
	link = notify.forecast_route(company, alert_date, days)
	sent = []
	for user in recipients.get_recipients():
		banks = set(recipients.visible_banks(user, company, [p.bank_account for p in positions]))
		if not banks:
			continue
		if _locked({"alert_type": DIGEST, "company": company, "user": user, "alert_date": alert_date}):
			continue
		visible = [p for p in positions if p.bank_account in banks]

		def render(user, visible=visible):
			return notify.digest_message(company, visible, alert_date, days, zero_days, user)

		outcome = notify.deliver(user, render, None, link, _("Bank position digest e-mail could not be sent"))
		if not (outcome.notified or outcome.emailed):
			continue
		insert_log(
			alert_type=DIGEST,
			company=company,
			alert_date=alert_date,
			user=user,
			notified=cint(outcome.notified),
			emailed=cint(outcome.emailed),
			subject=outcome.subject,
			message="\n".join(notify.digest_lines(visible)),
		)
		sent.append(user)
	return sent


# ---------------------------------------------------------------------------
# Critical and warning alerts
# ---------------------------------------------------------------------------


def due_breaches(company: str, position, alert_date) -> list[frappe._dict]:
	"""The breaches of the position not alerted yet today, or alerted with a higher balance (escalation). A
	Warning day already alerted as Critical today has improved: a day that recovers sends nothing."""
	eps = epsilon(get_precision())
	due = []
	for breach in position.breaches:
		key = {
			"alert_type": BALANCE_ALERT,
			"company": company,
			"bank_account": position.bank_account,
			"projected_date": breach.date,
			"alert_date": alert_date,
		}
		if breach.level == WARNING and _locked({**key, "level": CRITICAL}):
			continue
		last = _locked({**key, "level": breach.level})
		if not last:
			due.append(frappe._dict(breach, is_escalation=0))
		elif flt(breach.closing) < flt(last[0].balance) - eps:
			# `previous`: the balance of the last alert ("was ..." in the e-mail)
			due.append(frappe._dict(breach, is_escalation=1, previous=flt(last[0].balance)))
	return due


def send_balance_alerts(company: str, positions: list, now) -> list[str]:
	"""One alert per position with due breaches, to every recipient who may see the bank; one Treasury Alert
	Log per due breach. Returns the names of the logs written."""
	now = get_datetime(now)
	alert_date = now.date()
	days = horizon()
	with_breaches = [p for p in positions if p.breaches]
	if not with_breaches:
		return []
	users = recipients.get_recipients()
	visible = {
		user: set(recipients.visible_banks(user, company, [p.bank_account for p in with_breaches]))
		for user in users
	}
	names = []
	for position in with_breaches:
		due = due_breaches(company, position, alert_date)
		if not due:
			continue
		escalation = all(b.is_escalation for b in due)
		link = notify.forecast_route(company, alert_date, days, position.bank_account)

		# rendered per recipient: each one sees only the payments they may read
		def render(user, position=position, due=due, escalation=escalation):
			return (
				notify.alert_subject(position, due, escalation),
				notify.render_alert(company, position, due, alert_date, days, escalation, user),
				notify.alert_summary(position, due, user),
			)

		notified, emailed, reached = False, False, []
		for user in users:
			if position.bank_account not in visible[user]:
				continue
			outcome = notify.deliver(
				user, render, position.bank_account, link, _("Bank balance alert e-mail could not be sent")
			)
			notified |= outcome.notified
			emailed |= outcome.emailed
			if outcome.notified or outcome.emailed:
				reached.append(user)
		if not reached:
			# nobody got it (no recipient may see the bank, or no channel worked): no dedupe key, so the
			# alert goes out as soon as someone can receive it (e.g. after recipients are added)
			continue
		# the log keeps the subject in the system language
		subject = notify.alert_subject(position, due, escalation)
		for breach in due:
			names.append(
				insert_log(
					alert_type=BALANCE_ALERT,
					level=breach.level,
					company=company,
					bank_account=position.bank_account,
					alert_date=alert_date,
					projected_date=breach.date,
					balance=breach.closing,
					threshold=breach.threshold,
					currency=position.currency,
					is_escalation=breach.is_escalation,
					recipients="\n".join(reached),
					notified=cint(notified),
					emailed=cint(emailed),
					subject=subject,
					message=subject,
				)
			)
	return names

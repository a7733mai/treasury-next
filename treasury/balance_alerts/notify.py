"""Rendering and channels of the bank position digest and alerts (spec 7e §3.4).

Every message is rendered in the recipient's language (`print_language`), like the PDC due alerts. Channels
(Treasury Settings): a Notification Log of type Alert (which sends no e-mail of its own: Frappe skips the
e-mail of "Alert" logs) and / or the e-mail, sent only when a default outgoing e-mail account exists; a
failing e-mail is logged once and never fails the job.
"""

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.translate import get_user_lang, print_language
from frappe.utils import add_days, cint, fmt_money, formatdate, getdate

from treasury.balance_alerts import positions as balance_positions
from treasury.pdc import scheduler as pdc_scheduler
from treasury.utils.context import SYSTEM_USER

DIGEST_TEMPLATE = "treasury/templates/emails/bank_position_digest.html"
ALERT_TEMPLATE = "treasury/templates/emails/balance_alert.html"
FORECAST_REPORT = "Bank Position Forecast"
BANK_CURRENCY, COMPANY_CURRENCY = "Bank Currency", "Company Currency"
RTL_LANGUAGES = ("ar", "fa", "he", "ur", "ps")
# Row colours of the digest / alert tables (inline: e-mail clients drop style sheets)
COLOURS = {
	balance_positions.CRITICAL: ("#FDECEC", "#C0392B"),
	balance_positions.WARNING: ("#FFF4E0", "#B9770E"),
}


# ---------------------------------------------------------------------------
# Routes and formatting
# ---------------------------------------------------------------------------


def forecast_route(company: str, today, horizon: int, bank_account: str | None = None) -> str:
	"""The Bank Position Forecast, Daily, from today over the horizon: one bank in its currency, or all banks
	in the company currency."""
	today = getdate(today)
	params = {
		"company": company,
		"from_date": str(today),
		"to_date": str(getdate(add_days(today, max(cint(horizon), 1) - 1))),
		"periodicity": "Daily",
		"include_overdue": 1,
		"show_details": 1,
		"currency_view": BANK_CURRENCY if bank_account else COMPANY_CURRENCY,
	}
	if bank_account:
		params["bank_account"] = bank_account
	return f"/app/query-report/{FORECAST_REPORT.replace(' ', '%20')}?{urlencode(params)}"


def text_direction() -> str:
	"""`rtl` for the current language when it is written right to left."""
	lang = (frappe.local.lang or "en").split("-")[0]
	return "rtl" if lang in RTL_LANGUAGES else "ltr"


def money(value, currency) -> str:
	return fmt_money(value, currency=currency)


def level_label(level: str) -> str:
	return _(level, context="Treasury Alert Log")


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def digest_subject(company: str, today, critical: int, warning: int) -> str:
	return _("Bank position for {0} on {1}: {2} critical, {3} warning").format(
		company, formatdate(today), critical, warning
	)


def digest_summary(positions: list) -> str:
	"""One short line per bank (the Notification Log text, HTML line breaks)."""
	lines = []
	for p in positions:
		lowest = p.lowest or frappe._dict(date=None, closing=p.current_balance)
		lines.append(
			_("{0}: today {1}, lowest {2} on {3} ({4})").format(
				p.bank_account,
				money(p.current_balance, p.currency),
				money(lowest.closing, p.currency),
				formatdate(lowest.date) if lowest.date else "",
				level_label(p.level),
			)
		)
	return "<br>".join(lines)


def sort_for_digest(positions: list) -> list:
	return sorted(positions, key=lambda p: (balance_positions.level_order(p.level), p.bank_account))


def render_digest(company: str, positions: list, today, horizon: int, zero_days: bool = True) -> str:
	"""The digest HTML (also the e-mail body), in the current language."""
	today = getdate(today)
	banks = [_digest_bank(company, p, today, horizon, zero_days) for p in sort_for_digest(positions)]
	unassigned = positions[0].unassigned_flows if positions else 0
	return frappe.render_template(
		DIGEST_TEMPLATE,
		{
			"company": company,
			"date": formatdate(today),
			"horizon": horizon,
			"dir": text_direction(),
			"critical": sum(1 for p in positions if p.level == balance_positions.CRITICAL),
			"warning": sum(1 for p in positions if p.level == balance_positions.WARNING),
			"banks": banks,
			"unassigned": _("{0} expected cheque flows have no bank account and are not included.").format(
				unassigned
			)
			if unassigned
			else None,
		},
	)


def _digest_bank(company: str, position, today, horizon: int, zero_days: bool) -> frappe._dict:
	currency = position.currency
	rows = []
	if position.overdue_in or position.overdue_out:
		rows.append(
			frappe._dict(
				label=_("Overdue (expected)"),
				inflow=money(position.overdue_in, currency),
				outflow=money(position.overdue_out, currency),
				closing="",
				status="",
				level=None,
			)
		)
	last = len(position.days) - 1
	for index, day in enumerate(position.days):
		if not zero_days and index not in (0, last) and not day.net and not day.flows:
			continue
		level = day.get("level")
		rows.append(
			frappe._dict(
				label=formatdate(day.date),
				inflow=money(day.actual_in + day.expected_in, currency),
				outflow=money(day.actual_out + day.expected_out, currency),
				closing=money(day.closing, currency),
				status=level_label(level) if level and level != balance_positions.NO_THRESHOLD else "",
				level=level,
				colours=COLOURS.get(level),
			)
		)
	merged = [b for b in position.bank_accounts if b != position.bank_account]
	return frappe._dict(
		name=position.bank_account,
		merged=", ".join(merged),
		currency=currency,
		level=position.level,
		level_label=level_label(position.level),
		current_balance=money(position.current_balance, currency),
		warning=money(position.warning, currency) if position.warning is not None else None,
		minimum=money(position.minimum, currency) if position.minimum is not None else None,
		rows=rows,
		link=frappe.utils.get_url(forecast_route(company, today, horizon, position.bank_account)),
	)


# ---------------------------------------------------------------------------
# Balance alert
# ---------------------------------------------------------------------------


def alert_subject(position, breaches: list, escalation: bool) -> str:
	"""Subject named after the worst due breach (the first Critical one, else the first Warning one)."""
	critical = [b for b in breaches if b.level == balance_positions.CRITICAL]
	breach = (critical or breaches)[0]
	values = (
		position.bank_account,
		money(breach.closing, position.currency),
		formatdate(breach.date),
		money(breach.threshold, position.currency),
	)
	if critical:
		subject = _("Critical: {0} projected {1} on {2} (minimum {3})").format(*values)
	else:
		subject = _("Warning: {0} projected {1} on {2} (warning {3})").format(*values)
	if escalation:
		subject = _("Worse: {0}").format(subject)
	return subject


def render_alert(company: str, position, breaches: list, today, horizon: int) -> str:
	"""The alert HTML (also the e-mail body), in the current language."""
	currency = position.currency
	first = min(b.date for b in breaches)
	rows = [
		frappe._dict(
			date=formatdate(b.date),
			level=b.level,
			level_label=level_label(b.level),
			closing=money(b.closing, currency),
			threshold=money(b.threshold, currency),
			shortfall=money(max(b.threshold - b.closing, 0), currency),
			worse=bool(b.get("is_escalation")),
			colours=COLOURS.get(b.level),
		)
		for b in sorted(breaches, key=lambda b: b.date)
	]
	movements = [
		frappe._dict(
			m,
			date=formatdate(m.date),
			amount=money(m.amount, currency),
			url=frappe.utils.get_url(
				f"/app/{frappe.scrub(m.reference_doctype).replace('_', '-')}/{m.reference_name}"
			)
			if m.reference_doctype and m.reference_name
			else None,
			label=_(m.reference_doctype) if m.reference_doctype else "",
		)
		for m in balance_positions.top_movements(position, first)
	]
	return frappe.render_template(
		ALERT_TEMPLATE,
		{
			"company": company,
			"bank_account": position.bank_account,
			"dir": text_direction(),
			"currency": currency,
			"current_balance": money(position.current_balance, currency),
			"rows": rows,
			"movements": movements,
			"link": frappe.utils.get_url(forecast_route(company, today, horizon, position.bank_account)),
		},
	)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def channels() -> frappe._dict:
	values = balance_positions.alert_settings()
	return frappe._dict(notification=values.balance_alert_notification, email=values.balance_alert_email)


def outgoing_email_enabled() -> bool:
	return pdc_scheduler.outgoing_email_enabled()


def deliver(user: str, render, document_name: str | None, link: str, email_title: str) -> frappe._dict:
	"""Render `render()` -> (subject, html, summary) in the user's language and send it through the enabled
	channels. Returns {subject, notified, emailed}."""
	with print_language(get_user_lang(user)):
		subject, html, summary = render()
	options = channels()
	notified = emailed = False
	if options.notification:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"type": "Alert",
				"for_user": user,
				"from_user": SYSTEM_USER,
				"subject": subject,
				"email_content": summary or html,
				"document_type": "Bank Account",
				"document_name": document_name,
				"link": link,
			}
		).insert(ignore_permissions=True)
		notified = True
	if options.email and outgoing_email_enabled():
		email = frappe.db.get_value("User", user, "email")
		if email:
			try:
				frappe.sendmail(recipients=[email], subject=subject, message=html)
				emailed = True
			except Exception:
				frappe.log_error(title=email_title)
	return frappe._dict(subject=subject, notified=notified, emailed=emailed)

"""Rendering and channels of the bank position digest and alerts (spec 7e §3.4, design 7e §3).

Every message is rendered in the recipient's language (`print_language`), like the PDC due alerts. Channels
(Treasury Settings): a Notification Log of type Alert (which sends no e-mail of its own: Frappe skips the
e-mail of "Alert" logs) and / or the e-mail, sent only when a default outgoing e-mail account exists; a
failing e-mail is logged once and never fails the job.

The e-mail templates use inline styles only (e-mail clients drop style sheets), a table layout and escape every
value; the direction (`rtl`) follows the language. A level always shows a colour *and* a word.

Treasury Managers can preview the digest a user would receive, send a test digest to themselves and queue an
alert check (design 7e P1): `preview_digest`, `send_test_digest`, `run_alerts_now`. None of them writes a
Treasury Alert Log, so the real digest of the day still goes out.
"""

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.translate import get_user_lang, print_language
from frappe.utils import add_days, cint, escape_html, flt, fmt_money, formatdate, get_url, getdate
from frappe.utils.data import get_user_date_format

from treasury.balance_alerts import positions as balance_positions
from treasury.balance_alerts import recipients
from treasury.pdc import scheduler as pdc_scheduler
from treasury.pdc.report_utils import validate_company
from treasury.utils.context import SYSTEM_USER, elevated

DIGEST_TEMPLATE = "treasury/templates/emails/bank_position_digest.html"
ALERT_TEMPLATE = "treasury/templates/emails/balance_alert.html"
FORECAST_REPORT = "Bank Position Forecast"
OUTLOOK_REPORT = "Bank Balance Outlook"
BANK_CURRENCY, COMPANY_CURRENCY = "Bank Currency", "Company Currency"
RTL_LANGUAGES = ("ar", "fa", "he", "ur", "ps")
# The levels are shown with the report's context (design 7e §8)
LEVEL_CTX = "Bank Balance Outlook"
MANAGER_ROLES = ("Treasury Manager", "System Manager")
# Row colours of the digest / alert tables (inline: e-mail clients drop style sheets)
COLOURS = {
	balance_positions.CRITICAL: ("#FDECEC", "#C0392B"),
	balance_positions.WARNING: ("#FFF4E0", "#B9770E"),
}
# Text colour and pill (background, text) of each level (design 7e §3)
LEVEL_STYLES = {
	balance_positions.CRITICAL: frappe._dict(text="#C0392B", pill=("#C0392B", "#FFFFFF")),
	balance_positions.WARNING: frappe._dict(text="#B9770E", pill=("#F5B041", "#1F272E")),
	balance_positions.OK: frappe._dict(text="#1E8449", pill=("#D5F5E3", "#1E8449")),
	balance_positions.NO_THRESHOLD: frappe._dict(text="#6C757D", pill=("#EEF0F2", "#6C757D")),
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


def outlook_route(company: str) -> str:
	"""The Bank Balance Outlook of the company (design 7e P13)."""
	return f"/app/query-report/{OUTLOOK_REPORT.replace(' ', '%20')}?{urlencode({'company': company})}"


def form_url(doctype: str, name: str) -> str:
	return get_url(f"/app/{frappe.scrub(doctype).replace('_', '-')}/{name}")


def is_rtl() -> bool:
	return (frappe.local.lang or "en").split("-")[0] in RTL_LANGUAGES


def text_direction() -> str:
	"""`rtl` for the current language when it is written right to left."""
	return "rtl" if is_rtl() else "ltr"


def email_date(value, long: bool = False) -> str:
	"""A date with its weekday in the current language (design 7e P9): "Thu 01-10-2026", or "Thursday ..."."""
	if not value:
		return ""
	return formatdate(value, ("EEEE " if long else "EEE ") + get_user_date_format())


def money(value, currency) -> str:
	return fmt_money(value, currency=currency)


def level_label(level: str) -> str:
	return _(level, context=LEVEL_CTX) if level else ""


def level_style(level: str) -> frappe._dict:
	return LEVEL_STYLES.get(level) or LEVEL_STYLES[balance_positions.NO_THRESHOLD]


def _template_context(**values) -> dict:
	rtl = is_rtl()
	return {
		"dir": "rtl" if rtl else "ltr",
		"rtl": rtl,
		"start": "right" if rtl else "left",
		"end": "left" if rtl else "right",
		"arrow": "←" if rtl else "→",
		**values,
	}


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def digest_subject(company: str, today, critical: int, warning: int) -> str:
	if not critical and not warning:
		return _("Bank position for {0} on {1}: all banks above their thresholds").format(
			company, formatdate(today)
		)
	return _("Bank position for {0} on {1}: {2} critical, {3} warning").format(
		company, formatdate(today), critical, warning
	)


def digest_lines(positions: list) -> list[str]:
	"""One short plain line per bank, in level order: `{bank}: today {balance}, lowest {amount} on {date}
	({level})`."""
	lines = []
	for p in sort_for_digest(positions):
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
	return lines


def digest_summary(positions: list) -> str:
	"""The Notification Log text: one escaped line per bank, HTML line breaks."""
	return "<br>".join(escape_html(line) for line in digest_lines(positions))


def sort_for_digest(positions: list) -> list:
	return sorted(positions, key=lambda p: (balance_positions.level_order(p.level), p.bank_account))


def level_counts(positions: list) -> dict[str, int]:
	return {level: sum(1 for p in positions if p.level == level) for level in balance_positions.LEVELS}


def render_digest(
	company: str, positions: list, today, horizon: int, zero_days: bool = True, receive_as: str | None = None
) -> str:
	"""The digest HTML (also the e-mail body), in the current language."""
	today = getdate(today)
	counts = level_counts(positions)
	pill_texts = {
		balance_positions.CRITICAL: _("{0} Critical"),
		balance_positions.WARNING: _("{0} Warning"),
		balance_positions.OK: _("{0} OK"),
		balance_positions.NO_THRESHOLD: _("{0} No Threshold"),
	}
	pills = [
		frappe._dict(text=pill_texts[level].format(counts[level]), colours=level_style(level).pill)
		for level in balance_positions.LEVELS
		if counts[level]
	]
	unassigned = positions[0].unassigned_flows if positions else 0
	return frappe.render_template(
		DIGEST_TEMPLATE,
		_template_context(
			company=company,
			company_link=get_url(outlook_route(company)),
			date=email_date(today, long=True),
			horizon_text=_("next {0} days").format(horizon),
			pills=pills,
			banks=[_digest_bank(company, p, today, horizon, zero_days) for p in sort_for_digest(positions)],
			unassigned=_("{0} expected cheque flows have no bank account and are not included.").format(
				unassigned
			)
			if unassigned
			else None,
			thresholds_hint=_("Thresholds: Treasury Company Settings → Bank Balance Alerts"),
			receive_as=_("You receive this as {0}.").format(receive_as) if receive_as else None,
		),
	)


def _amount_or_blank(value, currency) -> str:
	"""Empty In / Out cells stay blank (design 7e §3.1)."""
	return money(value, currency) if flt(value) else ""


def _digest_bank(company: str, position, today, horizon: int, zero_days: bool) -> frappe._dict:
	currency = position.currency
	style = level_style(position.level)
	lowest = position.lowest or frappe._dict(date=today, closing=position.current_balance)
	rows = []
	if position.overdue_in or position.overdue_out:
		rows.append(
			frappe._dict(
				overdue=True,
				label=_("Overdue (expected)"),
				inflow=_amount_or_blank(position.overdue_in, currency),
				outflow=_amount_or_blank(position.overdue_out, currency),
			)
		)
	last = len(position.days) - 1
	for index, day in enumerate(position.days):
		if not zero_days and index not in (0, last) and not day.net and not day.flows:
			continue
		level = day.get("level")
		rows.append(
			frappe._dict(
				overdue=False,
				label=email_date(day.date),
				inflow=_amount_or_blank(day.actual_in + day.expected_in, currency),
				outflow=_amount_or_blank(day.actual_out + day.expected_out, currency),
				closing=money(day.closing, currency),
				status=level_label(level) if level and level != balance_positions.NO_THRESHOLD else "",
				level=level,
				colours=COLOURS.get(level),
			)
		)
	merged = [b for b in position.bank_accounts if b != position.bank_account]
	lowest_amount, lowest_date = money(lowest.closing, currency), email_date(lowest.date)
	breached = position.level in (balance_positions.CRITICAL, balance_positions.WARNING)
	return frappe._dict(
		name=position.bank_account,
		merged=_("also: {0}").format(", ".join(merged)) if merged else "",
		currency=currency,
		level=position.level,
		level_label=level_label(position.level),
		style=style,
		current_balance=money(position.current_balance, currency),
		warning=money(position.warning, currency) if position.warning is not None else None,
		minimum=money(position.minimum, currency) if position.minimum is not None else None,
		lowest=_("Lowest: {0} on {1}").format(lowest_amount, lowest_date),
		lowest_colour=style.text if breached else None,
		# design 7e P12: a bank without thresholds gets one line instead of the day table
		compact=position.level == balance_positions.NO_THRESHOLD,
		compact_text=_("Today {0} · lowest {1} on {2}").format(
			money(position.current_balance, currency), lowest_amount, lowest_date
		),
		rows=rows,
		link=get_url(forecast_route(company, today, horizon, position.bank_account)),
	)


def digest_message(
	company: str, positions: list, today, horizon: int, zero_days: bool = True, user: str | None = None
) -> tuple[str, str, str]:
	"""(subject, html, Notification Log text) of the digest of `positions`, in the current language."""
	counts = level_counts(positions)
	receive_as = recipients.recipient_reason(user) if user else None
	return (
		digest_subject(company, today, counts[balance_positions.CRITICAL], counts[balance_positions.WARNING]),
		render_digest(company, positions, today, horizon, zero_days, receive_as),
		digest_summary(positions),
	)


# ---------------------------------------------------------------------------
# Balance alert
# ---------------------------------------------------------------------------


def headline_breach(breaches: list) -> frappe._dict:
	"""The breach an alert is named after: the first Critical one, else the first Warning one."""
	ordered = sorted(breaches, key=lambda b: b.date)
	critical = [b for b in ordered if b.level == balance_positions.CRITICAL]
	return (critical or ordered)[0]


def alert_subject(position, breaches: list, escalation: bool) -> str:
	"""Subject named after the worst due breach (the first Critical one, else the first Warning one). The bank
	account name only, never the merged list (short enough for a lock screen)."""
	breach = headline_breach(breaches)
	values = (
		position.bank_account,
		money(breach.closing, position.currency),
		formatdate(breach.date),
		money(breach.threshold, position.currency),
	)
	if breach.level == balance_positions.CRITICAL:
		subject = _("Critical: {0} projected {1} on {2} (minimum {3})").format(*values)
	else:
		subject = _("Warning: {0} projected {1} on {2} (warning {3})").format(*values)
	if escalation:
		subject = _("Worse: {0}").format(subject)
	return subject


def key_sentence(position, breach) -> str:
	"""`{date}: projected {amount}, minimum {min}` (or `warning {w}`) of a breach."""
	values = (
		email_date(breach.date, long=True),
		money(breach.closing, position.currency),
		money(breach.threshold, position.currency),
	)
	if breach.level == balance_positions.CRITICAL:
		return _("{0}: projected {1}, minimum {2}").format(*values)
	return _("{0}: projected {1}, warning {2}").format(*values)


def _movements(position, date) -> list[frappe._dict]:
	"""The largest payments up to `date`, with the party Payment Entry of a cheque (design 7e P14)."""
	currency = position.currency
	rows = []
	for m in balance_positions.top_movements(position, date):
		payment_entry = None
		if m.reference_doctype == "PDC Cheque" and m.reference_name:
			payment_entry = frappe.db.get_value("PDC Cheque", m.reference_name, "party_payment_entry")
		rows.append(
			frappe._dict(
				m,
				date=email_date(m.date),
				amount=money(m.amount, currency),
				url=form_url(m.reference_doctype, m.reference_name)
				if m.reference_doctype and m.reference_name
				else None,
				label=_(m.reference_doctype) if m.reference_doctype else "",
				payment_entry=_("PE {0}").format(payment_entry) if payment_entry else None,
				payment_entry_url=form_url("Payment Entry", payment_entry) if payment_entry else None,
			)
		)
	return rows


def render_alert(company: str, position, breaches: list, today, horizon: int, escalation: bool = False) -> str:
	"""The alert HTML (also the e-mail body), in the current language."""
	currency = position.currency
	headline = headline_breach(breaches)
	style = level_style(headline.level)
	first = min(b.date for b in breaches)
	rows = [
		frappe._dict(
			date=email_date(b.date),
			level=b.level,
			level_label=level_label(b.level),
			closing=money(b.closing, currency),
			threshold=money(b.threshold, currency),
			shortfall=money(max(flt(b.threshold) - flt(b.closing), 0), currency),
			worse=bool(b.get("is_escalation")),
			was=_("was {0}").format(money(b.previous, currency))
			if b.get("is_escalation") and b.get("previous") is not None
			else None,
			colours=COLOURS.get(b.level),
		)
		for b in sorted(breaches, key=lambda b: b.date)
	]
	head_label = level_label(headline.level)
	if escalation:
		head_label = _("Worse: {0}").format(head_label)
	return frappe.render_template(
		ALERT_TEMPLATE,
		_template_context(
			company=company,
			bank_account=position.bank_account,
			currency=currency,
			style=style,
			head_label=head_label,
			title=_("Low projected balance"),
			key_sentence=key_sentence(position, headline),
			shortfall=_("Shortfall {0}").format(
				money(max(flt(headline.threshold) - flt(headline.closing), 0), currency)
			),
			current_balance=money(position.current_balance, currency),
			rows=rows,
			movements_title=_("Largest payments up to {0}").format(email_date(first, long=True)),
			movements=_movements(position, first),
			link=get_url(forecast_route(company, today, horizon, position.bank_account)),
		),
	)


def alert_summary(position, breaches: list) -> str:
	"""The Notification Log text of an alert: the key sentence, one line per due breach, the largest payment
	(escaped, HTML line breaks)."""
	currency = position.currency
	headline = headline_breach(breaches)
	lines = [key_sentence(position, headline)]
	for b in sorted(breaches, key=lambda b: b.date):
		lines.append(
			_("{0}: {1} ({2}, threshold {3})").format(
				formatdate(b.date),
				money(b.closing, currency),
				level_label(b.level),
				money(b.threshold, currency),
			)
		)
	largest = balance_positions.top_movements(position, min(b.date for b in breaches), n=1)
	if largest:
		m = largest[0]
		document = f"{_(m.reference_doctype)} {m.reference_name}" if m.reference_doctype else ""
		lines.append(_("Largest payment: {0} {1}").format(document, money(m.amount, currency)))
	return "<br>".join(escape_html(line) for line in lines)


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
		emailed = send_email(user, subject, html, email_title)
	return frappe._dict(subject=subject, notified=notified, emailed=emailed)


def send_email(user: str, subject: str, html: str, email_title: str) -> bool:
	"""E-mail `user`; a failure is logged and never raised."""
	email = frappe.db.get_value("User", user, "email")
	if not email:
		return False
	try:
		frappe.sendmail(recipients=[email], subject=subject, message=html)
	except Exception:
		frappe.log_error(title=email_title)
		return False
	return True


# ---------------------------------------------------------------------------
# Manager tools (design 7e P1): preview, test send, alert check
# ---------------------------------------------------------------------------


def _manager_company(company: str) -> str:
	frappe.only_for(MANAGER_ROLES)
	return validate_company(company)


def _visible_positions(company: str, user: str, today) -> list:
	with elevated(actor=SYSTEM_USER):
		positions = balance_positions.evaluate(company, today=today)
	banks = set(recipients.visible_banks(user, company, [p.bank_account for p in positions]))
	return [p for p in positions if p.bank_account in banks]


def _user_digest(company: str, user: str) -> frappe._dict:
	"""The digest `user` would receive today (their banks, their language); nothing is sent or logged."""
	if not frappe.db.exists("User", user):
		frappe.throw(_("{0} {1} does not exist").format(_("User"), user), frappe.DoesNotExistError)
	today = getdate()
	visible = _visible_positions(company, user, today)
	if not visible:
		return frappe._dict(
			subject=None, html=None, message=_("{0} may not see any bank of {1}.").format(user, company)
		)
	settings = balance_positions.alert_settings()
	with print_language(get_user_lang(user)):
		subject, html, _summary = digest_message(
			company, visible, today, balance_positions.horizon(), bool(settings.digest_zero_days), user
		)
	return frappe._dict(subject=subject, html=html, message=None)


@frappe.whitelist(methods=["POST"])
def preview_digest(company: str, user: str | None = None) -> dict:
	"""Treasury Manager: the digest `user` (default: the current user) would receive for `company` today.
	Read only: nothing is sent and no Treasury Alert Log is written."""
	company = _manager_company(company)
	user = user or frappe.session.user
	digest = _user_digest(company, user)
	return {"user": user, "subject": digest.subject, "html": digest.html, "message": digest.message}


@frappe.whitelist(methods=["POST"])
def send_test_digest(company: str) -> dict:
	"""Treasury Manager: e-mail today's digest of `company` to the current user only, subject prefixed
	"[Test]". No Treasury Alert Log is written, so the real digest still goes out."""
	company = _manager_company(company)
	user = frappe.session.user
	if not outgoing_email_enabled():
		return {"sent": False, "message": _("No default outgoing e-mail account.")}
	digest = _user_digest(company, user)
	if not digest.html:
		return {"sent": False, "message": digest.message}
	subject = f"{_('[Test]')} {digest.subject}"
	sent = send_email(user, subject, digest.html, _("Bank position digest e-mail could not be sent"))
	email = frappe.db.get_value("User", user, "email")
	message = _("Test digest sent to {0}.").format(email) if sent else None
	return {"sent": sent, "email": email, "message": message}


@frappe.whitelist(methods=["POST"])
def run_alerts_now(company: str) -> dict:
	"""Treasury Manager: queue an evaluation of `company` (alerts only; the same job and dedupe as the event
	trigger). Alerts already sent today are not repeated."""
	from treasury.balance_alerts import scheduler

	company = _manager_company(company)
	scheduler.enqueue_evaluation(company)
	return {"queued": True, "message": _("Alert check queued for {0}.").format(company)}

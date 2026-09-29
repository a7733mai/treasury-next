"""Daily security cheque job (hooks.py `scheduler_events`): expiry alerts (PLAN.md §7).

Follows the PDC due alert pattern (treasury.pdc.scheduler): one digest per company, recipient and `as_of`
day, sent to the Treasury Managers / Users as a system notification and, when outgoing email is set up, by
email; each recipient gets only the security cheques they may read, in their own language.
"""

from urllib.parse import quote

import frappe
from frappe import _
from frappe.translate import get_user_lang, print_language
from frappe.utils import add_days, cint, fmt_money, formatdate, getdate, today

from treasury.pdc import scheduler as pdc_scheduler
from treasury.security_cheques.lifecycle import INCOMING, LIVE_STATUSES, OPEN
from treasury.utils.settings import stored_single_value

DOCTYPE = "Security Cheque"
ALERT_TEMPLATE = "treasury/templates/emails/security_expiry_alert.html"
# Used when Treasury Settings has no stored value (the field default; see alert_days).
DEFAULT_ALERT_DAYS = 15


def send_security_expiry_alerts(as_of=None) -> dict:
	"""Digest of the security cheques still held / given whose expiry date is within the alert days
	(`Treasury Settings.security_alert_days`), expired ones not returned included.

	Returns {company: {"cheques": [names], "recipients": [users notified by this run]}} for the companies
	that had such cheques.
	"""
	as_of = getdate(as_of or today())
	until = add_days(as_of, alert_days())

	by_company: dict[str, list] = {}
	for cheque in _alert_cheques(until):
		by_company.setdefault(cheque.company, []).append(cheque)

	result = {}
	users = pdc_scheduler._alert_users()
	for company, cheques in sorted(by_company.items()):
		recipients = []
		for user in users:
			visible = [c for c in cheques if frappe.has_permission(DOCTYPE, "read", doc=c.name, user=user)]
			if visible and _send_digest(company, visible, user, as_of):
				recipients.append(user)
		result[company] = {"cheques": [c.name for c in cheques], "recipients": recipients}
	return result


def alert_days() -> int:
	"""`Treasury Settings.security_alert_days`; DEFAULT_ALERT_DAYS only when the setting was never stored.

	Read from the raw Singles row: `get_single_value` casts a missing Int to 0, and Frappe does not store the
	default of a new Single field on migrate (treasury.install.set_default_settings does). An explicit 0
	stays 0.
	"""
	value = stored_single_value("Treasury Settings", "security_alert_days")
	return DEFAULT_ALERT_DAYS if value in (None, "") else cint(value)


def _alert_cheques(until) -> list[frappe._dict]:
	return frappe.get_all(
		DOCTYPE,
		filters={
			"status": ["in", LIVE_STATUSES],
			"expiry_date": ["<=", until],
		},
		fields=[
			"name",
			"company",
			"direction",
			"cheque_no",
			"party_type",
			"party",
			"party_name",
			"purpose",
			"amount_type",
			"amount",
			"currency",
			"expiry_date",
			"status",
			"custody_location",
		],
		order_by="expiry_date asc, name asc",
	)


def render_digest(company: str, cheques: list[frappe._dict], as_of) -> str:
	"""The digest HTML (also the email body). Strings are translated in the current language."""
	as_of = getdate(as_of)
	expired = [c for c in cheques if getdate(c.expiry_date) < as_of]
	expiring = [c for c in cheques if getdate(c.expiry_date) >= as_of]
	groups = []
	for title, rows in (
		(_("Expired, Not Returned"), expired),
		(_("Expiring Soon"), expiring),
	):
		if rows:
			groups.append(frappe._dict(title=title, rows=[_digest_row(c) for c in rows]))
	return frappe.render_template(
		ALERT_TEMPLATE,
		{"company": company, "as_of": formatdate(as_of), "groups": groups},
	)


def _digest_row(cheque) -> frappe._dict:
	return frappe._dict(
		cheque,
		direction_label=_("Held from party") if cheque.direction == INCOMING else _("Given to party"),
		amount_formatted=(
			_(OPEN, context=DOCTYPE)
			if cheque.amount_type == OPEN
			else fmt_money(cheque.amount, currency=cheque.currency)
		),
		expiry=formatdate(cheque.expiry_date),
	)


def _send_digest(company: str, cheques: list[frappe._dict], user: str, as_of) -> bool:
	"""Notify `user` in their language; False if they already got this company's digest for `as_of`."""
	with print_language(get_user_lang(user)):
		subject = _("Security cheques expiring for {0} as of {1}").format(company, formatdate(as_of))
		message = render_digest(company, cheques, as_of)
	if frappe.db.exists("Notification Log", {"for_user": user, "document_type": DOCTYPE, "subject": subject}):
		return False
	frappe.get_doc(
		{
			"doctype": "Notification Log",
			"type": "Alert",
			"for_user": user,
			"from_user": "Administrator",
			"subject": subject,
			"email_content": message,
			"document_type": DOCTYPE,
			"link": "/app/security-cheque?company=" + quote(company),
		}
	).insert(ignore_permissions=True)
	_send_email(subject, message, user)
	return True


def _send_email(subject: str, message: str, user: str):
	"""Email the digest only when an outgoing email account is set up; never fail the job because of it."""
	if not pdc_scheduler.outgoing_email_enabled():
		return
	email = frappe.db.get_value("User", user, "email")
	if not email:
		return
	try:
		frappe.sendmail(recipients=[email], subject=subject, message=message)
	except Exception:
		frappe.log_error(title=_("Security cheque expiry alert email could not be sent"))

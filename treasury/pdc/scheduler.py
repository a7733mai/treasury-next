"""Daily PDC jobs (hooks.py `scheduler_events`): `On Cheque Date` party postings and due-date alerts.

Both jobs take an optional `as_of` date (default: today) so a run can be reproduced for a given day.

`post_due_party_postings` posts through `treasury.pdc.lifecycle.post_party(system=True)` — the same rules as
the "Post Now" action, without the user permission check (a scheduler job has no acting user). Every cheque
runs in its own savepoint: a failing cheque is rolled back and logged (once per cheque and day), the others
go on; each cheque is committed on its own, so a job timeout keeps the cheques already posted. Days on which
the job did not run are caught up automatically, because every cheque due up to `as_of` that is still not
posted is posted on its own (effective) due date.

`send_due_alerts` sends every recipient the cheques they may read, in their own language, at most once per
company and day.
"""

from urllib.parse import quote

import frappe
from frappe import _
from frappe.translate import get_user_lang, print_language
from frappe.utils import add_days, cint, fmt_money, formatdate, getdate, today

from treasury.pdc import lifecycle

DOCTYPE = "PDC Cheque"
SAVEPOINT = "treasury_pdc_scheduler"
ALERT_ROLES = ("Treasury Manager", "Treasury User")
ALERT_TEMPLATE = "treasury/templates/emails/pdc_due_alert.html"
# Cheques still waiting for the party posting of the `On Cheque Date` mode (never Created: not handed over).
POSTABLE_STATUSES = {
	lifecycle.OUTGOING: ("Delivered",),
	lifecycle.INCOMING: ("Received", "Deposited"),
}
# Cheques whose due date still matters for the alert digest.
OPEN_STATUSES = ("Created", "Delivered", "Received", "Deposited")


# ---------------------------------------------------------------------------
# On Cheque Date party postings
# ---------------------------------------------------------------------------


def post_due_party_postings(as_of=None) -> dict:
	"""Post the party settlement of every due `On Cheque Date` cheque. Returns {"posted": [], "failed": []}."""
	as_of = getdate(as_of or today())
	result = {"posted": [], "failed": []}
	for company, directions in _cheque_date_companies().items():
		for name in _due_cheques(company, directions, as_of):
			if _post_one(name):
				result["posted"].append(name)
			else:
				result["failed"].append(name)
			_commit()
	return result


def _commit():
	"""Keep each cheque's result even if the job dies later (tests run in one rolled back transaction)."""
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: allowed in scheduler jobs


def _cheque_date_companies() -> dict[str, list[str]]:
	"""{company: [directions]} of the companies posting to the party on the cheque date."""
	companies = {}
	for settings in frappe.get_all(
		"Treasury Company Settings",
		fields=["company", "outgoing_party_posting", "incoming_party_posting"],
	):
		directions = []
		if settings.outgoing_party_posting == "On Cheque Date":
			directions.append(lifecycle.OUTGOING)
		if settings.incoming_party_posting == "On Cheque Date":
			directions.append(lifecycle.INCOMING)
		if directions:
			companies[settings.company] = directions
	return companies


def _due_cheques(company: str, directions: list[str], as_of) -> list[str]:
	"""Not posted cheques of `company` due up to `as_of`, oldest due date first (so the posting order
	follows the calendar). Cheques without a bank account cannot be posted and are left to the user."""
	names = []
	for direction in directions:
		names += frappe.get_all(
			DOCTYPE,
			filters={
				"company": company,
				"direction": direction,
				"accounting_status": lifecycle.NOT_POSTED,
				"operational_status": ["in", POSTABLE_STATUSES[direction]],
				"effective_due_date": ["<=", as_of],
				"bank_account": ["is", "set"],
			},
			fields=["name", "effective_due_date"],
			order_by="effective_due_date asc, creation asc",
		)
	names.sort(key=lambda d: getdate(d.effective_due_date))
	return [d.name for d in names]


def _post_one(name: str) -> bool:
	"""Post one cheque in its own savepoint; roll it back and log the error if it fails."""
	frappe.db.savepoint(SAVEPOINT)
	try:
		# no posting date: the lifecycle posts on the effective due date, or later if the date order
		# rules require it (e.g. a cheque handed over after its due date)
		lifecycle.post_party(name, system=True)
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.clear_messages()
		_log_failure(name)
		return False
	frappe.db.release_savepoint(SAVEPOINT)
	return True


def _log_failure(name: str):
	"""Error Log of a failing cheque, at most once per day (a cheque may fail every day, e.g. closed period)."""
	title = _("PDC scheduled party posting failed for cheque {0}").format(name)
	if frappe.db.exists(
		"Error Log",
		{"reference_doctype": DOCTYPE, "reference_name": name, "method": title, "creation": [">=", today()]},
	):
		return
	frappe.log_error(title=title, reference_doctype=DOCTYPE, reference_name=name)


# ---------------------------------------------------------------------------
# Due alerts
# ---------------------------------------------------------------------------


def send_due_alerts(as_of=None) -> dict:
	"""One digest per company of the open cheques due within the alert days (overdue ones included), sent
	to the Treasury Managers / Users as a system notification and, when outgoing email is set up, by email.

	Each recipient gets only the cheques they may read (role and User Permissions), rendered in their own
	language, and at most one digest per company and `as_of` day (a second run the same day sends nothing).

	Returns {company: {"cheques": [names], "recipients": [users notified by this run]}} for the companies
	that had due cheques.
	"""
	as_of = getdate(as_of or today())
	days = cint(frappe.db.get_single_value("Treasury Settings", "default_alert_days"))
	until = add_days(as_of, days)
	result = {}
	users = _alert_users()
	for company in frappe.get_all("Treasury Company Settings", pluck="company"):
		cheques = _alert_cheques(company, until)
		if not cheques:
			continue
		recipients = []
		for user in users:
			visible = [c for c in cheques if frappe.has_permission(DOCTYPE, "read", doc=c.name, user=user)]
			if visible and _send_digest(company, visible, user, as_of):
				recipients.append(user)
		result[company] = {"cheques": [c.name for c in cheques], "recipients": recipients}
	return result


def _alert_cheques(company: str, until) -> list[frappe._dict]:
	cheques = frappe.get_all(
		DOCTYPE,
		filters={
			"company": company,
			"operational_status": ["in", OPEN_STATUSES],
			"effective_due_date": ["<=", until],
		},
		fields=[
			"name",
			"direction",
			"cheque_no",
			"party_type",
			"party",
			"party_name",
			"bank_account",
			"drawer_bank",
			"amount",
			"currency",
			"effective_due_date",
			"operational_status",
		],
		order_by="effective_due_date asc, name asc",
	)
	return cheques


def _alert_users() -> list[str]:
	"""Enabled system users holding a Treasury role."""
	has_role = frappe.qb.DocType("Has Role")
	user = frappe.qb.DocType("User")
	users = (
		frappe.qb.from_(has_role)
		.join(user)
		.on(user.name == has_role.parent)
		.select(user.name)
		.distinct()
		.where(
			(has_role.parenttype == "User")
			& (has_role.role.isin(ALERT_ROLES))
			& (user.enabled == 1)
			& (user.user_type == "System User")
		)
		.orderby(user.name)
	).run(pluck=True)
	return users


def render_digest(company: str, cheques: list[frappe._dict], as_of) -> str:
	"""The digest HTML (also the email body). Strings are translated in the current language."""
	as_of = getdate(as_of)
	groups = []
	for direction, title in (
		(lifecycle.OUTGOING, _("Outgoing Cheques")),
		(lifecycle.INCOMING, _("Incoming Cheques")),
	):
		rows = [
			frappe._dict(
				c,
				bank=c.bank_account or c.drawer_bank or "",
				due_date=formatdate(c.effective_due_date),
				amount_formatted=fmt_money(c.amount, currency=c.currency),
				overdue=getdate(c.effective_due_date) < as_of,
			)
			for c in cheques
			if c.direction == direction
		]
		if rows:
			groups.append(frappe._dict(title=title, rows=rows))
	return frappe.render_template(
		ALERT_TEMPLATE,
		{"company": company, "as_of": formatdate(as_of), "groups": groups},
	)


def _send_digest(company: str, cheques: list[frappe._dict], user: str, as_of) -> bool:
	"""Notify `user` in their language; False if they already got this company's digest for `as_of`."""
	with print_language(get_user_lang(user)):
		subject = _("Cheques due for {0} as of {1}").format(company, formatdate(as_of))
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
			"link": "/app/pdc-cheque?company=" + quote(company),
		}
	).insert(ignore_permissions=True)
	_send_email(subject, message, user)
	return True


def outgoing_email_enabled() -> bool:
	return bool(frappe.db.exists("Email Account", {"enable_outgoing": 1, "default_outgoing": 1}))


def _send_email(subject: str, message: str, user: str):
	"""Email the digest only when an outgoing email account is set up; never fail the job because of it."""
	if not outgoing_email_enabled():
		return
	email = frappe.db.get_value("User", user, "email")
	if not email:
		return
	try:
		frappe.sendmail(recipients=[email], subject=subject, message=message)
	except Exception:
		frappe.log_error(title=_("PDC due alert email could not be sent"))

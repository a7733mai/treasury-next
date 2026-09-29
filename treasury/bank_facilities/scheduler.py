"""Daily Bank Facilities jobs (hooks.py `scheduler_events`, PLAN.md §8).

- `refresh_facilities`: Active / Suspended facilities past their expiry date become Expired, so do the open
  Letters of Guarantee and the Letters of Credit without documents past their effective expiry date (no
  accounting: a held cash margin is released by the user), the repayment schedule statuses of the open
  drawdowns are refreshed (Overdue moves with the calendar) and the display utilisation of every facility not
  Closed is stored again. Each document runs in its own savepoint and is committed on its own; a failing one
  is rolled back and logged once per day.
- `post_interest_accruals`: treasury.bank_facilities.interest (month-end accruals, catch-up included).
- `send_facility_alerts`: one digest per company, recipient and day (the PDC due alert pattern): unpaid
  installments due within `Treasury Settings.facility_alert_days` (overdue ones included), facilities
  expiring within EXPIRY_ALERT_DAYS days, Letters of Guarantee expiring within the alert days and expired
  ones whose cash margin is still held; each recipient gets only the documents they may read, in their own
  language.
"""

from urllib.parse import quote

import frappe
from frappe import _
from frappe.translate import get_user_lang, print_language
from frappe.utils import add_days, cint, date_diff, flt, fmt_money, formatdate, getdate, today

from treasury.bank_facilities import facility as lifecycle
from treasury.bank_facilities import lc, lg
from treasury.bank_facilities.repayment import PAID, refresh_repayment_status
from treasury.bank_facilities.utilisation import refresh_utilisation
from treasury.pdc import scheduler as pdc_scheduler
from treasury.utils.settings import stored_single_value

FACILITY = "Bank Facility"
DRAWDOWN = "Facility Drawdown"
SCHEDULE = "Facility Repayment Schedule"
ALERT_TEMPLATE = "treasury/templates/emails/facility_alert.html"
# Used when Treasury Settings has no stored value (the field default).
DEFAULT_ALERT_DAYS = 7
EXPIRY_ALERT_DAYS = 30
SAVEPOINT = "treasury_facility_refresh"


# ---------------------------------------------------------------------------
# Refresh (expiry, schedule statuses, utilisation)
# ---------------------------------------------------------------------------


def refresh_facilities(as_of=None) -> dict:
	"""Returns {"expired": [facilities], "expired_instruments": [LGs / LCs], "failed": [names]}."""
	as_of = getdate(as_of or today())
	result = {"expired": [], "expired_instruments": [], "failed": []}
	for name in frappe.get_all(
		FACILITY,
		filters={"status": ["in", lifecycle.OPEN_STATUSES], "expiry_date": ["<", as_of]},
		order_by="expiry_date asc, name asc",
		pluck="name",
	):
		if _isolated(name, FACILITY, lambda n=name: lifecycle.expire(n, as_of), result["failed"]):
			result["expired"].append(name)

	for doctype, module, statuses in (
		(lg.DOCTYPE, lg, lg.OPEN_STATUSES),
		(lc.DOCTYPE, lc, lc.AMENDABLE_STATUSES),
	):
		for name in frappe.get_all(
			doctype,
			filters={"docstatus": 1, "status": ["in", statuses], "effective_expiry_date": ["<", as_of]},
			order_by="effective_expiry_date asc, name asc",
			pluck="name",
		):
			expire = module.expire
			if _isolated(name, doctype, lambda n=name, fn=expire: fn(n, as_of), result["failed"]):
				result["expired_instruments"].append(name)

	for name in frappe.get_all(
		DRAWDOWN, filters={"docstatus": 1, "repayment_status": "Open"}, order_by="name asc", pluck="name"
	):
		_isolated(name, DRAWDOWN, lambda n=name: refresh_repayment_status(n, as_of), result["failed"])

	for name in frappe.get_all(
		FACILITY, filters={"status": ["!=", lifecycle.CLOSED]}, order_by="name asc", pluck="name"
	):
		_isolated(name, FACILITY, lambda n=name: refresh_utilisation(n), result["failed"])
	return result


def _isolated(name: str, doctype: str, fn, failed: list):
	"""Run `fn` in a savepoint, commit its result, log a failure (once per document and day)."""
	frappe.db.savepoint(SAVEPOINT)
	try:
		value = fn()
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.clear_messages()
		_log_failure(doctype, name)
		failed.append(name)
		value = None
	else:
		frappe.db.release_savepoint(SAVEPOINT)
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: allowed in scheduler jobs
	return value


def _log_failure(doctype: str, name: str):
	title = _("Bank facility daily refresh failed for {0} {1}").format(_(doctype), name)
	if frappe.db.exists(
		"Error Log",
		{"reference_doctype": doctype, "reference_name": name, "method": title, "creation": [">=", today()]},
	):
		return
	frappe.log_error(title=title, reference_doctype=doctype, reference_name=name)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def alert_days() -> int:
	"""`Treasury Settings.facility_alert_days`; DEFAULT_ALERT_DAYS only when never stored (0 stays 0)."""
	value = stored_single_value("Treasury Settings", "facility_alert_days")
	return DEFAULT_ALERT_DAYS if value in (None, "") else cint(value)


def get_alert_installments(until) -> list[frappe._dict]:
	"""Unpaid installments of the submitted drawdowns due up to `until` (overdue ones included)."""
	drawdown = frappe.qb.DocType(DRAWDOWN)
	row = frappe.qb.DocType(SCHEDULE)
	rows = (
		frappe.qb.from_(row)
		.join(drawdown)
		.on((drawdown.name == row.parent) & (row.parenttype == DRAWDOWN))
		.select(
			drawdown.name.as_("drawdown"),
			drawdown.company,
			drawdown.facility,
			drawdown.bank,
			drawdown.currency,
			drawdown.bank_account,
			row.installment_no,
			row.due_date,
			row.principal,
			row.interest,
			row.paid_principal,
			row.paid_interest,
		)
		.where(
			(drawdown.docstatus == 1)
			& (row.status != PAID)
			& (row.is_prepayment == 0)
			& (row.due_date <= getdate(until))
		)
		.orderby(row.due_date)
		.orderby(drawdown.name)
		.orderby(row.installment_no)
	).run(as_dict=True)
	for r in rows:
		r.unpaid = flt(
			max(flt(r.principal) - flt(r.paid_principal), 0) + max(flt(r.interest) - flt(r.paid_interest), 0)
		)
	return [r for r in rows if r.unpaid > 0]


def get_expiring_facilities(as_of, days: int = EXPIRY_ALERT_DAYS) -> list[frappe._dict]:
	return frappe.get_all(
		FACILITY,
		filters={
			"status": ["in", lifecycle.OPEN_STATUSES],
			"expiry_date": ["<=", add_days(getdate(as_of), days)],
		},
		fields=["name", "company", "facility_name", "bank", "expiry_date", "status", "total_limit", "currency"],
		order_by="expiry_date asc, name asc",
	)


def send_facility_alerts(as_of=None) -> dict:
	"""Returns {company: {"installments": [(drawdown, installment no)], "facilities": [names],
	"guarantees": [Letters of Guarantee], "recipients": [users notified by this run]}} for the companies with
	something to report."""
	as_of = getdate(as_of or today())
	days = alert_days()
	by_company: dict[str, frappe._dict] = {}

	def items_of(company):
		return by_company.setdefault(company, frappe._dict(installments=[], facilities=[], guarantees=[]))

	for row in get_alert_installments(add_days(as_of, days)):
		items_of(row.company).installments.append(row)
	for row in get_expiring_facilities(as_of):
		items_of(row.company).facilities.append(row)
	for row in lg.get_expiring(as_of, days):
		items_of(row.company).guarantees.append(row)

	result = {}
	users = pdc_scheduler._alert_users()
	for company, items in sorted(by_company.items()):
		recipients = []
		for user in users:
			installments = [
				r
				for r in items.installments
				if frappe.has_permission(DRAWDOWN, "read", doc=r.drawdown, user=user)
			]
			facilities = [
				f for f in items.facilities if frappe.has_permission(FACILITY, "read", doc=f.name, user=user)
			]
			guarantees = [
				g for g in items.guarantees if frappe.has_permission(lg.DOCTYPE, "read", doc=g.name, user=user)
			]
			if (installments or facilities or guarantees) and _send_digest(
				company, installments, facilities, user, as_of, guarantees
			):
				recipients.append(user)
		result[company] = {
			"installments": [(r.drawdown, r.installment_no) for r in items.installments],
			"facilities": [f.name for f in items.facilities],
			"guarantees": [g.name for g in items.guarantees],
			"recipients": recipients,
		}
	return result


def render_digest(company: str, installments: list, facilities: list, as_of, guarantees=None) -> str:
	"""The digest HTML (also the email body), in the current language."""
	as_of = getdate(as_of)
	overdue = [r for r in installments if getdate(r.due_date) < as_of]
	due = [r for r in installments if getdate(r.due_date) >= as_of]
	groups = []
	for title, rows in ((_("Overdue Installments"), overdue), (_("Installments Due Soon"), due)):
		if rows:
			groups.append(frappe._dict(title=title, rows=[_installment_row(r) for r in rows]))
	return frappe.render_template(
		ALERT_TEMPLATE,
		{
			"company": company,
			"as_of": formatdate(as_of),
			"groups": groups,
			"facilities": [_facility_row(f, as_of) for f in facilities],
			"guarantees": [_guarantee_row(g, as_of) for g in guarantees or []],
		},
	)


def _guarantee_row(row, as_of) -> frappe._dict:
	return frappe._dict(
		row,
		expiry=formatdate(row.effective_expiry_date),
		days_to_expiry=date_diff(row.effective_expiry_date, as_of),
		amount_formatted=fmt_money(row.open_amount or row.amount, currency=row.currency),
		margin_formatted=fmt_money(row.margin_held, currency=row.currency),
		note=_("Expired: release the cash margin") if row.status == lg.EXPIRED else _("Expiring soon"),
	)


def _installment_row(row) -> frappe._dict:
	return frappe._dict(
		row,
		due=formatdate(row.due_date),
		principal_formatted=fmt_money(max(flt(row.principal) - flt(row.paid_principal), 0), currency=row.currency),
		interest_formatted=fmt_money(max(flt(row.interest) - flt(row.paid_interest), 0), currency=row.currency),
		unpaid_formatted=fmt_money(row.unpaid, currency=row.currency),
	)


def _facility_row(row, as_of) -> frappe._dict:
	return frappe._dict(
		row,
		expiry=formatdate(row.expiry_date),
		days_to_expiry=date_diff(row.expiry_date, as_of),
		limit_formatted=fmt_money(row.total_limit, currency=row.currency),
	)


def _send_digest(
	company: str, installments: list, facilities: list, user: str, as_of, guarantees=None
) -> bool:
	"""Notify `user` in their language; False if they already got this company's digest for `as_of`."""
	with print_language(get_user_lang(user)):
		subject = _("Bank facility installments and expiries for {0} as of {1}").format(
			company, formatdate(as_of)
		)
		message = render_digest(company, installments, facilities, as_of, guarantees)
	if frappe.db.exists("Notification Log", {"for_user": user, "document_type": DRAWDOWN, "subject": subject}):
		return False
	frappe.get_doc(
		{
			"doctype": "Notification Log",
			"type": "Alert",
			"for_user": user,
			"from_user": "Administrator",
			"subject": subject,
			"email_content": message,
			"document_type": DRAWDOWN,
			"link": "/app/query-report/Facility Repayment Schedule Report?company=" + quote(company),
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
		frappe.log_error(title=_("Bank facility alert email could not be sent"))

"""Bank Facility lifecycle (PLAN.md §8): the facility agreement's status, driven by actions only.

Statuses: Draft → *Activate* → Active ⇄ *Suspend* / *Reactivate* (Suspended) → *Close* → Closed.
Active / Suspended facilities past their expiry date are set Expired by the daily job (`expire_facilities`);
*Renew* moves the expiry date (and optionally the limits) and makes an Expired facility Active again.

Every public action is reached through a whitelisted method of the Bank Facility controller. Each one locks
the facility row, reloads it, checks the user's write permission and the Treasury Manager role, validates the
transition, appends a status log row (with the date of the event) and saves with `flags.treasury_lifecycle`
— the controller rejects any other change to the status, the log and the agreement's key terms once the
facility left Draft. Utilisation (display fields) is refreshed after every action.

A facility never posts anything: drawdowns and repayments do (treasury.bank_facilities.accounting).
"""

import frappe
from frappe import _
from frappe.utils import flt, fmt_money, formatdate, getdate, now_datetime, today

from treasury.utils.context import real_user

DOCTYPE = "Bank Facility"
MANAGER = "Treasury Manager"

DRAFT = "Draft"
ACTIVE = "Active"
SUSPENDED = "Suspended"
EXPIRED = "Expired"
CLOSED = "Closed"
# Facilities whose agreement is running (their limits count, alerts are sent).
OPEN_STATUSES = (ACTIVE, SUSPENDED)


def log_creation(doc):
	"""Called from `before_insert`: the initial status and the first status log row."""
	doc.status = DRAFT
	doc.append(
		"status_log",
		{"datetime": now_datetime(), "user": frappe.session.user, "event": "Created", "to_status": DRAFT},
	)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def activate(name: str, activation_date=None, remarks: str | None = None) -> dict:
	"""Draft → Active, or Suspended → Active (logged as Reactivated)."""
	doc = _begin(name)
	_require_status(doc, (DRAFT, SUSPENDED), already=(ACTIVE,), label=ACTIVE)
	date = _date(activation_date)
	_not_before(doc, date)
	if getdate(doc.expiry_date) < date:
		frappe.throw(
			_("Bank Facility {0} expired on {1}. Renew it instead.").format(
				frappe.bold(doc.name), formatdate(doc.expiry_date)
			)
		)
	_log(doc, "Activated" if doc.status == DRAFT else "Reactivated", ACTIVE, remarks, date)
	return _save(doc)


def suspend(name: str, suspension_date=None, reason: str | None = None) -> dict:
	"""Active → Suspended: no new drawdown until reactivated (repayments go on)."""
	doc = _begin(name)
	_require_status(doc, (ACTIVE,), already=(SUSPENDED,), label=SUSPENDED)
	if not reason:
		frappe.throw(_("A reason is required."))
	date = _date(suspension_date)
	_not_before(doc, date)
	_log(doc, "Suspended", SUSPENDED, reason, date)
	return _save(doc)


def close(name: str, closing_date=None, remarks: str | None = None) -> dict:
	"""Close a facility that has nothing outstanding (no utilisation and no unpaid installment)."""
	from treasury.bank_facilities.utilisation import get_utilisation

	doc = _begin(name)
	_require_status(doc, (ACTIVE, SUSPENDED, EXPIRED), already=(CLOSED,), label=CLOSED)
	date = _date(closing_date)
	_not_before(doc, date)
	utilisation = get_utilisation(doc)
	if flt(utilisation.utilised, doc.precision("total_limit")) > 0:
		frappe.throw(
			_("Bank Facility {0} cannot be closed: {1} is still utilised.").format(
				frappe.bold(doc.name), fmt_money(utilisation.utilised, currency=doc.currency)
			)
		)
	if unpaid := _unpaid_drawdowns(doc.name):
		frappe.throw(
			_("Bank Facility {0} cannot be closed: drawdowns {1} still have unpaid installments.").format(
				frappe.bold(doc.name), ", ".join(frappe.bold(d) for d in unpaid)
			)
		)
	_log(doc, "Closed", CLOSED, remarks, date)
	return _save(doc)


def renew(
	name: str,
	new_expiry_date=None,
	renewal_date=None,
	new_total_limit=None,
	new_limits=None,
	remarks: str | None = None,
) -> dict:
	"""Extend the agreement to `new_expiry_date` (after the current one), optionally with a new total limit and
	new sub-limit amounts (`new_limits`: {limit_type: amount} of existing sub-limits). An Expired facility
	becomes Active again; a Suspended one stays Suspended."""
	doc = _begin(name)
	_require_status(doc, (ACTIVE, SUSPENDED, EXPIRED))
	if not new_expiry_date:
		frappe.throw(_("Enter the new Expiry Date."))
	new_expiry = getdate(new_expiry_date)
	old_expiry = getdate(doc.expiry_date)
	if new_expiry <= old_expiry:
		frappe.throw(
			_("The new Expiry Date ({0}) must be after the current one ({1}).").format(
				formatdate(new_expiry), formatdate(old_expiry)
			)
		)
	date = _date(renewal_date)
	_not_before(doc, date)
	if new_expiry < date:
		frappe.throw(_("The new Expiry Date cannot be before the Renewal Date."))

	changes = [f"{_('Expiry Date', context='Treasury')}: {formatdate(old_expiry)} → {formatdate(new_expiry)}"]
	doc.expiry_date = new_expiry
	doc.renewal_date = date
	if new_total_limit not in (None, "") and flt(new_total_limit) != flt(doc.total_limit):
		changes.append(f"{_('Total Limit')}: {flt(doc.total_limit)} → {flt(new_total_limit)}")
		doc.total_limit = flt(new_total_limit, doc.precision("total_limit"))
	changes += _apply_new_limits(doc, new_limits)
	remarks = "; ".join(changes) + (f": {remarks}" if remarks else "")
	_log(doc, "Renewed", ACTIVE if doc.status == EXPIRED else None, remarks, date)
	return _save(doc)


def expire(name: str, as_of) -> bool:
	"""System action of the daily job: an Active / Suspended facility past its expiry date becomes Expired."""
	doc = _begin(name, system=True)
	as_of = getdate(as_of)
	if doc.status not in OPEN_STATUSES or getdate(doc.expiry_date) >= as_of:
		return False
	_log(doc, "Expired", EXPIRED, None, doc.expiry_date)
	_save(doc)
	return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_new_limits(doc, new_limits) -> list[str]:
	limits = frappe.parse_json(new_limits) if new_limits else {}
	if not isinstance(limits, dict):
		frappe.throw(_("New limits must be given as {limit type: amount}."))
	rows = {row.limit_type: row for row in doc.limits}
	changes = []
	for limit_type, amount in limits.items():
		if limit_type not in rows:
			frappe.throw(
				_("Bank Facility {0} has no {1} sub-limit.").format(
					frappe.bold(doc.name), _(limit_type, context=DOCTYPE)
				)
			)
		row = rows[limit_type]
		amount = flt(amount, row.precision("limit_amount"))
		if amount != flt(row.limit_amount):
			changes.append(f"{_(limit_type, context=DOCTYPE)}: {flt(row.limit_amount)} → {amount}")
			row.limit_amount = amount
	return changes


def _unpaid_drawdowns(facility: str) -> list[str]:
	drawdowns = frappe.get_all(
		"Facility Drawdown", filters={"facility": facility, "docstatus": 1}, pluck="name", order_by="name"
	)
	if not drawdowns:
		return []
	return sorted(
		set(
			frappe.get_all(
				"Facility Repayment Schedule",
				filters={
					"parenttype": "Facility Drawdown",
					"parent": ["in", drawdowns],
					"status": ["!=", "Paid"],
				},
				pluck="parent",
			)
		)
	)


def _begin(name: str, system: bool = False):
	"""Lock the facility row, reload it and check the user's write permission and the manager role.

	`system`: the daily job (no acting user) — no permission check."""
	if not name or not frappe.db.get_value(DOCTYPE, name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(DOCTYPE), name), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
	if system:
		return doc
	doc.check_permission("write")
	if MANAGER not in frappe.get_roles():
		frappe.throw(
			_("This action requires one of the roles: {0}.").format(_(MANAGER)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	return doc


def _save(doc) -> dict:
	from treasury.bank_facilities.utilisation import refresh_utilisation

	# the permission and role were checked by `_begin`; the flag unlocks the lifecycle-only fields
	doc.flags.treasury_lifecycle = True
	doc.flags.ignore_permissions = True
	doc.save()
	refresh_utilisation(doc.name)
	return {"name": doc.name, "status": doc.status}


def _log(doc, event: str, status=None, remarks=None, event_date=None):
	"""Append a status log row and apply the new status (`event_date`: the date of the event itself)."""
	doc.append(
		"status_log",
		{
			"datetime": now_datetime(),
			"event_date": getdate(event_date) if event_date else None,
			"user": real_user(),
			"event": event,
			"from_status": doc.status,
			"to_status": status or doc.status,
			"remarks": remarks,
		},
	)
	if status:
		doc.status = status


def _require_status(doc, allowed: tuple[str, ...], already: tuple[str, ...] = (), label: str | None = None):
	if doc.status in already:
		frappe.throw(
			_("Bank Facility {0} is already {1}.").format(
				frappe.bold(doc.name), _(label or doc.status, context=DOCTYPE)
			)
		)
	if doc.status not in allowed:
		frappe.throw(
			_("This action is not allowed while Bank Facility {0} is {1}.").format(
				frappe.bold(doc.name), _(doc.status, context=DOCTYPE)
			)
		)


def _date(value):
	return getdate(value) if value else getdate(today())


def _not_before(doc, date):
	"""An action may not be dated before the start date or the latest dated event of the facility."""
	latest = max(
		[getdate(doc.start_date), *(getdate(row.event_date) for row in doc.status_log if row.event_date)]
	)
	if getdate(date) < latest:
		frappe.throw(_("The date ({0}) cannot be before {1}.").format(formatdate(date), formatdate(latest)))

"""Security Cheque lifecycle (PLAN.md §7): guarantee cheques held from or given to a party.

A security cheque NEVER creates accounting entries (CLAUDE.md rule 4): nothing here calls the posting
service. The only way to accounting is `encash`, which converts the cheque into a PDC Cheque and hands it to
the PDC lifecycle (`treasury.pdc.lifecycle.receive` / `deliver`), which posts according to Treasury Company
Settings like any other PDC cheque.

Statuses:
  incoming  Created → Received → In Custody → Returned | Encashed | Lost   (Created → Cancelled)
  outgoing  Created → Issued → Returned | Encashed | Lost                   (Created → Cancelled)

Every public action is reached through a whitelisted method of the Security Cheque controller. Each one locks
the cheque row, reloads it, checks the user's permission (and role where required), validates the transition,
appends a status log row (with the date of the event) and, when the custody changes, a custody log row, then
saves with `flags.treasury_lifecycle` — the controller rejects any other change to the status, custody,
lifecycle dates, PDC link and logs. Calling an action again once it happened raises a clear "already ..."
error and changes nothing.

Status log `event` values and statuses are stored in English, like Select values, and are translated when
displayed.
"""

import frappe
from frappe import _
from frappe.utils import flt, formatdate, getdate, now_datetime, today

from treasury.pdc import lifecycle as pdc_lifecycle
from treasury.utils.context import real_user

DOCTYPE = "Security Cheque"
INCOMING = "Incoming"
OUTGOING = "Outgoing"
MANAGER = "Treasury Manager"
OTHER = "Other"
FIXED = "Fixed"
OPEN = "Open / Blank"
# Parties an encashment can be converted for: a PDC Cheque needs a Customer or Supplier.
PDC_PARTY_TYPES = ("Customer", "Supplier")

CREATED = "Created"
RECEIVED = "Received"
IN_CUSTODY = "In Custody"
ISSUED = "Issued"
RETURNED = "Returned"
ENCASHED = "Encashed"
LOST = "Lost"
CANCELLED = "Cancelled"

# Held from the party (incoming) or given to the party (outgoing): the guarantee is live.
LIVE_BY_DIRECTION = {INCOMING: (RECEIVED, IN_CUSTODY), OUTGOING: (ISSUED,)}
LIVE_STATUSES = (RECEIVED, IN_CUSTODY, ISSUED)
FINAL_STATUSES = (RETURNED, ENCASHED, LOST, CANCELLED)
ALL_STATUSES = (CREATED, *LIVE_STATUSES, *FINAL_STATUSES)

ENCASH_SAVEPOINT = "treasury_security_encash"


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def log_creation(doc):
	"""Called from `before_insert`: the initial status and the first status log row."""
	doc.status = CREATED
	doc.append(
		"status_log",
		{
			"datetime": now_datetime(),
			"user": frappe.session.user,
			"event": "Created",
			"to_status": CREATED,
		},
	)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def receive(
	name: str, receipt_date=None, custody_location=None, custodian=None, remarks: str | None = None
) -> dict:
	"""Receive an incoming security cheque; with a custody location it goes straight into custody."""
	doc = _begin(name)
	_require_direction(doc, INCOMING)
	_require_status(doc, (CREATED,), already=(RECEIVED, IN_CUSTODY), label=RECEIVED)
	require_open_amount_role(doc)
	date = _date(receipt_date)
	_not_before(doc, date, "handover_date")

	doc.handover_date = date
	_log(doc, "Received", RECEIVED, remarks, date)
	if custody_location:
		_move_custody(doc, date, custody_location, custodian, remarks, "Stored")
	elif custodian:
		frappe.throw(_("Select the Custody Location too."))
	return _save(doc)


def store(name: str, store_date=None, custody_location=None, custodian=None, remarks: str | None = None) -> dict:
	"""Put a received incoming security cheque into custody."""
	doc = _begin(name)
	_require_direction(doc, INCOMING)
	_require_status(doc, (RECEIVED,), already=(IN_CUSTODY,), label=IN_CUSTODY)
	date = _date(store_date)
	_not_before(doc, date, None)
	_move_custody(doc, date, custody_location, custodian, remarks, "Stored")
	return _save(doc)


def transfer_custody(
	name: str, transfer_date=None, custody_location=None, custodian=None, remarks: str | None = None
) -> dict:
	"""Move a cheque in custody to another location and / or custodian (custody log row)."""
	doc = _begin(name)
	_require_direction(doc, INCOMING)
	_require_status(doc, (IN_CUSTODY,))
	date = _date(transfer_date)
	_not_before(doc, date, None)
	if custody_location == doc.custody_location and (custodian or None) == (doc.custodian or None):
		frappe.throw(_("The cheque is already at this location with this custodian."))
	_move_custody(doc, date, custody_location, custodian, remarks, "Custody Transferred")
	return _save(doc)


def issue(name: str, handover_date=None, remarks: str | None = None) -> dict:
	"""Hand an outgoing security cheque over to the party."""
	doc = _begin(name)
	_require_direction(doc, OUTGOING)
	_require_status(doc, (CREATED,), already=(ISSUED,), label=ISSUED)
	require_open_amount_role(doc)
	date = _date(handover_date)
	_not_before(doc, date, "handover_date")

	doc.handover_date = date
	_log(doc, "Issued", ISSUED, remarks, date)
	return _save(doc)


def return_cheque(name: str, return_date=None, person: str | None = None, reason: str | None = None) -> dict:
	"""Incoming: give the cheque back to the party. Outgoing: the party gave our cheque back.

	`person`: who the cheque was returned to (incoming) / who received it back for us (outgoing)."""
	doc = _begin(name)
	_require_status(doc, LIVE_BY_DIRECTION[doc.direction], already=(RETURNED,), label=RETURNED)
	if not reason:
		frappe.throw(_("A reason is required."))
	date = _date(return_date)
	_not_before(doc, date, "return_date")

	doc.return_date = date
	doc.return_person = (person or "").strip() or None
	_release_custody(doc, date, reason)
	_log(doc, "Returned", RETURNED, reason, date)
	return _save(doc)


def mark_lost(name: str, lost_date=None, reason: str | None = None) -> dict:
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, LIVE_BY_DIRECTION[doc.direction], already=(LOST,), label=LOST)
	if not reason:
		frappe.throw(_("A reason is required."))
	date = _date(lost_date)
	_not_before(doc, date, "lost_date")

	doc.lost_date = date
	_release_custody(doc, date, reason)
	_log(doc, "Lost", LOST, reason, date)
	return _save(doc)


def cancel_cheque(name: str, reason: str | None = None) -> dict:
	"""Cancel a security cheque before any action. A cheque book leaf it took stays used (not freed)."""
	doc = _begin(name)
	_require_status(doc, (CREATED,), already=(CANCELLED,), label=CANCELLED)
	if not reason:
		frappe.throw(_("A reason is required."))
	_log(doc, "Cancelled", CANCELLED, reason)
	return _save(doc)


def encash(
	name: str,
	encashment_date=None,
	amount=None,
	due_date=None,
	bank_account: str | None = None,
	references=None,
	remarks: str | None = None,
) -> dict:
	"""Convert the security cheque into a PDC Cheque — the only way it reaches the accounts.

	Incoming (we present the party's cheque): a PDC Cheque (Incoming) to be deposited to `bank_account`, received
	on the encashment date, so it is posted to the party per Treasury Company Settings. `references` (invoices /
	orders of the party, like a PDC Cheque's) allocate it; without references it is an advance of the party.
	Outgoing (the holder presented our cheque): a PDC Cheque (Outgoing) on the cheque's bank account, delivered
	on the encashment date; the user clears it through the PDC flow when the bank pays it.

	`amount`: required for an Open / Blank cheque; a Fixed cheque is encashed for its amount. `due_date`: the
	PDC Cheque's due date (default: the encashment date, which is also its issue date, so not before it).
	All or nothing: if the PDC Cheque cannot be created or received / delivered, nothing is kept.
	"""
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, LIVE_BY_DIRECTION[doc.direction], already=(ENCASHED,), label=ENCASHED)
	if doc.party_type not in PDC_PARTY_TYPES:
		frappe.throw(
			_(
				"Only a security cheque of a Customer or Supplier can be encashed through a PDC Cheque; this one is of {0}."
			).format(_(doc.party_type, context=DOCTYPE)),
			title=_("Not Allowed"),
		)
	date = _date(encashment_date)
	_not_before(doc, date, "encashment_date")
	value = _encashment_amount(doc, amount)

	frappe.db.savepoint(ENCASH_SAVEPOINT)
	try:
		cheque = _make_pdc_cheque(doc, value, date, due_date, bank_account, references, remarks)
		doc.converted_pdc_cheque = cheque.name
		doc.encashment_date = date
		doc.encashed_amount = value
		_release_custody(doc, date, remarks)
		_log(doc, "Encashed", ENCASHED, remarks, date, pdc_cheque=cheque.name)
		result = _save(doc)
		# posting (if any) happens in the PDC lifecycle, per Treasury Company Settings
		if doc.direction == INCOMING:
			pdc_lifecycle.receive(cheque.name, date, remarks)
		else:
			pdc_lifecycle.deliver(cheque.name, date, remarks)
	except Exception:
		frappe.db.rollback(save_point=ENCASH_SAVEPOINT)
		raise
	frappe.db.release_savepoint(ENCASH_SAVEPOINT)
	return {**result, "pdc_cheque": cheque.name}


# ---------------------------------------------------------------------------
# Encashment helpers
# ---------------------------------------------------------------------------


def _encashment_amount(doc, amount) -> float:
	precision = doc.precision("amount")
	if doc.amount_type == OPEN:
		value = flt(amount, precision)
		if value <= 0:
			frappe.throw(_("Enter the amount to encash: security cheque {0} is Open / Blank.").format(doc.name))
		return value
	value = flt(doc.amount, precision)
	if amount not in (None, "") and flt(amount, precision) != value:
		frappe.throw(
			_("Security cheque {0} has the fixed amount {1}; it cannot be encashed for {2}.").format(
				frappe.bold(doc.name), value, flt(amount, precision)
			)
		)
	return value


def _make_pdc_cheque(doc, amount: float, date, due_date, bank_account, references, remarks):
	"""Insert the PDC Cheque of the encashment (status Created), linked back through `security_cheque`."""
	if doc.direction == INCOMING:
		if not bank_account:
			frappe.throw(_("Select the Bank Account the cheque is deposited to."))
	elif bank_account and bank_account != doc.bank_account:
		frappe.throw(
			_("An outgoing security cheque is paid from its own Bank Account {0}.").format(
				frappe.bold(doc.bank_account)
			)
		)

	cheque = frappe.new_doc(pdc_lifecycle.DOCTYPE)
	for fieldname in ("direction", "company", "party_type", "party", "cheque_no"):
		cheque.set(fieldname, doc.get(fieldname))
	if doc.direction == INCOMING:
		cheque.bank_account = bank_account
		cheque.drawer_bank = doc.drawer_bank
	else:
		# no cheque book: the leaf was consumed by the security cheque already
		cheque.bank_account = doc.bank_account
	cheque.amount = amount
	# the PDC Cheque comes into existence on the encashment date: an `On Creation` party posting is never
	# dated back to when the guarantee was written (that date stays on the security cheque)
	cheque.issue_date = date
	cheque.original_due_date = getdate(due_date) if due_date else date
	cheque.remarks = remarks or _("Encashment of security cheque {0}").format(doc.name)
	for row in _parse_references(references):
		cheque.append("references", row)
	cheque.security_cheque = doc.name
	cheque.flags.treasury_security_cheque = True  # keeps the (insert-only) back-link
	cheque.insert()  # the user's own create permission on PDC Cheque (and all of its validations)

	if cheque.currency != doc.currency:
		frappe.throw(
			_("Security cheque {0} is in {1}, but Bank Account {2} is in {3}.").format(
				frappe.bold(doc.name), doc.currency, frappe.bold(cheque.bank_account), cheque.currency
			)
		)
	return cheque


def _parse_references(references) -> list[dict]:
	rows = frappe.parse_json(references) if references else []
	if not isinstance(rows, list):
		frappe.throw(_("References must be a list."))
	return [
		{
			"reference_doctype": row.get("reference_doctype"),
			"reference_name": row.get("reference_name"),
			"allocated_amount": flt(row.get("allocated_amount")),
		}
		for row in rows
		if row
	]


# ---------------------------------------------------------------------------
# Custody
# ---------------------------------------------------------------------------


def _move_custody(doc, date, location, custodian, remarks, event: str):
	"""Record the new custody (custody log row) and set the cheque In Custody."""
	if not location:
		frappe.throw(_("Custody Location is required."))
	if not frappe.db.get_value("Custody Location", location, "is_active"):
		frappe.throw(_("Custody Location {0} does not exist or is not active.").format(frappe.bold(location)))
	if custodian and not frappe.db.get_value("User", custodian, "enabled"):
		frappe.throw(_("Custodian {0} is not an enabled user.").format(frappe.bold(custodian)))
	_custody_row(doc, date, location, custodian, remarks)
	_log(doc, event, IN_CUSTODY, remarks, date)


def _release_custody(doc, date, remarks):
	"""The cheque leaves custody (returned, encashed, lost): a closing custody log row."""
	if doc.custody_location or doc.custodian:
		_custody_row(doc, date, None, None, remarks)


def _custody_row(doc, date, location, custodian, remarks):
	doc.append(
		"custody_log",
		{
			"datetime": now_datetime(),
			"event_date": getdate(date),
			"user": real_user(),
			"from_location": doc.custody_location,
			"to_location": location,
			"from_custodian": doc.custodian,
			"to_custodian": custodian or None,
			"remarks": remarks,
		},
	)
	doc.custody_location = location
	doc.custodian = custodian or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def require_open_amount_role(doc):
	"""Creating, receiving or issuing an Open / Blank security cheque needs the Treasury Manager role."""
	if doc.amount_type == OPEN and MANAGER not in frappe.get_roles():
		frappe.throw(
			_("An Open / Blank security cheque can only be created, received or issued by a {0}.").format(
				_(MANAGER)
			),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)


def _begin(name: str, roles: tuple[str, ...] | None = None, ptype: str = "write"):
	"""Lock the security cheque row, reload it and check the user's permission (and role)."""
	if not name or not frappe.db.get_value(DOCTYPE, name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(DOCTYPE), name), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission(ptype)
	if roles and not set(roles) & set(frappe.get_roles()):
		frappe.throw(
			_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in roles)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	return doc


def _save(doc) -> dict:
	# the permission (and role) was checked by `_begin`; the flag unlocks the lifecycle-only fields
	doc.flags.treasury_lifecycle = True
	doc.flags.ignore_permissions = True
	doc.save()
	return {"name": doc.name, "status": doc.status}


def _log(doc, event: str, status=None, remarks=None, event_date=None, pdc_cheque=None):
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
			"pdc_cheque": pdc_cheque,
			"remarks": remarks,
		},
	)
	if status:
		doc.status = status


def _require_direction(doc, direction: str):
	if doc.direction != direction:
		frappe.throw(_("This action is only available for {0} security cheques.").format(_(direction)))


def _require_status(doc, allowed: tuple[str, ...], already: tuple[str, ...] = (), label: str | None = None):
	if doc.status in already:
		frappe.throw(
			_("Security cheque {0} is already {1}.").format(
				frappe.bold(doc.name), _(label or doc.status, context=DOCTYPE)
			)
		)
	if doc.status not in allowed:
		frappe.throw(
			_("This action is not allowed while security cheque {0} is {1}.").format(
				frappe.bold(doc.name), _(doc.status, context=DOCTYPE)
			)
		)


def _date(value):
	return getdate(value) if value else getdate(today())


def _latest_date(doc):
	"""The issue date or the latest dated event of the cheque: a new event may not be dated before it."""
	return max(
		[getdate(doc.issue_date), *(getdate(row.event_date) for row in doc.status_log if row.event_date)]
	)


def _not_before(doc, date, fieldname: str | None):
	latest = _latest_date(doc)
	if getdate(date) < latest:
		label = _(doc.meta.get_label(fieldname)) if fieldname else _("The date")
		frappe.throw(_("{0} ({1}) cannot be before {2}.").format(label, formatdate(date), formatdate(latest)))

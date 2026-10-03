"""PDC Cheques (and Bank Transfers) created from a Custody Request: how much the request still accepts, and the
checks a new cheque / transfer of a request must pass (spec 9 §3.2).

Custody caps every custodian Payment Entry by its allowance (F1, F5), but only once the Payment Entry exists.
A cheque is posted to the party later (handover, cheque date ...): open cheques and transfers of the request
that are not posted yet are reserved here, so the request is never promised twice.
"""

import erpnext
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, fmt_money

from treasury.integrations.custody import PARTY_TYPE, REQUEST, compat, is_installed
from treasury.utils import source_documents as sd

SOURCE_DOCTYPES = ("PDC Cheque", "Bank Transfer")
OPEN_CHEQUE_STATUSES = ("Created", "Delivered", "Received", "Deposited")
OPEN_TRANSFER_STATUSES = ("Draft", "Scheduled", "Sent")
OUTGOING, INCOMING = "Outgoing", "Incoming"
PAYMENT_TYPE = {OUTGOING: "Pay", INCOMING: "Receive"}
# custody purpose of a Pay to a custodian who is owed money (a wind-down, F1)
REIMBURSEMENT = "Reimbursement"
PERMANENT = "Permanent"
NOT_POSTED = "Not Posted"
TRANSFER = "Bank Transfer"
PARTY_PAYMENT = "Party Payment"
# a difference below this is rounding (custody's own EPSILON)
TOLERANCE = 0.005


def request_of(doctype: str | None, name: str | None) -> str | None:
	"""The Custody Request a cheque / transfer was created from (its "Created From"), if any."""
	if doctype not in SOURCE_DOCTYPES or not name or not frappe.get_meta(doctype).has_field("source_name"):
		return None
	values = frappe.db.get_value(doctype, name, ["source_doctype", "source_name"], as_dict=True)
	if values and values.source_doctype == REQUEST:
		return values.source_name
	return None


def _precision() -> int:
	return frappe.get_precision("PDC Cheque", "amount") or 2


def reserved(
	request: str, direction: str, exclude: str | None = None, exclude_doctype: str | None = None
) -> float:
	"""What open cheques / transfers of the request that are not posted to the party yet already cover.
	`exclude`: a document that does not count (the one being edited); with `exclude_doctype` only a document
	of that doctype is skipped, without it a document of either doctype with that name."""
	filters = {
		"source_doctype": REQUEST,
		"source_name": request,
		"direction": direction,
		"accounting_status": NOT_POSTED,
		"operational_status": ["in", OPEN_CHEQUE_STATUSES],
	}
	if exclude and exclude_doctype in (None, "PDC Cheque"):
		filters["name"] = ["!=", exclude]
	total = sum(flt(amount) for amount in frappe.get_all("PDC Cheque", filters=filters, pluck="amount"))
	if direction == OUTGOING and frappe.get_meta(TRANSFER).has_field("source_name"):
		base = {"source_doctype": REQUEST, "source_name": request, "transfer_type": PARTY_PAYMENT}
		if exclude and exclude_doctype in (None, TRANSFER):
			base["name"] = ["!=", exclude]
		for extra in ({"docstatus": 0}, {"docstatus": 1, "status": "Scheduled"}):
			total += sum(
				flt(amount) for amount in frappe.get_all(TRANSFER, filters={**base, **extra}, pluck="amount")
			)
	return flt(total, _precision())


def reservation_provider(request: str, payment_type: str, voucher: Document | None = None) -> float:
	"""Hook `custody_reservation_providers` (EC-20): the amount custody must treat as unavailable to its own
	Payment Entries, i.e. what open cheques / transfers of the request already promise (EC-20, AC-F18).

	`voucher`: `Document | None` (custody API contract): the Payment Entry DOCUMENT custody is about to post, or
	None when custody only asks (a draft, a proposal, a report). When Treasury itself creates the voucher for a
	cheque or transfer (custom fields `treasury_source_doctype` / `treasury_source_name`), that document's own
	reservation does not count, or the cheque would block its own posting. With None everything counts."""
	direction = next((d for d, ptype in PAYMENT_TYPE.items() if ptype == payment_type), None)
	if not direction:
		return 0.0
	source = (
		(voucher.get("treasury_source_doctype"), voucher.get("treasury_source_name"))
		if voucher is not None
		else None
	)
	if source and source[0] in SOURCE_DOCTYPES:
		return reserved(request, direction, exclude=source[1], exclude_doctype=source[0])
	return reserved(request, direction)


def open_documents(request: str, limit: int) -> tuple[list[dict], int]:
	"""([{doctype, name, amount, status, payment_entry}], number not listed): the open cheques (posted or not)
	and the draft / scheduled / sent transfers of the request the user may read, newest first, for the Treasury
	block of the request form."""
	cheques = frappe.get_list(
		"PDC Cheque",
		filters={
			"source_doctype": REQUEST,
			"source_name": request,
			"operational_status": ["in", OPEN_CHEQUE_STATUSES],
		},
		fields=["name", "amount", "operational_status", "party_payment_entry", "creation"],
	)
	rows = [
		frappe._dict(
			doctype="PDC Cheque",
			name=row.name,
			amount=row.amount,
			status=row.operational_status,
			payment_entry=row.party_payment_entry,
			creation=row.creation,
		)
		for row in cheques
	]
	if frappe.get_meta(TRANSFER).has_field("source_name"):
		transfers = frappe.get_list(
			TRANSFER,
			filters={
				"source_doctype": REQUEST,
				"source_name": request,
				"docstatus": ["<", 2],
				"status": ["in", OPEN_TRANSFER_STATUSES],
			},
			fields=["name", "amount", "status", "payment_entry", "creation"],
		)
		rows += [frappe._dict(row, doctype=TRANSFER) for row in transfers]
	rows.sort(key=lambda r: r.creation, reverse=True)
	shown = [{k: v for k, v in row.items() if k != "creation"} for row in rows[:limit]]
	return shown, max(len(rows) - limit, 0)


def available(req, direction: str, exclude: str | None = None, for_update: bool = False) -> frappe._dict:
	"""{amount, allowance, purpose, totals}: what a new cheque / transfer of the request may still move
	(custody's allowance less the reserved amount, never below 0) and the purpose custody will give it.

	`for_update`: the figure is about to become a reservation (a cheque / transfer is being created, edited or
	re-presented): custody's locking duty of a consumer, in the caller's transaction: the custodian is locked
	(`lock_custodians`, idempotent) and the totals are read with `for_update=True`, so two concurrent
	reservations cannot both see the same availability. A plain question (the buttons) does not lock."""
	payment_type = PAYMENT_TYPE[direction]
	precision = _precision()
	if for_update:
		compat.lock_custodian(req.custodian)
	totals = compat.request_totals(req.name, for_update=for_update)
	allowance = flt(compat.payment_allowance(req, payment_type, totals), precision)
	amount = max(flt(allowance - reserved(req.name, direction, exclude), precision), 0.0)
	return frappe._dict(
		amount=amount,
		allowance=allowance,
		purpose=compat.resolve_purpose(req, payment_type, totals),
		totals=totals,
	)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def closed_refusal(req) -> str | None:
	"""Why the request accepts no payment at all (not approved, closed), without throwing."""
	if cint(req.docstatus) != 1:
		return _("Custody Request {0} is not approved.").format(frappe.bold(req.name))
	if cint(req.is_closed):
		return _("Custody Request {0} is closed.").format(frappe.bold(req.name))
	return None


def draft_refusal(req, direction: str, a: frappe._dict) -> str | None:
	"""A float with a draft replenishment Payment Entry cannot be topped up by cheque or transfer (F13)."""
	if direction != OUTGOING or req.custody_type != PERMANENT or a.purpose not in compat.replenish_purposes():
		return None
	drafts = compat.draft_replenishments(req.name)
	if not drafts:
		return None
	return _(
		"Payment Entry {0} is a draft replenishment of {1}. Submit or delete it first, then pay by cheque or transfer."
	).format(frappe.bold(drafts[0]), frappe.bold(req.name))


def nothing_left(req, direction: str, a: frappe._dict) -> str:
	if a.allowance > 0:
		return _("Open cheques or transfers already cover what is left on Custody Request {0}.").format(
			frappe.bold(req.name)
		)
	if direction == OUTGOING:
		return _("Nothing is left to pay on Custody Request {0}.").format(frappe.bold(req.name))
	return _("Nothing is left to return on Custody Request {0}.").format(frappe.bold(req.name))


def refusal(req, direction: str) -> tuple[frappe._dict | None, str | None]:
	"""(available, None) when a cheque / transfer of `direction` can be made now, else (available or None,
	the reason), without throwing (the Treasury buttons of the request)."""
	if reason := closed_refusal(req):
		return None, reason
	a = available(req, direction)
	reason = draft_refusal(req, direction, a)
	if not reason and a.amount <= 0:
		reason = nothing_left(req, direction, a)
	return a, reason or custodian_refusal(req, direction, a)


def _winddown(direction: str, a: frappe._dict) -> bool:
	return direction == INCOMING or a.purpose == REIMBURSEMENT


def custodian_refusal(req, direction: str, a: frappe._dict) -> str | None:
	"""Custody's refusal of the custodian (e.g. inactive), without throwing: the mapper would refuse it too."""
	shown = len(frappe.local.message_log)
	try:
		compat.assert_custodian_usable(req.custodian, winddown=_winddown(direction, a))
	except (frappe.ValidationError, frappe.PermissionError) as e:
		# the message becomes the reason of the button, not a dialog
		del frappe.local.message_log[shown:]
		return str(e) or _("Custodian {0} cannot be paid now.").format(frappe.bold(req.custodian))
	return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_open(req):
	compat.assert_request_payable(req)
	if cint(req.is_closed):
		frappe.throw(_("Custody Request {0} is closed.").format(frappe.bold(req.name)))


def check_request(req, direction: str, amount=None, exclude: str | None = None) -> frappe._dict:
	"""The request-level checks of a new cheque / transfer of `direction` (spec 9 §3.2 a, f, g, h). `amount`:
	the document's amount; None (a mapper) takes the whole available amount, refused when it is 0. Returns the
	`available` result with the `amount` to use."""
	_check_open(req)
	return _check_amount(req, direction, amount, exclude)


def _check_amount(req, direction: str, amount=None, exclude: str | None = None) -> frappe._dict:
	"""`check_request` of a request already checked open (`_check_open`)."""
	a = available(req, direction, exclude, for_update=True)
	if reason := draft_refusal(req, direction, a):
		frappe.throw(reason, title=_("Draft Replenishment Exists"))
	if amount is None:
		if a.amount <= 0:
			frappe.throw(nothing_left(req, direction, a))
		amount = a.amount
	elif flt(amount) - a.amount > TOLERANCE:
		message = (
			_("At most {0} can still be paid on Custody Request {1}.")
			if direction == OUTGOING
			else _("At most {0} can still be returned on Custody Request {1}.")
		)
		currency = erpnext.get_company_currency(req.company)
		frappe.throw(message.format(fmt_money(a.amount, currency=currency), frappe.bold(req.name)))
	compat.assert_custodian_usable(req.custodian, winddown=_winddown(direction, a))
	return frappe._dict(a, amount=flt(amount, _precision()))


def _direction(doc) -> str:
	return doc.direction if doc.doctype == "PDC Cheque" else OUTGOING


def _check_document(doc, req):
	"""The document-level checks (spec 9 §3.2 b-e): the request's custodian and company, no references, a bank
	in the company currency, and for a transfer a Party Payment that is not SWIFT."""
	if doc.company != req.company or doc.party_type != PARTY_TYPE or doc.party != req.custodian:
		frappe.throw(
			_("{0} must be paid to custodian {1} of company {2}.").format(
				_(doc.doctype), frappe.bold(req.custodian_name or req.custodian), frappe.bold(req.company)
			)
		)
	if doc.get("references"):
		frappe.throw(_("A payment to a custodian cannot have references."))
	company_currency = erpnext.get_company_currency(req.company)
	if doc.get("bank_account") and sd.bank_currency(doc.bank_account) != company_currency:
		frappe.throw(
			_("Custody is paid in the company currency only: choose a Bank Account in {0}.").format(
				company_currency
			)
		)
	if doc.doctype == TRANSFER:
		if doc.get("transfer_type") != PARTY_PAYMENT:
			frappe.throw(_("A custody return by transfer is recorded as a Payment Entry in Custody."))
		if doc.get("method") == "SWIFT":
			frappe.throw(_("SWIFT transfers to custodians are not supported."))


# a saved document of a request: these never change (its custodian, company and bank were checked on insert;
# a bank may still be chosen when there was none)
FROZEN_FIELDS = ("party_type", "party", "company", "direction", "bank_account")
# ... and these re-run the checks of the request when they change
RECHECK_FIELDS = ("amount", "transfer_type", "method")


def _is_new(doc) -> bool:
	return not doc.name or doc.is_new()


def _before(doc):
	"""The saved values of `doc` (the document before this save)."""
	return doc.get_doc_before_save() or frappe.get_doc(doc.doctype, doc.name)


def _rows(doc) -> list[tuple]:
	rows = doc.get("references") or []
	return [(r.reference_doctype, r.reference_name, flt(r.allocated_amount)) for r in rows]


def _changed(doc, before, fieldnames) -> list[str]:
	return [
		f for f in fieldnames if doc.meta.has_field(f) and (doc.get(f) or None) != (before.get(f) or None)
	]


def lock_request(name: str):
	"""Lock the custodian, then the request row (custody's lock order), and return the request read under the
	lock: two documents saved at the same time cannot both take what is still available."""
	compat.lock_custodian(frappe.db.get_value(REQUEST, name, "custodian"))
	return frappe.get_doc(REQUEST, name, for_update=True)


def check_saved(doc, req, exclude: str | None = None):
	"""The checks of a document of the request (spec 9 §3.2): open request, document, available amount. They
	run on the request read under the lock, not on `req` (loaded before it)."""
	req = lock_request(req.name)
	_check_open(req)
	_check_document(doc, req)
	_check_amount(req, _direction(doc), doc.amount, exclude)


def _check_request_access(doc, req):
	"""Read on the request; a replacement (lifecycle) may instead come from a user who may write the cheque it
	replaces. One error whether the request is missing or not permitted."""
	if req and frappe.has_permission(REQUEST, "read", req):
		return
	replaces = doc.get("replaces") if doc.flags.treasury_lifecycle else None
	if replaces and frappe.has_permission(doc.doctype, "write", replaces):
		return
	request_not_permitted(doc.source_name)


def request_not_permitted(name: str | None):
	frappe.throw(
		_("Custody Request {0} does not exist or you may not read it.").format(frappe.bold(name or "-")),
		frappe.PermissionError,
		title=_("Not Permitted"),
	)


def get_request(name: str | None):
	"""The Custody Request, or None when it does not exist."""
	if not name or not frappe.db.exists(REQUEST, name):
		return None
	return frappe.get_doc(REQUEST, name)


def recheck(doc):
	"""A lifecycle action that makes a document of the request reserve its amount again (re-presenting a bounced
	cheque): the request must still accept it."""
	if doc.get("source_doctype") != REQUEST:
		return
	if not is_installed():
		frappe.throw(_("The Custody app is not installed."))
	req = get_request(doc.source_name)
	if not req:
		request_not_permitted(doc.source_name)
	req = lock_request(req.name)
	check_request(req, _direction(doc), doc.amount, exclude=doc.name)


def validate(doc, method=None):
	"""doc_events "validate" of PDC Cheque and Bank Transfer (runs after the controller's own validate)."""
	source_doctype = doc.get("source_doctype")
	is_new = _is_new(doc)
	if doc.get("party_type") == PARTY_TYPE and source_doctype != REQUEST:
		# also without custody: the Party Type "Custodian" only exists with it. Only for a new document or a
		# changed party: a custodian cheque of 1.2 (made before the rule) can still be worked and cancelled
		if is_new or (
			not doc.flags.treasury_lifecycle and _changed(doc, _before(doc), ("party_type", "party"))
		):
			frappe.throw(_("A cheque or transfer to a custodian must be created from its Custody Request."))
	if source_doctype != REQUEST:
		return
	if not is_installed():
		frappe.throw(_("The Custody app is not installed."))
	if is_new:
		req = get_request(doc.source_name)
		_check_request_access(doc, req)
		if not req:
			# a replacement whose request no longer exists: refused like a request the user may not read
			request_not_permitted(doc.source_name)
		check_saved(doc, req)
		return
	if doc.flags.treasury_lifecycle:
		# the link never changes (set_only_once, treasury.utils.document_sources); the actions check the rest
		return
	_validate_edit(doc)


def _validate_edit(doc):
	"""A saved document of the request edited through the form / REST (still Created / a draft): its custodian,
	company and bank never change; a new amount (or references, method ...) is checked against the request."""
	before = _before(doc)
	frozen = _changed(doc, before, FROZEN_FIELDS)
	if "bank_account" in frozen and not before.get("bank_account"):
		# a bank chosen after the insert (e.g. a cheque received back): checked like on insert below
		frozen.remove("bank_account")
	if frozen:
		labels = ", ".join(_(doc.meta.get_label(f)) for f in frozen)
		frappe.throw(
			_("{0} cannot be changed on a {1} created from Custody Request {2}.").format(
				labels, _(doc.doctype), frappe.bold(doc.source_name)
			),
			title=_("Not Allowed"),
		)
	if not _changed(doc, before, (*RECHECK_FIELDS, "bank_account")) and _rows(doc) == _rows(before):
		return
	req = get_request(doc.source_name)
	if not req:
		request_not_permitted(doc.source_name)
	check_saved(doc, req, exclude=doc.name)

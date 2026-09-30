"""Bank Transfer lifecycle: statuses, actions and the Payment Entry each transfer posts (spec 7b §3.3, §4).

Submit: a future execution date schedules the transfer (Scheduled, no accounting; the daily job or "Send Now"
sends it); otherwise it is sent at once. Sending posts one Payment Entry through treasury.accounting.posting,
dated the execution date and keyed (Bank Transfer, name, bt_transfer, 1):
- Party Payment: Pay, the bank pays amount + fee, the party gets the amount, the fee is a deduction row;
- Party Receipt: Receive, the bank receives amount - fee, the party is settled with the amount;
- Internal Transfer: amount + fee leave the source bank, the amount (or the received amount of a transfer
  between two currencies) reaches the target bank, the fee is the flagged deduction row.
The transfer is then Sent / Posted, and Completed (its Payment Entry cleared) when the bank confirms it (Mark
Completed, PDC Bank Clearing), or at once per the company's Transfer Completion setting.

Actions (whitelisted methods of the controller): Send Now / Reschedule (Scheduled), Mark Completed (Sent),
Mark Returned (Sent / Completed, Treasury Manager, not for a receipt: reverse Payment Entry or Journal Entry,
the fee kept unless the company's bank refunds it). Cancel: a Scheduled or Sent transfer (its Payment Entry is
cancelled); a Completed or Returned one is returned instead.

Only this module changes the statuses, voucher links, lifecycle dates and the status log of a submitted
transfer: it saves with `flags.treasury_lifecycle`, and the controller rejects any other change to them.
Status log events are stored in English and translated when displayed.
"""

import erpnext
import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.utils import flt, fmt_money, formatdate, getdate, now_datetime, today

from treasury.accounting import dimensions, payment_modes, posting
from treasury.balance_alerts import guard
from treasury.bank_facilities import instruments
from treasury.bank_transfers import fees
from treasury.utils.context import SYSTEM_USER, elevated, real_user
from treasury.utils.protected_fields import insert_log_row

DOCTYPE = "Bank Transfer"
EVENT_TRANSFER = "bt_transfer"
EVENT_TRANSFER_REVERSAL = "bt_transfer_reversal"
USER_ROLES = ("Treasury User", "Treasury Manager", "System Manager")
MANAGER_ROLES = ("Treasury Manager", "System Manager")
# Method of the Modes of Payment map (treasury.accounting.payment_modes.METHODS) per transfer method.
TRANSFER_METHOD = {"Normal": "Bank Transfer", "Fawran": "Fawran", "SWIFT": "SWIFT"}
INTERNAL_METHOD = "Internal Transfer"

PARTY_PAYMENT = "Party Payment"
PARTY_RECEIPT = "Party Receipt"
INTERNAL = "Internal Transfer"
FAWRAN = "Fawran"

DRAFT, SCHEDULED, SENT, COMPLETED, RETURNED, CANCELLED = (
	"Draft",
	"Scheduled",
	"Sent",
	"Completed",
	"Returned",
	"Cancelled",
)
NOT_POSTED, POSTED, REVERSED = "Not Posted", "Posted", "Reversed"

# Treasury Company Settings `transfer_completion`
ON_BANK_CONFIRMATION = "On Bank Confirmation"
ON_SEND_FOR_FAWRAN = "On Send for Fawran"
ON_SEND = "On Send"
BLOCK = "Block"


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _begin(name: str, roles: tuple[str, ...] | None = None, ptype: str = "write", system: bool = False):
	"""Lock the transfer row, reload it and check the user's permission and role (`system`: the daily job)."""
	if not name or not frappe.db.get_value(DOCTYPE, name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(DOCTYPE), name), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} {1} must be submitted first.").format(_(DOCTYPE), frappe.bold(name)))
	if system:
		return doc
	doc.check_permission(ptype)
	if roles and not set(roles) & set(frappe.get_roles()):
		frappe.throw(
			_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in roles)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	return doc


def _save(doc) -> dict:
	# the permission and role were checked by `_begin`; the flag unlocks the lifecycle-only fields
	doc.flags.treasury_lifecycle = True
	doc.flags.ignore_permissions = True
	doc.save()
	return {"name": doc.name, "status": doc.status, "accounting_status": doc.accounting_status}


def log(doc, event: str, status=None, accounting=None, voucher=None, remarks=None, event_date=None):
	"""Append a status log row and apply the new statuses. `event_date`: the date of the event itself."""
	row = doc.append(
		"status_log",
		{
			"datetime": now_datetime(),
			"event_date": getdate(event_date) if event_date else None,
			"user": real_user(),
			"event": event,
			"from_status": doc.status,
			"to_status": status or doc.status,
			"from_accounting": doc.accounting_status,
			"to_accounting": accounting or doc.accounting_status,
			"voucher_type": voucher[0] if voucher and voucher[1] else None,
			"voucher_no": voucher[1] if voucher and voucher[1] else None,
			"remarks": remarks,
		},
	)
	if status:
		doc.status = status
	if accounting:
		doc.accounting_status = accounting
	return row


def _write_in_place(doc, first_new_row: int, fields: tuple[str, ...] = ()):
	"""Submit / cancel hooks, where the document is not saved again: insert the new log rows and write the
	statuses (and `fields`) directly."""
	for row in doc.status_log[first_new_row:]:
		insert_log_row(row)
	values = {"status": doc.status, "accounting_status": doc.accounting_status}
	values.update({f: doc.get(f) for f in fields})
	doc.db_set(values)


def _settings(doc):
	return posting.get_company_settings(doc.company)


def _bank_gl(bank_account: str) -> str:
	account = frappe.db.get_value("Bank Account", bank_account, "account")
	if not account:
		frappe.throw(_("Bank Account {0} has no GL account.").format(frappe.bold(bank_account)))
	return account


def resolve_mode(doc) -> frappe._dict:
	"""Mode of Payment and Payment Entry series of the map (Treasury Company Settings) for the transfer's bank
	and method; an Internal Transfer takes the bank's "Internal Transfer" row first."""
	if doc.transfer_type == INTERNAL:
		mode = payment_modes.resolve(doc.company, doc.bank_account, INTERNAL_METHOD)
		if mode.mode_of_payment or mode.naming_series:
			return mode
	return payment_modes.resolve(doc.company, doc.bank_account, TRANSFER_METHOD.get(doc.method))


def _details(doc) -> dict:
	mode = resolve_mode(doc)
	return {
		"mode_of_payment": doc.mode_of_payment or mode.mode_of_payment,
		"naming_series": mode.naming_series,
		"cost_center": doc.cost_center or erpnext.get_default_cost_center(doc.company),
		"project": doc.project,
		"bank_account": doc.bank_account,
		"party_bank_account": doc.party_bank_account,
		"contact_person": doc.contact_person,
		"dimensions": dimensions.get_dimension_values(doc),
	}


def _remarks(doc) -> str:
	remarks = _("Bank Transfer {0} ({1})").format(doc.name, _(doc.method, context=DOCTYPE))
	if doc.bank_reference:
		remarks += f" {doc.bank_reference}"
	if doc.remarks:
		remarks += "\n" + doc.remarks
	return remarks


def _date(value):
	return getdate(value) if value else getdate(today())


def _not_before(date, reference, label: str):
	if reference and getdate(date) < getdate(reference):
		frappe.throw(
			_("{0} ({1}) cannot be before {2}.").format(label, formatdate(date), formatdate(reference))
		)


def _label(doc, fieldname: str) -> str:
	return _(doc.meta.get_label(fieldname), context=DOCTYPE)


# ---------------------------------------------------------------------------
# Submit, approval, cancel (controller hooks)
# ---------------------------------------------------------------------------


def is_manager() -> bool:
	return bool(set(MANAGER_ROLES) & set(frappe.get_roles()))


def approval_amount(doc) -> float | None:
	"""The company-currency value the approval threshold is compared with (`doc`: a transfer, or a
	`frappe._dict` with its fields):
	- the money that leaves the bank account, fee included (an overridden fee as entered): amount + fee for a
	  Party Payment / Internal Transfer; the amount the party pays for a Party Receipt;
	- converted at the higher of the transfer's exchange rate and the system rate of the execution date, so a
	  rate entered on the transfer can never lower it;
	- for an Internal Transfer into a company-currency account, at least the received amount.
	None when the bank currency has no system rate on that date: the value cannot be verified."""
	company_currency = erpnext.get_company_currency(doc.company)
	fee = 0 if doc.get("transfer_type") == PARTY_RECEIPT else flt(doc.get("fee_amount"))
	amount = flt(doc.get("amount")) + fee
	if doc.get("currency") and doc.currency != company_currency:
		date = getdate(doc.get("execution_date"))
		system_rate = flt(get_exchange_rate(doc.currency, company_currency, date))
		if system_rate <= 0:
			return None
		amount *= max(flt(doc.get("exchange_rate")), system_rate)
	if doc.get("transfer_type") == INTERNAL and doc.get("to_currency") == company_currency:
		amount = max(amount, flt(doc.get("received_amount")))
	return flt(amount, frappe.get_precision(DOCTYPE, "base_amount") or 2)


def approval_required(doc, threshold: float | None = None) -> bool:
	"""Whether the transfer is above the company's approval threshold (0 = no rule), or cannot be valued."""
	if threshold is None:
		threshold = flt(_settings(doc).transfer_approval_threshold)
	if threshold <= 0:
		return False
	value = approval_amount(doc)
	return value is None or value > threshold


def check_approval(doc):
	"""Above the company's approval threshold (company currency, see `approval_amount`) only a Treasury
	Manager may submit."""
	threshold = flt(_settings(doc).transfer_approval_threshold)
	if is_manager() or not approval_required(doc, threshold):
		return
	company_currency = erpnext.get_company_currency(doc.company)
	if approval_amount(doc) is None:
		message = _(
			"There is no exchange rate from {0} to {1} on {2}: the transfer can only be submitted by a Treasury Manager."
		).format(doc.currency, company_currency, formatdate(doc.execution_date))
	else:
		message = _("Transfers above {0} must be submitted by a Treasury Manager.").format(
			fmt_money(threshold, currency=company_currency)
		)
	frappe.throw(message, frappe.PermissionError, title=_("Not Permitted"))


def enforce_limits(doc, execution_date=None, warn: bool = True):
	"""The per-transaction and daily limits of the transfer on its execution date (or `execution_date`),
	race-safe: the Bank Account row is locked first, then the day's transfers are counted with a locking read.
	Block (Treasury Company Settings) rejects the transfer; Warn shows the messages (`warn`)."""
	if not fees.get_limit(doc.company, doc.bank_account, doc.method):
		return
	fees.lock_bank_account(doc.bank_account)
	messages = fees.check_limits(doc, execution_date, lock=True)
	if not messages:
		return
	if _settings(doc).transfer_limit_action == BLOCK:
		frappe.throw("<br>".join(messages), title=_("Transfer Limit Exceeded"))
	if warn:
		frappe.msgprint("<br>".join(messages), title=_("Transfer Limit Exceeded"), indicator="orange")


def balance_banks(doc) -> list[str]:
	"""The bank accounts whose projected balance a transfer changes (the target of an Internal Transfer too)."""
	banks = [doc.bank_account]
	if doc.transfer_type == INTERNAL and doc.get("to_bank_account"):
		banks.append(doc.to_bank_account)
	return banks


def begin_balance_guard(doc, system: bool = False):
	"""Low balance guard, pre-state (treasury.balance_alerts.guard): before the limits (lock order)."""
	return guard.begin(doc.company, balance_banks(doc), system=system)


def finish_balance_guard(doc, snapshot, allow_low_balance=0):
	"""Low balance guard, post-state: a Treasury Manager's override adds a "Low Balance Override" log row
	(returned; the caller writes it)."""
	text = guard.finish(snapshot, doctype=DOCTYPE, name=doc.name, allow_override=allow_low_balance)
	if text:
		return log(doc, "Low Balance Override", remarks=text, event_date=today())
	return None


def on_submit(doc):
	"""A future execution date schedules the transfer; otherwise it is sent now (see the module docstring).
	The low balance guard started in `before_submit` is finished once the transfer's state is written."""
	first_new_row = len(doc.status_log)
	if getdate(doc.execution_date) > getdate(today()):
		log(doc, "Scheduled", SCHEDULED, event_date=today(), remarks=formatdate(doc.execution_date))
		_write_in_place(doc, first_new_row)
	else:
		_send(doc, doc.execution_date)
		_write_in_place(
			doc,
			first_new_row,
			("payment_entry", "base_fee_amount", "completion_date", "bank_transaction_date"),
		)
	first_new_row = len(doc.status_log)
	if finish_balance_guard(doc, doc.flags.pop("balance_snapshot", None), doc.get("allow_low_balance")):
		_write_in_place(doc, first_new_row)


def before_cancel(doc):
	if doc.status == COMPLETED:
		frappe.throw(
			_("Bank Transfer {0} is completed and cannot be cancelled. Return it instead.").format(
				frappe.bold(doc.name)
			),
			title=_("Not Allowed"),
		)
	if doc.status == RETURNED:
		frappe.throw(
			_("Bank Transfer {0} is returned and cannot be cancelled.").format(frappe.bold(doc.name)),
			title=_("Not Allowed"),
		)


def on_cancel(doc):
	"""Cancel the transfer's Payment Entry (before Frappe's check for submitted back links)."""
	first_new_row = len(doc.status_log)
	voucher = None
	if doc.payment_entry and frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus") == 1:
		_cancel_voucher("Payment Entry", doc.payment_entry)
		voucher = ("Payment Entry", doc.payment_entry)
	log(doc, "Cancelled", CANCELLED, NOT_POSTED, voucher=voucher)
	_write_in_place(doc, first_new_row)


def _cancel_voucher(doctype: str, name: str):
	try:
		posting.cancel_voucher(doctype, name)
	except frappe.ValidationError:
		frappe.throw(
			_("{0} {1} could not be cancelled (see the message above).").format(
				_(doctype), frappe.bold(name)
			),
			title=_("Cancellation Failed"),
		)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send(name: str, system: bool = False) -> dict:
	"""Send a Scheduled transfer on its execution date (the daily job: `system=True`, as Administrator, who
	is also the acting user; the approval was checked when the transfer was submitted)."""
	if system:
		with elevated(actor=SYSTEM_USER):
			return _send_scheduled(name, system=True)
	return _send_scheduled(name)


def _send_scheduled(name: str, system: bool = False) -> dict:
	doc = _begin(name, None if system else USER_ROLES, system=system)
	instruments.require_status(doc, (SCHEDULED,))
	# the daily job is not guarded: sending on the execution date changes no projected day
	snapshot = begin_balance_guard(doc, system=system)
	# the limits of the day again (e.g. lowered since the submit): Block leaves the transfer Scheduled
	enforce_limits(doc, warn=False)
	_send(doc, doc.execution_date)
	result = _save(doc)
	_finish_action_guard(doc, snapshot)
	return result


def _finish_action_guard(doc, snapshot, allow_low_balance=0):
	"""Post-state of the guard of an action on a saved transfer: an override row is inserted in place."""
	row = finish_balance_guard(doc, snapshot, allow_low_balance)
	if row:
		insert_log_row(row)


def send_now(name: str, execution_date=None, remarks=None, allow_low_balance=0) -> dict:
	"""Send a Scheduled transfer today (or on a past `execution_date`), which becomes its execution date; the
	limits of that day apply as on submit, and the low balance guard (`allow_low_balance`: a Treasury
	Manager's override)."""
	guard.check_override_flag(allow_low_balance)
	doc = _begin(name, USER_ROLES)
	instruments.require_status(doc, (SCHEDULED,))
	date = _date(execution_date)
	if date > getdate(today()):
		frappe.throw(_("Use Reschedule for a future date."))
	snapshot = begin_balance_guard(doc)
	enforce_limits(doc, date)
	doc.execution_date = date
	_send(doc, date, remarks)
	result = _save(doc)
	_finish_action_guard(doc, snapshot, allow_low_balance)
	return result


def reschedule(name: str, execution_date, remarks=None, allow_low_balance=0) -> dict:
	"""Move a Scheduled transfer to a later date (limits and low balance guard of the new date apply; an
	incoming transfer moved later lowers the days in between)."""
	guard.check_override_flag(allow_low_balance)
	doc = _begin(name, USER_ROLES)
	instruments.require_status(doc, (SCHEDULED,))
	if not execution_date or getdate(execution_date) <= getdate(today()):
		frappe.throw(_("The new execution date must be in the future; use Send Now to send it today."))
	new_date = getdate(execution_date)
	snapshot = begin_balance_guard(doc)
	enforce_limits(doc, new_date)
	note = f"{formatdate(doc.execution_date)} → {formatdate(new_date)}"
	if remarks:
		note += f": {remarks}"
	doc.execution_date = new_date
	log(doc, "Rescheduled", remarks=note, event_date=today())
	result = _save(doc)
	_finish_action_guard(doc, snapshot, allow_low_balance)
	return result


def _send(doc, date, remarks=None):
	"""Post the transfer's Payment Entry on `date` (idempotent on the posting key), status Sent / Posted, and
	Completed at once when the company's Transfer Completion setting says so."""
	date = getdate(date)
	common = {
		"source_doctype": DOCTYPE,
		"source_name": doc.name,
		"event": EVENT_TRANSFER,
		"cycle": 1,
		"company": doc.company,
		"posting_date": date,
		"reference_no": doc.bank_reference or doc.name,
		"reference_date": date,
		"remarks": _remarks(doc),
		"details": _details(doc),
	}
	fee = flt(doc.fee_amount)
	if doc.transfer_type == INTERNAL:
		same_currency = doc.currency == doc.to_currency
		payment_entry = posting.create_internal_transfer(
			**common,
			paid_from=_bank_gl(doc.bank_account),
			paid_to=_bank_gl(doc.to_bank_account),
			paid_amount=flt(doc.amount) + fee,
			received_amount=flt(doc.amount) if same_currency else flt(doc.received_amount),
			charges=fee,
			deduction_account=doc.fee_account if fee else None,
			exchange_rate=doc.exchange_rate,
		)
	else:
		payment_entry = posting.create_payment_entry(
			**common,
			payment_type="Pay" if doc.transfer_type == PARTY_PAYMENT else "Receive",
			party_type=doc.party_type,
			party=doc.party,
			amount=doc.amount,
			bank_account_head=_bank_gl(doc.bank_account),
			currency=doc.currency,
			exchange_rate=doc.exchange_rate,
			references=[
				{
					"reference_doctype": ref.reference_doctype,
					"reference_name": ref.reference_name,
					"allocated_amount": ref.allocated_amount,
				}
				for ref in doc.references
			],
			charges=fee,
			charges_account=doc.fee_account if fee else None,
		)
	doc.payment_entry = payment_entry
	doc.base_fee_amount = _booked_fee(payment_entry, doc.fee_account)
	log(doc, "Sent", SENT, POSTED, ("Payment Entry", payment_entry), remarks=remarks, event_date=date)

	completion = _settings(doc).transfer_completion
	if completion == ON_SEND or (completion == ON_SEND_FOR_FAWRAN and doc.method == FAWRAN):
		_complete(doc, date, None)


def _booked_fee(payment_entry: str, fee_account: str | None) -> float:
	"""The fee deduction booked on the Payment Entry (company currency)."""
	if not fee_account:
		return 0.0
	amounts = frappe.get_all(
		"Payment Entry Deduction",
		filters={"parenttype": "Payment Entry", "parent": payment_entry, "account": fee_account},
		pluck="amount",
	)
	return flt(sum(flt(a) for a in amounts), frappe.get_precision(DOCTYPE, "base_fee_amount"))


# ---------------------------------------------------------------------------
# Completion, return, target confirmation
# ---------------------------------------------------------------------------


def complete(name: str, completion_date=None, bank_transaction_date=None, remarks=None) -> dict:
	"""The bank confirmed the transfer: Completed, its Payment Entry cleared on the bank transaction date
	(else the completion date). The PDC Bank Clearing tool calls this with the user's own permission."""
	doc = _begin(name, USER_ROLES)
	instruments.require_status(doc, (SENT,), COMPLETED)
	date = _date(completion_date)
	bt_date = getdate(bank_transaction_date) if bank_transaction_date else None
	for fieldname, value in (("completion_date", date), ("bank_transaction_date", bt_date)):
		if not value:
			continue
		_not_before(value, doc.execution_date, _label(doc, fieldname))
		if value > getdate(today()) and not frappe.db.get_single_value(
			"Treasury Settings", "allow_future_clearance_date"
		):
			frappe.throw(
				_("{0} {1} cannot be in the future (see Treasury Settings).").format(
					_label(doc, fieldname), formatdate(value)
				)
			)
	_complete(doc, date, bt_date, remarks)
	return _save(doc)


def _complete(doc, date, bank_transaction_date, remarks=None):
	doc.completion_date = getdate(date)
	doc.bank_transaction_date = getdate(bank_transaction_date) if bank_transaction_date else None
	posting.set_clearance_date("Payment Entry", doc.payment_entry, doc.bank_transaction_date or date)
	log(doc, "Completed", COMPLETED, remarks=remarks, event_date=date)


def return_transfer(name: str, return_date=None, reason=None) -> dict:
	"""The transfer came back: its Payment Entry is reversed on the return date — by a reverse Payment Entry
	(an Internal Transfer without fee) or a Journal Entry. The fee stays booked unless the company's bank
	refunds it (Treasury Company Settings: Refund Fees on Return)."""
	doc = _begin(name, MANAGER_ROLES)
	if doc.transfer_type == PARTY_RECEIPT:
		frappe.throw(
			_("A received transfer cannot be returned here; record the refund as a new Party Payment.")
		)
	instruments.require_status(doc, (SENT, COMPLETED), RETURNED)
	if not (reason or "").strip():
		frappe.throw(_("A reason is required."))
	date = _date(return_date)
	_not_before(date, doc.completion_date or doc.execution_date, _label(doc, "return_date"))
	if date > getdate(today()):
		# the reversal is posted on the return date: the money has not come back yet
		frappe.throw(
			_("{0} ({1}) cannot be in the future.").format(_label(doc, "return_date"), formatdate(date))
		)

	fee = flt(doc.fee_amount)
	refund = bool(_settings(doc).refund_fees_on_return)
	kept = None
	if fee and not refund:
		kept = {
			"account": doc.fee_account,
			"bank_account_head": _bank_gl(doc.bank_account),
			"amount_in_account_currency": fee,
		}
	voucher_type, voucher = posting.reverse_voucher(
		"Payment Entry",
		doc.payment_entry,
		date,
		EVENT_TRANSFER_REVERSAL,
		remarks=_("Return of Bank Transfer {0}: {1}").format(doc.name, reason),
		cycle=1,
		kept_charges=kept,
	)
	if voucher_type == "Payment Entry":
		doc.reversal_payment_entry = voucher
	else:
		doc.reversal_journal = voucher
	doc.return_date = date
	doc.return_reason = reason
	doc.fees_refunded = 1 if refund and fee else 0
	log(doc, "Returned", RETURNED, REVERSED, (voucher_type, voucher), remarks=reason, event_date=date)
	return _save(doc)


def confirm_target(name: str, bank_transaction_date) -> dict:
	"""The target bank of an Internal Transfer shows the money (PDC Bank Clearing): logged, no status change."""
	doc = _begin(name, USER_ROLES)
	if doc.transfer_type != INTERNAL:
		frappe.throw(_("Only an Internal Transfer has a target bank account to confirm."))
	instruments.require_status(doc, (SENT, COMPLETED))
	log(doc, "Target Confirmed", event_date=bank_transaction_date or today())
	return _save(doc)


def target_confirmed(names: list[str]) -> set[str]:
	"""The transfers among `names` whose target bank side was already confirmed."""
	if not names:
		return set()
	return set(
		frappe.get_all(
			"Bank Transfer Status Log",
			filters={"parenttype": DOCTYPE, "parent": ["in", names], "event": "Target Confirmed"},
			pluck="parent",
		)
	)

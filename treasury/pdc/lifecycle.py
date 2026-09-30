"""PDC Cheque lifecycle: operational transitions and the accounting events they trigger.

Every public action below is reached through a whitelisted method of the PDC Cheque controller. Each one:
locks the cheque row, reloads it, checks the user's permission (and role where required), validates the
transition, posts accounting through `treasury.accounting.posting` according to Treasury Company Settings,
updates dates / statuses / voucher links, appends status log rows and saves — all in one transaction.

Idempotency: calling an action again once it has happened raises a clear "already ..." validation error and
creates nothing. Underneath, every voucher is created idempotently by the posting service, keyed on
(cheque, event, cycle); `cycle` is incremented when a bounced cheque is re-presented, so each presentation
gets its own vouchers while the history of earlier cycles is kept.

Only this module may change the statuses, lifecycle dates, voucher links and history tables of a cheque:
it saves with `flags.treasury_lifecycle`, and the controller rejects any other change to them. After every
action on a cheque created by a Cheque Batch, the batch's derived status is refreshed (`batch_status`; once
at the end of a batch-wide operation instead).

Date order: an event or voucher may not be dated before the cheque's latest event / voucher date
(`_latest_date`), so no PDC or Under Collection account is ever credited before it was debited.

Status log `event` values (and the from/to statuses) are stored in English, like Select values, and are
translated when displayed (Select formatter / pdc_cheque.js).
"""

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.utils import cint, flt, formatdate, getdate, now_datetime, today

from treasury.accounting import dimensions, payment_modes, posting
from treasury.balance_alerts import guard
from treasury.pdc.batch_status import in_batch_operation, lock_batch, update_batch_status
from treasury.pdc.doctype.cheque_book.cheque_book import record_cancelled_leaf
from treasury.utils.context import SYSTEM_USER, elevated, real_user

DOCTYPE = "PDC Cheque"
OUTGOING = "Outgoing"
INCOMING = "Incoming"
MANAGER = "Treasury Manager"
APPROVER = "Cheque Approver"

# `treasury_event` of the vouchers created for a cheque
EVENT_PARTY = "pdc_party_posting"
EVENT_UNDER_COLLECTION = "pdc_under_collection"
EVENT_BANK = "pdc_bank_posting"
EVENT_BANK_CHARGES = "pdc_bank_charges"
EVENT_BOUNCE_CHARGES = "pdc_bounce_charges"
# Discounted cheques (treasury.bank_facilities.discounting): the drawer paid the bank at maturity, or the
# bank took its money back from the company when the cheque bounced (recourse).
EVENT_DISCOUNT_SETTLEMENT = "pdc_discount_settlement"
EVENT_DISCOUNT_RECOURSE = "pdc_discount_recourse"
REVERSAL_SUFFIX = "_reversal"
# Vouchers undone (in reverse order) when a cheque bounces, is returned or replaced.
UNDOABLE_EVENTS = (EVENT_PARTY, EVENT_UNDER_COLLECTION, EVENT_BANK, EVENT_BANK_CHARGES)

NOT_POSTED = "Not Posted"
PARTY_POSTED = "Party Posted"
UNDER_COLLECTION_POSTED = "Under Collection Posted"
BANK_POSTED = "Bank Posted"
REVERSED = "Reversed"

# Treasury Company Settings `bank_settlement_voucher`: the voucher of the under-collection stage and of the
# bank settlement (the party posting is always a Payment Entry).
PAYMENT_ENTRY_MODE = "Payment Entry"
JOURNAL_ENTRY_MODE = "Journal Entry"

# Accounting details of a cheque (PDC Cheque ACCOUNTING_FIELDS), carried to its replacement.
ACCOUNTING_FIELDS = ("cost_center", "project", "mode_of_payment", "party_bank_account", "contact_person")

# Statuses after which the due date no longer matters.
FINAL_STATUSES = ("Cleared", "Bounced", "Returned", "Endorsed", "Discounted", "Replaced", "Cancelled")

# Per-presentation fields, reset when a bounced cheque is re-presented (history stays in the status log).
CYCLE_FIELDS = (
	"party_payment_entry",
	"under_collection_payment_entry",
	"bank_payment_entry",
	"reversal_payment_entry",
	"under_collection_journal",
	"bank_journal",
	"bank_charges_journal",
	"reversal_journal",
	"bounce_charges_journal",
	"deposit_date",
	"discount_date",
	"cheque_discounting",
	"actual_clearance_date",
	"bank_transaction_date",
	"party_posting_date",
	"bank_posting_date",
)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def log_creation(doc):
	"""Called from `before_insert`: initial statuses and the first status log row."""
	doc.operational_status = "Created"
	doc.accounting_status = NOT_POSTED
	doc.cycle = 1
	doc.is_postponed = 0
	doc.postponement_count = 0
	doc.append(
		"status_log",
		{
			"datetime": now_datetime(),
			"user": frappe.session.user,
			"event": "Created",
			"to_operational": "Created",
			"to_accounting": NOT_POSTED,
		},
	)


def after_insert(doc):
	"""Outgoing cheques in `On Creation` mode are posted to the party as soon as they exist."""
	settings = posting.get_company_settings(doc.company)
	if doc.direction != OUTGOING or settings.outgoing_party_posting != "On Creation":
		return
	# No `_begin` permission check here: the user's *create* permission on PDC Cheque, checked by
	# `Document.insert` before this hook runs, is what authorises this posting. Only after it does the
	# posting service build the Payment Entry in its elevated context (treasury.utils.context.elevated).
	_post_party(doc, doc.issue_date)
	# The parent insert is still running: write the changes in place instead of a nested save.
	# This relies on Frappe's insert order (the parent row and its existing children are already in the
	# database when `after_insert` runs), so only the rows appended by `_post_party` are inserted here.
	# Covered by test_outgoing_on_creation / test_replace_in_on_creation_mode.
	doc.db_update()
	for row in doc.status_log:
		if not row.name or not frappe.db.exists("PDC Status Log", row.name):
			row.db_insert()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def deliver(name: str, handover_date=None, remarks: str | None = None, allow_low_balance=0) -> dict:
	"""Hand an outgoing cheque over. The low balance guard (treasury.balance_alerts.guard) checks the bank's
	projected balance; `allow_low_balance`: a Treasury Manager overrides a Block (logged)."""
	guard.check_override_flag(allow_low_balance)
	doc = _begin(name)
	_require_direction(doc, OUTGOING)
	_require_status(doc, ("Created",), "Delivered")
	date = _date(handover_date)
	_not_before(doc, date, "handover_date", _latest_date(doc))
	snapshot = guard.begin(doc.company, [doc.bank_account])

	doc.handover_date = date
	_log(doc, "Delivered", operational="Delivered", remarks=remarks, event_date=date)
	if _party_mode(doc) == "On Handover":
		_post_party(doc, date)
	result = _save(doc)
	text = guard.finish(snapshot, doctype=DOCTYPE, name=doc.name, allow_override=allow_low_balance)
	if text:
		# the cheque was just saved: the override row is inserted in place
		_log(doc, "Low Balance Override", remarks=text)
		doc.status_log[-1].db_insert()
	return result


def receive(name: str, receipt_date=None, remarks: str | None = None) -> dict:
	doc = _begin(name)
	_require_direction(doc, INCOMING)
	_require_status(doc, ("Created",), "Received")
	date = _date(receipt_date)
	_not_before(doc, date, "handover_date", _latest_date(doc))

	doc.handover_date = date
	_log(doc, "Received", operational="Received", remarks=remarks, event_date=date)
	if _party_mode(doc) == "On Receipt":
		_post_party(doc, date)
	return _save(doc)


def post_party(name: str, posting_date=None, *, system: bool = False) -> dict:
	"""Post the party settlement now ("Post Now"; also used by the `On Cheque Date` scheduler).

	The default posting date is the effective (possibly postponed) due date, or the handover / receipt date
	when the cheque was handed over after its due date.

	`system=True` is the internal entry point of the scheduler (treasury.pdc.scheduler): it skips the user
	permission / role check and runs as Administrator (`treasury.utils.context.elevated`), who is also
	recorded as the acting user, whoever the session user is; every business rule of `_post_party_now`
	still applies. Never pass it from a whitelisted method.
	"""
	if system:
		with elevated(actor=SYSTEM_USER):
			return _post_party_now(name, posting_date, system=True)
	return _post_party_now(name, posting_date)


def _post_party_now(name: str, posting_date=None, system: bool = False) -> dict:
	doc = _begin(name, system=system)
	if doc.accounting_status != NOT_POSTED:
		frappe.throw(_("Cheque {0} is already posted to the party.").format(frappe.bold(doc.name)))
	_require_status(doc, ("Created", "Delivered", "Received", "Deposited"))
	latest = _latest_date(doc, "handover_date")
	if posting_date:
		date = getdate(posting_date)
		_not_before(doc, date, "party_posting_date", latest)
	else:
		date = max(getdate(doc.effective_due_date), latest)
	_post_party(doc, date)
	return _save(doc)


def deposit(
	name: str, deposit_date=None, bank_account: str | None = None, remarks: str | None = None
) -> dict:
	doc = _begin(name)
	_require_direction(doc, INCOMING)
	_require_status(doc, ("Received",), "Deposited")
	date = _date(deposit_date)
	_not_before(doc, date, "deposit_date", _latest_date(doc, "handover_date"))

	_set_bank_account(
		doc,
		bank_account,
		_("Cheque {0} is in {1} and cannot be deposited to Bank Account {2}, which is in {3}."),
	)
	_require_bank_account(doc)

	doc.deposit_date = date
	_log(doc, "Deposited", operational="Deposited", remarks=remarks, event_date=date)
	if _settings(doc).incoming_under_collection == "On Deposit":
		_ensure_party_posted(doc, date)
		_post_under_collection(doc, date)
	return _save(doc)


def clear(
	name: str,
	clearance_date=None,
	bank_transaction_date=None,
	bank_charges=0,
	exchange_rate=None,
	remarks: str | None = None,
	bank_account: str | None = None,
) -> dict:
	"""Mark the cheque cleared by the bank and post the bank settlement according to the settings.

	`exchange_rate` (foreign currency cheques) is the bank rate of the clearance; the difference to the
	rate the cheque was carried at is booked as exchange gain / loss.

	`bank_account`: the bank an incoming cheque received without one was cleared into (same checks as a
	deposit). A cheque that already has a bank account keeps it.
	"""
	doc = _begin(name)
	allowed = ("Delivered",) if doc.direction == OUTGOING else ("Received", "Deposited")
	_require_status(doc, allowed, "Cleared")
	if bank_account and doc.bank_account and bank_account != doc.bank_account:
		frappe.throw(
			_("Cheque {0} is already assigned to Bank Account {1}.").format(
				frappe.bold(doc.name), frappe.bold(doc.bank_account)
			)
		)
	_set_bank_account(
		doc,
		bank_account,
		_("Cheque {0} is in {1} and cannot be cleared into Bank Account {2}, which is in {3}."),
	)
	_require_bank_account(doc)
	date = _date(clearance_date)
	latest = _latest_date(doc, "handover_date", "deposit_date")
	_validate_clearance_date(doc, date, latest)

	mode = _bank_mode(doc)
	transaction_date = getdate(bank_transaction_date) if bank_transaction_date else None
	if mode == "On Bank Transaction Date" and not transaction_date:
		frappe.throw(
			_("Bank Transaction Date is required because the bank posting of this cheque happens on it.")
		)
	if transaction_date:
		_not_before(doc, transaction_date, "bank_transaction_date", latest)
	if flt(bank_charges) < 0:
		frappe.throw(_("Bank charges cannot be negative."))
	_validate_settlement_charges(doc, bank_charges)

	doc.actual_clearance_date = date
	doc.bank_transaction_date = transaction_date
	doc.bank_charges = flt(bank_charges, doc.precision("bank_charges"))
	_log(doc, "Cleared", operational="Cleared", remarks=remarks, event_date=date)
	# the party posting may not come after the bank posting (e.g. a bank transaction before the due date)
	bank_date = transaction_date if mode == "On Bank Transaction Date" else date
	_ensure_party_posted(doc, bank_date)
	if mode != "Manual":
		_post_bank(doc, bank_date, exchange_rate)
	return _save(doc)


def post_bank(
	name: str, posting_date=None, bank_charges=None, exchange_rate=None, bank_transaction_date=None
) -> dict:
	"""Post the bank settlement of a cleared cheque (bank posting mode `Manual`).

	`bank_transaction_date` (e.g. from the PDC Bank Clearing tool) is recorded if the cheque has none yet.
	"""
	doc = _begin(name)
	if doc.accounting_status == BANK_POSTED:
		frappe.throw(_("Cheque {0} is already posted to the bank.").format(frappe.bold(doc.name)))
	_require_status(doc, ("Cleared",))
	latest = _latest_date(doc, "handover_date", "deposit_date")
	if bank_transaction_date and not doc.bank_transaction_date:
		_not_before(doc, bank_transaction_date, "bank_transaction_date", latest)
		doc.bank_transaction_date = getdate(bank_transaction_date)
	date = getdate(posting_date or doc.bank_transaction_date or doc.actual_clearance_date)
	_not_before(doc, date, "bank_posting_date", latest)
	if bank_charges is not None:
		if flt(bank_charges) < 0:
			frappe.throw(_("Bank charges cannot be negative."))
		doc.bank_charges = flt(bank_charges, doc.precision("bank_charges"))
	_validate_settlement_charges(doc, doc.bank_charges)

	_ensure_party_posted(doc, date)
	_post_bank(doc, date, exchange_rate)
	return _save(doc)


def postpone(name: str, new_due_date, reason: str | None = None, contact_person: str | None = None) -> dict:
	"""Move the effective due date. No accounting: a posted party entry stays as it is."""
	# Cheque Approvers hold only read access to the cheque: the role check is what authorises them here.
	doc = _begin(name, roles=(APPROVER, MANAGER), ptype="read")
	if doc.operational_status in FINAL_STATUSES:
		frappe.throw(
			_("Cheque {0} is {1} and cannot be postponed.").format(
				frappe.bold(doc.name), _(doc.operational_status)
			)
		)
	if not reason:
		frappe.throw(_("A reason is required."))
	_apply_postponement(doc, new_due_date, reason, contact_person)
	return _save(doc)


def bounce(name: str, bounce_date=None, reason: str | None = None, charges=0) -> dict:
	"""A discounted cheque that bounces is first taken back by the bank from the company (recourse: Dr
	Discounted Liability / Cr Bank), then its party posting is undone like any other bounce."""
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, ("Delivered", "Received", "Deposited", "Discounted"), "Bounced")
	if not reason:
		frappe.throw(_("A reason is required."))
	if flt(charges) < 0:
		frappe.throw(_("Bounce charges cannot be negative."))
	if flt(charges) and doc.direction != INCOMING:
		frappe.throw(_("Bounce charges can only be charged on incoming cheques."))
	date = _date(bounce_date)
	_not_before(doc, date, None, _latest_date(doc, "handover_date", "deposit_date", "discount_date"))

	discounted = doc.operational_status == "Discounted"
	_log(doc, "Bounced", operational="Bounced", remarks=reason, event_date=date)
	if discounted:
		_post_discount_recourse(doc, date, reason)
	_undo_postings(doc, date, reason)
	if flt(charges) > 0:
		_post_bounce_charges(doc, date, charges)
	result = _save(doc)
	if discounted:
		_refresh_discounting(doc)
	return result


# ---------------------------------------------------------------------------
# Cheque Discounting (treasury.bank_facilities.discounting)
# ---------------------------------------------------------------------------


def discount(name: str, discount_date, cheque_discounting: str, journal: str | None = None) -> dict:
	"""Received → Discounted, called by the submit of a Cheque Discounting batch. No accounting on the cheque:
	it stays in PDC Receivable (the batch posts Dr Bank + Dr Discount Expense / Cr Discounted Liability)."""
	doc = _begin(name, roles=(MANAGER,))
	if error := discount_error(doc, discount_date):
		frappe.throw(error)
	date = getdate(discount_date)
	doc.discount_date = date
	doc.cheque_discounting = cheque_discounting
	voucher = ("Journal Entry", journal) if journal else ("Cheque Discounting", cheque_discounting)
	_log(
		doc,
		"Discounted",
		operational="Discounted",
		voucher=voucher,
		remarks=cheque_discounting,
		event_date=date,
	)
	return _save(doc)


def discount_error(doc, discount_date) -> str | None:
	"""Why cheque `doc` cannot be discounted on `discount_date` (None if it can)."""
	if doc.direction != INCOMING:
		return _("Cheque {0} is not an incoming cheque.").format(frappe.bold(doc.name))
	if doc.operational_status != "Received":
		return _("Cheque {0} is {1}: only Received cheques can be discounted.").format(
			frappe.bold(doc.name), _(doc.operational_status)
		)
	if doc.accounting_status != PARTY_POSTED:
		return _(
			"Cheque {0} must be posted to the party (in PDC Receivable) before it can be discounted; it is {1}."
		).format(frappe.bold(doc.name), _(doc.accounting_status))
	date = getdate(discount_date)
	latest = _latest_date(doc, "handover_date")
	if date < latest:
		return _("The Discount Date ({0}) of cheque {1} cannot be before {2}.").format(
			formatdate(date), frappe.bold(doc.name), formatdate(latest)
		)
	if getdate(doc.effective_due_date) <= date:
		return _(
			"Cheque {0} is due on {1}: only cheques due after the Discount Date can be discounted."
		).format(frappe.bold(doc.name), formatdate(doc.effective_due_date))
	return None


def undiscount(name: str, cheque_discounting: str, remarks: str | None = None) -> dict:
	"""Discounted → Received, called by the cancellation of the Cheque Discounting batch (whose Journal Entry
	is cancelled by the batch). Nothing else was posted on the cheque."""
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, ("Discounted",))
	if doc.cheque_discounting != cheque_discounting:
		frappe.throw(
			_("Cheque {0} is not discounted with {1}.").format(
				frappe.bold(doc.name), frappe.bold(cheque_discounting)
			)
		)
	doc.discount_date = None
	doc.cheque_discounting = None
	_log(doc, "Discount Cancelled", operational="Received", remarks=remarks or cheque_discounting)
	return _save(doc)


def settle_discounted(name: str, settlement_date=None, remarks: str | None = None) -> dict:
	"""The drawer paid the discounted cheque to the bank at maturity: Dr Discounted Liability / Cr PDC
	Receivable. The cheque is Cleared and Bank Posted; `bank_journal` holds this Journal Entry (the voucher
	that took the cheque out of PDC Receivable — the bank itself credited the company at the discount)."""
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, ("Discounted",), "Cleared")
	date = _date(settlement_date)
	_validate_clearance_date(doc, date, _latest_date(doc, "handover_date", "discount_date"))
	if date < getdate(doc.effective_due_date):
		# the bank collects the cheque from the drawer at maturity, not before
		frappe.throw(
			_("Cheque {0} is due on {1}: a discounted cheque cannot be settled before its due date.").format(
				frappe.bold(doc.name), formatdate(doc.effective_due_date)
			)
		)
	from treasury.bank_facilities import discounting

	journal = discounting.post_settlement(doc, date)
	doc.actual_clearance_date = date
	doc.bank_journal = journal
	doc.bank_posting_date = date
	_log(
		doc,
		"Discount Settled",
		operational="Cleared",
		accounting=BANK_POSTED,
		voucher=("Journal Entry", journal),
		remarks=remarks,
		event_date=date,
	)
	discounting.close_item(doc.cheque_discounting, doc.name, discounting.ITEM_SETTLED, date, journal)
	result = _save(doc)
	_refresh_discounting(doc)
	return result


def _post_discount_recourse(doc, posting_date, remarks=None):
	from treasury.bank_facilities import discounting

	journal = discounting.post_recourse(doc, posting_date)
	_log(
		doc,
		"Discount Recourse Posted",
		voucher=("Journal Entry", journal),
		remarks=remarks,
		event_date=posting_date,
	)
	discounting.close_item(doc.cheque_discounting, doc.name, discounting.ITEM_BOUNCED, posting_date, journal)


def _refresh_discounting(doc):
	from treasury.bank_facilities import discounting

	if doc.cheque_discounting:
		discounting.refresh_batch(doc.cheque_discounting)


def represent(name: str, represent_date=None, new_due_date=None, remarks: str | None = None) -> dict:
	"""Present a bounced cheque again: a new cycle whose postings are independent of the bounced one."""
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, ("Bounced",))
	date = _date(represent_date)
	_not_before(doc, date, None, _latest_date(doc, "handover_date", "deposit_date"))

	doc.cycle = cint(doc.cycle) + 1
	for fieldname in CYCLE_FIELDS:
		doc.set(fieldname, None)
	doc.bank_charges = 0
	target = "Delivered" if doc.direction == OUTGOING else "Received"
	_log(doc, "Re-presented", operational=target, accounting=NOT_POSTED, remarks=remarks, event_date=date)
	if new_due_date and getdate(new_due_date) != getdate(doc.effective_due_date):
		_apply_postponement(doc, new_due_date, remarks or _("Re-presented after bounce"))

	if _party_mode(doc) in ("On Handover", "On Receipt", "On Creation"):
		_post_party(doc, date)
	return _save(doc)


def replace(name: str, new_cheque_values: dict | str | None = None) -> dict:
	"""Replace the cheque by a new one; the old one's postings are undone like on a bounce.

	`new_cheque_values`: cheque_no (else the next leaf of the cheque book), amount, original_due_date,
	issue_date, exchange_rate, cheque_book, replacement_date, reason. A cheque that was already handed over /
	received is replaced by one handed over / received on the replacement date. Returns the statuses of the
	replaced cheque and the new cheque name as `replaced_by`.
	"""
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, ("Created", "Delivered", "Received", "Bounced"), "Replaced")
	values = frappe._dict(frappe.parse_json(new_cheque_values) or {})
	date = _date(values.replacement_date)
	_not_before(doc, date, None, _latest_date(doc, "handover_date", "deposit_date"))
	was_handed_over = doc.operational_status != "Created"

	_undo_postings(doc, date, values.reason)
	_log(doc, "Replaced", operational="Replaced", remarks=values.reason, event_date=date)
	_save(doc)

	new = _make_replacement(doc, values, date)
	doc.replaced_by = new.name
	# saved log rows are immutable (validate_protected_fields), so the new cheque gets its own row
	_log(doc, "Replacement Created", voucher=(DOCTYPE, new.name), remarks=values.reason, event_date=date)
	result = _save(doc)

	if was_handed_over:
		(deliver if new.direction == OUTGOING else receive)(new.name, date)
	return {**result, "replaced_by": new.name}


def cancel_cheque(name: str, reason: str | None = None) -> dict:
	"""Cancel a cheque that was never posted, or whose postings were reversed (bounced / returned)."""
	doc = _begin(name, roles=(MANAGER,))
	_require_status(doc, ("Created", "Bounced", "Returned"), "Cancelled")
	if not reason:
		frappe.throw(_("A reason is required."))
	never_posted = doc.accounting_status == NOT_POSTED
	reversed_ = doc.accounting_status == REVERSED
	if not (never_posted or reversed_):
		frappe.throw(
			_(
				"Cheque {0} is posted to accounts. Bounce, return or replace it first so its postings are reversed."
			).format(frappe.bold(doc.name))
		)

	_log(doc, "Cancelled", operational="Cancelled", remarks=reason)
	if doc.direction == OUTGOING and doc.cheque_book:
		record_cancelled_leaf(doc.cheque_book, doc.cheque_no, reason)
	return _save(doc)


def return_to_party(name: str, return_date=None, reason: str | None = None) -> dict:
	"""Give an incoming cheque back to the party (before deposit, or after a bounce)."""
	doc = _begin(name)
	_require_direction(doc, INCOMING)
	_require_status(doc, ("Received", "Bounced"), "Returned")
	if not reason:
		frappe.throw(_("A reason is required."))
	date = _date(return_date)
	_not_before(doc, date, None, _latest_date(doc, "handover_date"))

	_log(doc, "Returned", operational="Returned", remarks=reason, event_date=date)
	_undo_postings(doc, date, reason)
	return _save(doc)


# ---------------------------------------------------------------------------
# Accounting events
# ---------------------------------------------------------------------------


def _post_party(doc, posting_date):
	"""Party settlement: Payment Entry between the party and the PDC Payable / Receivable of the bank."""
	if doc.accounting_status != NOT_POSTED:
		return
	_require_bank_account(doc)
	mapping = posting.get_account_mapping(doc.company, doc.bank_account)
	outgoing = doc.direction == OUTGOING
	if not flt(doc.exchange_rate):
		doc.exchange_rate = posting.get_cheque_exchange_rate(doc.currency, doc.company, posting_date)
	# e.g. the bank chosen at the deposit: its mode of payment, not the one of the bank the cheque had
	doc.set_accounting_defaults()

	payment_entry = posting.create_payment_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_PARTY,
		payment_type="Pay" if outgoing else "Receive",
		company=doc.company,
		posting_date=posting_date,
		party_type=doc.party_type,
		party=doc.party,
		amount=doc.amount,
		bank_account_head=mapping.pdc_payable_account if outgoing else mapping.pdc_receivable_account,
		reference_no=doc.cheque_no,
		reference_date=doc.effective_due_date,
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
		remarks=_voucher_remarks(doc),
		cycle=_cycle(doc),
		details=_details(doc),
	)
	doc.party_payment_entry = payment_entry
	doc.party_posting_date = getdate(posting_date)
	_log(
		doc,
		"Party Posted",
		accounting=PARTY_POSTED,
		voucher=("Payment Entry", payment_entry),
		event_date=posting_date,
	)


def _ensure_party_posted(doc, event_date):
	"""Edge rule: a later event (deposit, clearance) needs the party posting first.

	It is posted on its scheduled date, or on `event_date` when that is earlier (e.g. an `On Cheque Date`
	cheque cleared before its due date) or when the mode has no date (`Manual`).
	"""
	if doc.accounting_status != NOT_POSTED:
		return
	scheduled = _scheduled_party_date(doc)
	date = getdate(event_date)
	if scheduled and getdate(scheduled) < date:
		# never before the handover / receipt or an earlier voucher (e.g. a due date before the handover)
		date = max(getdate(scheduled), _latest_date(doc, "handover_date"))
	_post_party(doc, date)


def _post_under_collection(doc, posting_date):
	"""Incoming deposit: Dr Under Collection / Cr PDC Receivable (an Internal Transfer Payment Entry, or a
	Journal Entry per the settings' Bank Settlement Voucher)."""
	if doc.accounting_status != PARTY_POSTED:
		return
	mapping = posting.get_account_mapping(doc.company, doc.bank_account)
	if not mapping.under_collection_account:
		frappe.throw(
			_("Set the Under Collection Account of Bank Account {0} in Treasury Company Settings.").format(
				frappe.bold(doc.bank_account)
			),
			title=_("Missing Settings"),
		)
	rate = _carrying_rate(doc)
	if _settlement_voucher(doc) == PAYMENT_ENTRY_MODE:
		payment_entry = posting.create_internal_transfer(
			source_doctype=DOCTYPE,
			source_name=doc.name,
			event=EVENT_UNDER_COLLECTION,
			company=doc.company,
			posting_date=posting_date,
			paid_from=mapping.pdc_receivable_account,
			paid_to=mapping.under_collection_account,
			paid_amount=doc.amount,
			exchange_rate=rate,
			reference_no=doc.cheque_no,
			reference_date=doc.effective_due_date,
			remarks=_voucher_remarks(doc),
			details=_details(doc),
			cycle=_cycle(doc),
		)
		doc.under_collection_payment_entry = payment_entry
		_log(
			doc,
			"Under Collection Posted",
			accounting=UNDER_COLLECTION_POSTED,
			voucher=("Payment Entry", payment_entry),
			event_date=posting_date,
		)
		return

	journal = posting.create_journal_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_UNDER_COLLECTION,
		company=doc.company,
		posting_date=posting_date,
		accounts=[
			_row(doc, mapping.under_collection_account, rate, debit=doc.amount),
			_row(doc, mapping.pdc_receivable_account, rate, credit=doc.amount),
		],
		cheque_no=doc.cheque_no,
		cheque_date=doc.effective_due_date,
		remarks=_voucher_remarks(doc),
		cycle=_cycle(doc),
	)
	doc.under_collection_journal = journal
	_log(
		doc,
		"Under Collection Posted",
		accounting=UNDER_COLLECTION_POSTED,
		voucher=("Journal Entry", journal),
		event_date=posting_date,
	)


def _post_bank(doc, posting_date, exchange_rate=None):
	"""Bank settlement: outgoing Dr PDC Payable / Cr Bank; incoming Dr Bank / Cr Under Collection or PDC
	Receivable. The Journal Entry gets its clearance date for the bank reconciliation.

	Bank reconciliation dates, by design: `clearance_date` is the date the bank actually moved the money
	(bank transaction date, else actual clearance date), even when the JE is posted later (`Manual` mode) —
	the Bank Reconciliation Statement then simply never lists it as outstanding. `cheque_date` stays the
	(effective) date of the cheque itself, which may be after the clearance when a cheque clears early.
	"""
	if doc.accounting_status == BANK_POSTED:
		return
	mapping = posting.get_account_mapping(doc.company, doc.bank_account)
	bank_gl = frappe.db.get_value("Bank Account", doc.bank_account, "account")
	if _settlement_voucher(doc) == PAYMENT_ENTRY_MODE:
		_post_bank_payment_entry(doc, mapping, bank_gl, posting_date, exchange_rate)
		return
	carrying_rate = _carrying_rate(doc)
	bank_rate = _bank_rate(doc, posting_date, exchange_rate)

	if doc.direction == OUTGOING:
		accounts = [
			_row(doc, mapping.pdc_payable_account, carrying_rate, debit=doc.amount),
			_row(doc, bank_gl, bank_rate, credit=doc.amount),
		]
	else:
		if doc.accounting_status == UNDER_COLLECTION_POSTED:
			contra = mapping.under_collection_account
		else:
			contra = mapping.pdc_receivable_account
		accounts = [
			_row(doc, bank_gl, bank_rate, debit=doc.amount),
			_row(doc, contra, carrying_rate, credit=doc.amount),
		]

	clearance_date = doc.bank_transaction_date or doc.actual_clearance_date or posting_date
	journal = posting.create_journal_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_BANK,
		company=doc.company,
		posting_date=posting_date,
		accounts=accounts,
		cheque_no=doc.cheque_no,
		cheque_date=doc.effective_due_date,
		clearance_date=clearance_date,
		remarks=_voucher_remarks(doc),
		voucher_type="Bank Entry",
		cycle=_cycle(doc),
		balance_exchange_difference=True,
	)
	doc.bank_journal = journal
	doc.bank_posting_date = getdate(posting_date)
	_log(
		doc,
		"Bank Posted",
		accounting=BANK_POSTED,
		voucher=("Journal Entry", journal),
		event_date=posting_date,
	)

	if flt(doc.bank_charges) > 0:
		_post_bank_charges(doc, mapping, bank_gl, posting_date, bank_rate, clearance_date)


def _post_bank_payment_entry(doc, mapping, bank_gl: str, posting_date, exchange_rate=None):
	"""Bank settlement as one Internal Transfer Payment Entry, the bank charges inside it (on the deduction
	row): outgoing Bank → PDC Payable (paid amount + charges, received amount), incoming Under Collection /
	PDC Receivable → Bank (paid amount, received amount - charges). Both sides are at the carrying rate (one
	currency, one rate: ERPNext forces it); the exchange difference of a foreign cheque is left to Exchange
	Rate Revaluation. Clearance date as for the Journal Entry (see `_post_bank`)."""
	charges = flt(doc.bank_charges)
	if charges and not mapping.bank_charges_account:
		_missing_bank_charges_account(doc)
	carrying_rate = _carrying_rate(doc)
	_warn_carrying_rate(doc, exchange_rate, carrying_rate)
	amount = flt(doc.amount)
	if doc.direction == OUTGOING:
		accounts = {
			"paid_from": bank_gl,
			"paid_to": mapping.pdc_payable_account,
			"paid_amount": amount + charges,
			"received_amount": amount,
		}
	else:
		if doc.accounting_status == UNDER_COLLECTION_POSTED:
			contra = mapping.under_collection_account
		else:
			contra = mapping.pdc_receivable_account
		accounts = {
			"paid_from": contra,
			"paid_to": bank_gl,
			"paid_amount": amount,
			"received_amount": amount - charges,
		}

	clearance_date = doc.bank_transaction_date or doc.actual_clearance_date or posting_date
	payment_entry = posting.create_internal_transfer(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_BANK,
		company=doc.company,
		posting_date=posting_date,
		**accounts,
		exchange_rate=carrying_rate,
		deduction_account=mapping.bank_charges_account,
		reference_no=doc.cheque_no,
		reference_date=doc.effective_due_date,
		remarks=_voucher_remarks(doc),
		details=_details(doc),
		cycle=_cycle(doc),
		clearance_date=clearance_date,
	)
	doc.bank_payment_entry = payment_entry
	doc.bank_posting_date = getdate(posting_date)
	voucher = ("Payment Entry", payment_entry)
	_log(doc, "Bank Posted", accounting=BANK_POSTED, voucher=voucher, event_date=posting_date)
	if charges > 0:
		_log(doc, "Bank Charges Posted", voucher=voucher, event_date=posting_date)


def _validate_settlement_charges(doc, charges):
	"""Payment Entry mode: an incoming cheque's charges are taken from its amount on the same entry."""
	if (
		_settlement_voucher(doc) == PAYMENT_ENTRY_MODE
		and doc.direction == INCOMING
		and flt(charges)
		and flt(charges) >= flt(doc.amount)
	):
		frappe.throw(_("Bank charges must be less than the cheque amount."))


def _warn_carrying_rate(doc, exchange_rate, carrying_rate: float):
	if (
		flt(exchange_rate)
		and doc.currency != erpnext.get_company_currency(doc.company)
		and flt(exchange_rate, 9) != flt(carrying_rate, 9)
	):
		frappe.msgprint(
			_(
				"In Payment Entry mode the cheque is settled at its carrying rate {0}; the exchange difference is booked by Exchange Rate Revaluation."
			).format(frappe.format(carrying_rate, {"fieldtype": "Float", "precision": 9})),
			title=_("Warning"),
			indicator="orange",
		)


def _missing_bank_charges_account(doc):
	frappe.throw(
		_("Set the Bank Charges Account of Bank Account {0} in Treasury Company Settings.").format(
			frappe.bold(doc.bank_account)
		),
		title=_("Missing Settings"),
	)


def _post_bank_charges(doc, mapping, bank_gl: str, posting_date, bank_rate: float, clearance_date):
	"""Dr Bank Charges / Cr Bank. `bank_charges` is in the cheque (bank) currency."""
	if not mapping.bank_charges_account:
		_missing_bank_charges_account(doc)
	charges = flt(doc.bank_charges)
	precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")
	journal = posting.create_journal_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_BANK_CHARGES,
		company=doc.company,
		posting_date=posting_date,
		accounts=[
			{
				"account": mapping.bank_charges_account,
				"debit_in_account_currency": flt(charges * bank_rate, precision),
				"cost_center": erpnext.get_default_cost_center(doc.company),
			},
			_row(doc, bank_gl, bank_rate, credit=charges),
		],
		cheque_no=doc.cheque_no,
		cheque_date=doc.effective_due_date,
		clearance_date=clearance_date,
		remarks=_("Bank charges of cheque {0}").format(doc.cheque_no),
		voucher_type="Bank Entry",
		cycle=_cycle(doc),
		balance_exchange_difference=True,
	)
	doc.bank_charges_journal = journal
	_log(doc, "Bank Charges Posted", voucher=("Journal Entry", journal), event_date=posting_date)


def _post_bounce_charges(doc, posting_date, charges):
	"""Incoming bounce charges recovered from the party: Dr Party / Cr Bounce Charges Account."""
	settings = _settings(doc)
	if not settings.bounce_charges_account:
		frappe.throw(
			_("Set the Bounce Charges Account in Treasury Company Settings of {0}.").format(
				frappe.bold(doc.company)
			),
			title=_("Missing Settings"),
		)
	company_currency = erpnext.get_company_currency(doc.company)
	party_account = posting.get_party_account(doc.party_type, doc.party, doc.company)
	if get_account_currency(party_account) != company_currency:
		frappe.throw(
			_("Bounce charges can only be charged to a party account in the company currency {0}.").format(
				company_currency
			)
		)
	amount = flt(charges, frappe.get_precision("Journal Entry Account", "debit_in_account_currency"))
	journal = posting.create_journal_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_BOUNCE_CHARGES,
		company=doc.company,
		posting_date=posting_date,
		accounts=[
			{
				"account": party_account,
				"party_type": doc.party_type,
				"party": doc.party,
				"debit_in_account_currency": amount,
			},
			{
				"account": settings.bounce_charges_account,
				"credit_in_account_currency": amount,
				"cost_center": erpnext.get_default_cost_center(doc.company),
			},
		],
		cheque_no=doc.cheque_no,
		cheque_date=doc.effective_due_date,
		remarks=_("Bounce charges of cheque {0}").format(doc.cheque_no),
		cycle=_cycle(doc),
	)
	doc.bounce_charges_journal = journal
	_log(doc, "Bounce Charges Posted", voucher=("Journal Entry", journal), event_date=posting_date)


def _undo_postings(doc, posting_date, remarks: str | None = None):
	"""Undo the current cycle's postings, newest first, as `on_bounce` says: cancel them, or reverse them
	dated `posting_date` — an Internal Transfer Payment Entry (no deductions) by a reverse Payment Entry, the
	party Payment Entry and any Journal Entry by a Journal Entry (`posting.reverse_voucher`: the kind follows
	each voucher, not the current settings). Nothing to do if nothing was posted (edge rule)."""
	if doc.accounting_status in (NOT_POSTED, REVERSED):
		return
	cancel = _settings(doc).on_bounce == "Cancel Payment Entry"
	vouchers = [
		v
		for v in posting.get_linked_vouchers(DOCTYPE, doc.name)
		if v.docstatus == 1 and cint(v.treasury_cycle) == _cycle(doc) and v.treasury_event in UNDOABLE_EVENTS
	]
	for voucher in reversed(vouchers):
		if cancel:
			_cancel_voucher(voucher.doctype, voucher.name)
			_log(
				doc,
				"Voucher Cancelled",
				voucher=(voucher.doctype, voucher.name),
				remarks=remarks,
				event_date=posting_date,
			)
			continue
		reversal_doctype, reversal = posting.reverse_voucher(
			voucher.doctype,
			voucher.name,
			posting_date,
			voucher.treasury_event + REVERSAL_SUFFIX,
			remarks=_("Reversal of {0} {1}").format(_(voucher.doctype), voucher.name),
			cycle=_cycle(doc),
		)
		if voucher.treasury_event == EVENT_PARTY:
			doc.reversal_journal = (
				reversal  # always a Journal Entry (see posting.can_reverse_by_payment_entry)
			)
		elif reversal_doctype == "Payment Entry":
			doc.reversal_payment_entry = reversal
		_log(
			doc,
			"Voucher Reversed",
			voucher=(reversal_doctype, reversal),
			remarks=remarks,
			event_date=posting_date,
		)
	_log(doc, "Reversed", accounting=REVERSED, remarks=remarks, event_date=posting_date)


def _cancel_voucher(doctype: str, name: str):
	try:
		posting.cancel_voucher(doctype, name)
	except frappe.ValidationError:
		frappe.throw(
			_(
				"{0} {1} could not be cancelled (see the message above). If its accounting period is closed, set On Bounce to 'Reverse by Journal Entry' in Treasury Company Settings."
			).format(_(doctype), frappe.bold(name)),
			title=_("Cancellation Failed"),
		)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _begin(name: str, roles: tuple[str, ...] | None = None, ptype: str = "write", system: bool = False):
	"""Lock the cheque row, reload it and check permissions.

	`system`: internal service call (scheduler, run inside `elevated`) — no user permission / role check.
	The vouchers themselves are built by the posting service in an elevated context, *after* this check.

	A cheque of a Cheque Batch locks the batch row first (always batch before cheque, so no deadlock): the
	actions on cheques of one batch are serialised and the batch status is never refreshed from stale data.
	"""
	batch = name and frappe.db.get_value(DOCTYPE, name, "cheque_batch")  # insert-only, never changes
	if batch:
		lock_batch(batch)
	if not name or not frappe.db.get_value(DOCTYPE, name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(DOCTYPE), name), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
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
	# the permission (and role) was checked by `_begin`; the flag unlocks the lifecycle-only fields
	doc.flags.treasury_lifecycle = True
	doc.flags.ignore_permissions = True
	doc.save()
	# a batch operation (submit, cancel, Deliver All) refreshes its batch once at the end instead
	if doc.get("cheque_batch") and not in_batch_operation(doc.cheque_batch):
		update_batch_status(doc.cheque_batch)
	return {
		"name": doc.name,
		"operational_status": doc.operational_status,
		"accounting_status": doc.accounting_status,
	}


def _settings(doc):
	return posting.get_company_settings(doc.company)


def _settlement_voucher(doc) -> str:
	"""Voucher of the under-collection stage and the bank settlement (`bank_settlement_voucher`)."""
	return _settings(doc).bank_settlement_voucher or PAYMENT_ENTRY_MODE


def _details(doc) -> dict:
	"""Accounting details of the cheque's Payment Entries (posting._apply_details): the cheque's own values,
	else the Mode of Payment / series mapped to its bank and direction, else the company's cost center."""
	mode = payment_modes.resolve(doc.company, doc.bank_account, payment_modes.CHEQUE_METHOD[doc.direction])
	return {
		"mode_of_payment": doc.get("mode_of_payment") or mode.mode_of_payment,
		"naming_series": mode.naming_series,
		"cost_center": doc.get("cost_center") or erpnext.get_default_cost_center(doc.company),
		"project": doc.get("project"),
		"bank_account": doc.bank_account,
		"party_bank_account": doc.get("party_bank_account"),
		"contact_person": doc.get("contact_person"),
		"dimensions": dimensions.get_dimension_values(doc),
	}


def _party_mode(doc) -> str:
	settings = _settings(doc)
	return settings.outgoing_party_posting if doc.direction == OUTGOING else settings.incoming_party_posting


def _bank_mode(doc) -> str:
	settings = _settings(doc)
	return settings.outgoing_bank_posting if doc.direction == OUTGOING else settings.incoming_bank_posting


def _scheduled_party_date(doc):
	mode = _party_mode(doc)
	if mode in ("On Handover", "On Receipt"):
		return doc.handover_date
	if mode == "On Cheque Date":
		return doc.effective_due_date
	if mode == "On Creation":
		return doc.issue_date
	return None


def _cycle(doc) -> int:
	return cint(doc.cycle) or 1


def _carrying_rate(doc) -> float:
	"""Rate the cheque is carried at in the PDC accounts (the party posting rate)."""
	return flt(doc.exchange_rate) or 1.0


def _bank_rate(doc, posting_date, exchange_rate=None) -> float:
	if doc.currency == erpnext.get_company_currency(doc.company):
		return 1.0
	if flt(exchange_rate):
		return flt(exchange_rate)
	return posting.get_cheque_exchange_rate(doc.currency, doc.company, posting_date)


def _row(doc, account: str, rate: float, debit=0, credit=0) -> dict:
	"""Journal Entry row in the cheque currency (all PDC and bank accounts of a cheque share it)."""
	row = {
		"account": account,
		"debit_in_account_currency": flt(debit),
		"credit_in_account_currency": flt(credit),
	}
	if doc.currency != erpnext.get_company_currency(doc.company):
		row["exchange_rate"] = flt(rate)
	return row


def _voucher_remarks(doc) -> str:
	remarks = _("Cheque {0} ({1})").format(doc.cheque_no, doc.name)
	if doc.get("remarks"):
		remarks += "\n" + doc.remarks
	return remarks


def _log(doc, event: str, operational=None, accounting=None, voucher=None, remarks=None, event_date=None):
	"""Append a status log row and apply the new statuses.

	`event_date`: the date of the event itself (the date the user entered, or the voucher posting date),
	distinct from `datetime`, the moment the row was recorded. Reports filter on it (e.g. bounce dates).
	"""
	doc.append(
		"status_log",
		{
			"datetime": now_datetime(),
			"event_date": getdate(event_date) if event_date else None,
			"user": real_user(),
			"event": event,
			"from_operational": doc.operational_status,
			"to_operational": operational or doc.operational_status,
			"from_accounting": doc.accounting_status,
			"to_accounting": accounting or doc.accounting_status,
			"voucher_type": voucher[0] if voucher else None,
			"voucher_no": voucher[1] if voucher else None,
			"remarks": remarks,
		},
	)
	if operational:
		doc.operational_status = operational
	if accounting:
		doc.accounting_status = accounting


def _apply_postponement(doc, new_due_date, reason: str | None, contact_person: str | None = None):
	if not new_due_date:
		frappe.throw(_("The new due date is required."))
	new_date = getdate(new_due_date)
	old_date = getdate(doc.effective_due_date)
	if new_date == old_date:
		frappe.throw(_("The new due date is the same as the current due date."))
	_not_before(doc, new_date, None, doc.issue_date)

	doc.append(
		"postponements",
		{
			"datetime": now_datetime(),
			"user": frappe.session.user,
			"old_due_date": old_date,
			"new_due_date": new_date,
			"reason": reason,
			"contact_person": contact_person,
			"approved_by": frappe.session.user,
		},
	)
	doc.effective_due_date = new_date
	doc.is_postponed = 1
	doc.postponement_count = cint(doc.postponement_count) + 1
	_log(doc, "Postponed", remarks=f"{formatdate(old_date)} → {formatdate(new_date)}: {reason or ''}")


def _make_replacement(doc, values: frappe._dict, date):
	new = frappe.new_doc(DOCTYPE)
	# the replacement stands for the same installment of the cheque's batch, if any, and for the same
	# security cheque encashment (the security cheque keeps pointing at the first PDC Cheque; `replaced_by`
	# leads from there to the replacement)
	for fieldname in (
		"direction",
		"company",
		"party_type",
		"party",
		"bank_account",
		"drawer_bank",
		"remarks",
		"cheque_batch",
		"cheque_batch_row",
		"security_cheque",
		*ACCOUNTING_FIELDS,
	):
		new.set(fieldname, doc.get(fieldname))
	dimensions.copy_dimensions(doc, new)
	new.cheque_no = values.cheque_no
	if values.cheque_book:
		new.cheque_book = values.cheque_book
	elif doc.direction == OUTGOING and not values.cheque_no:
		new.cheque_book = doc.cheque_book
	new.amount = flt(values.amount) or doc.amount
	new.issue_date = getdate(values.issue_date or date)
	new.original_due_date = getdate(
		values.original_due_date or max(getdate(doc.effective_due_date), new.issue_date)
	)
	if flt(values.exchange_rate):
		new.exchange_rate = flt(values.exchange_rate)
	new.replaces = doc.name
	# keeps `replaces` and the carried batch / security cheque links (lifecycle-only fields) on insert
	new.flags.treasury_lifecycle = True
	for ref in doc.references:
		new.append(
			"references",
			{
				"reference_doctype": ref.reference_doctype,
				"reference_name": ref.reference_name,
				"allocated_amount": ref.allocated_amount,
			},
		)
	new.insert()
	return new


def _require_direction(doc, direction: str):
	if doc.direction != direction:
		frappe.throw(_("This action is only available for {0} cheques.").format(_(direction)))


def _require_status(doc, allowed: tuple[str, ...], target: str | None = None):
	if target and doc.operational_status == target:
		frappe.throw(_("Cheque {0} is already {1}.").format(frappe.bold(doc.name), _(target)))
	if doc.operational_status not in allowed:
		frappe.throw(
			_("This action is not allowed while cheque {0} is {1}.").format(
				frappe.bold(doc.name), _(doc.operational_status)
			)
		)


def _set_bank_account(doc, bank_account: str | None, currency_message: str):
	"""Move a not yet posted incoming cheque to `bank_account` (deposit / clearance into another bank).

	`currency_message` ({0} cheque, {1} its currency, {2} bank account, {3} bank currency) is raised when
	the bank is in another currency: the amount (and any exchange rate) was entered in the cheque currency.
	"""
	if not bank_account or bank_account == doc.bank_account:
		return
	if doc.accounting_status != NOT_POSTED:
		frappe.throw(_("The bank account cannot be changed after the cheque is posted to accounts."))
	currency = doc.currency
	doc.bank_account = bank_account
	doc.validate_bank_account()
	doc.set_currency_and_rate()
	if currency and doc.currency != currency:
		frappe.throw(
			currency_message.format(frappe.bold(doc.name), currency, frappe.bold(bank_account), doc.currency)
		)


def _require_bank_account(doc):
	if not doc.bank_account:
		frappe.throw(_("Set the Bank Account of cheque {0} first.").format(frappe.bold(doc.name)))


def _date(value):
	return getdate(value) if value else getdate(today())


def _latest_date(doc, *fieldnames: str):
	"""Latest of the issue date, the given event dates of the cheque and the posting dates of all its
	submitted vouchers (every cycle, reversals included). A new event or voucher may not be dated before it,
	so e.g. a bank JE never precedes the party PE and a reversal never precedes the voucher it reverses."""
	dates = [getdate(doc.get(f)) for f in ("issue_date", *fieldnames) if doc.get(f)]
	dates += [
		getdate(v.posting_date) for v in posting.get_linked_vouchers(DOCTYPE, doc.name) if v.docstatus == 1
	]
	return max(dates)


def _not_before(doc, date, fieldname: str | None, reference_date):
	"""`date` may not be earlier than `reference_date` (usually `_latest_date`)."""
	if reference_date and getdate(date) < getdate(reference_date):
		label = _(doc.meta.get_label(fieldname)) if fieldname else _("The date")
		frappe.throw(
			_("{0} ({1}) cannot be before {2}.").format(label, formatdate(date), formatdate(reference_date))
		)


def _validate_clearance_date(doc, date, latest):
	_not_before(doc, date, "actual_clearance_date", latest)
	if getdate(date) > getdate(today()) and not frappe.db.get_single_value(
		"Treasury Settings", "allow_future_clearance_date"
	):
		frappe.throw(
			_("Clearance Date {0} cannot be in the future (see Treasury Settings).").format(formatdate(date))
		)

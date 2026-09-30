"""Shared services of the instruments issued under a Bank Facility (PLAN.md §8): Letters of Guarantee (lg.py),
Letters of Credit (lc.py) and Cheque Discounting batches (discounting.py).

- Facility checks: the instrument's sub-limit exists, the facility is Active on the instrument's date, and the
  amount fits the available sub-limit / total limit. `lock_facility` (the drawdown pattern) serialises every
  instrument, drawdown and facility action of one facility before the limit is checked.
- Lifecycle plumbing of the submitted LG / LC documents: `begin` (lock the row, reload, write permission and
  the Treasury Manager role), `log` (status log row with the event date, the open amount after the event and
  the voucher), `save` (flag `treasury_lifecycle`, display utilisation refreshed) and
  `validate_protected_fields` (the fields and the log only the actions may change on a submitted document).
- `open_amounts`: the open amount of each instrument of a facility, now or on a past date (from the status
  log, whose rows carry the open amount after every event).
- The Journal Entries every instrument posts (cash margin, commission / charges), created by
  treasury.accounting.posting (idempotent on source, event and cycle). The margin account and the bank are in
  the instrument currency: the margin is carried at the issue rate, the bank moves at the event's rate, and a
  foreign currency difference goes to the company's Exchange Gain / Loss account.
"""

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.utils import cint, flt, fmt_money, formatdate, getdate, now_datetime, today

from treasury.accounting import posting
from treasury.bank_facilities import accounting
from treasury.bank_facilities.facility import ACTIVE
from treasury.bank_facilities.utilisation import get_utilisation, refresh_utilisation
from treasury.utils.context import real_user
from treasury.utils.protected_fields import changed_fields, changed_tables, insert_log_row

FACILITY = "Bank Facility"
MANAGER = "Treasury Manager"
LOG_DOCTYPE = "Bank Instrument Status Log"
# Facility values that change whenever its utilisation or status may have changed (concurrency check).
FACILITY_STAMP_FIELDS = ["modified", "status", "utilisation_refreshed_on"]


# ---------------------------------------------------------------------------
# Facility
# ---------------------------------------------------------------------------


def get_limit(facility_doc, limit_type: str):
	return next((row for row in facility_doc.limits if row.limit_type == limit_type), None)


def set_facility_details(doc, limit_type: str):
	"""Company, bank and currency come from the facility, which must have a `limit_type` sub-limit; the bank
	account defaults to the facility's. Returns (facility, sub-limit row)."""
	if not doc.facility:
		frappe.throw(_("Bank Facility is required."))
	facility = frappe.get_doc(FACILITY, doc.facility)
	doc.company, doc.bank, doc.currency = facility.company, facility.bank, facility.currency
	limit = get_limit(facility, limit_type)
	if not limit:
		frappe.throw(
			_("Bank Facility {0} has no {1} sub-limit.").format(
				frappe.bold(facility.name), _(limit_type, context=FACILITY)
			)
		)
	if not doc.bank_account:
		doc.bank_account = facility.default_bank_account
	return facility, limit


def validate_bank_account(doc):
	"""A company bank account of the instrument's company, in the facility currency."""
	if not doc.bank_account:
		frappe.throw(_("Bank Account is required."))
	bank_account = frappe.db.get_value(
		"Bank Account", doc.bank_account, ["company", "is_company_account", "account"], as_dict=True
	)
	if not bank_account or not bank_account.is_company_account or bank_account.company != doc.company:
		frappe.throw(
			_("Bank Account {0} must be a company bank account of {1}.").format(
				frappe.bold(doc.bank_account), frappe.bold(doc.company)
			)
		)
	if not bank_account.account:
		frappe.throw(_("Bank Account {0} has no GL account.").format(frappe.bold(doc.bank_account)))
	currency = get_account_currency(bank_account.account)
	if currency != doc.currency:
		frappe.throw(
			_("Bank Account {0} is in {1}, but the facility is in {2}.").format(
				frappe.bold(doc.bank_account), currency, doc.currency
			)
		)


def set_exchange_rate(doc, date):
	"""Instrument currency → company currency on `date` (1 for the company currency)."""
	if doc.currency == erpnext.get_company_currency(doc.company):
		doc.exchange_rate = 1
	elif not flt(doc.exchange_rate) or flt(doc.exchange_rate) == 1:
		doc.exchange_rate = posting.get_cheque_exchange_rate(doc.currency, doc.company, date)


def event_rate(doc, date, exchange_rate=None) -> float:
	"""The rate of an event after the issue (the bank moves at it): given, else the rate on `date`."""
	if doc.currency == erpnext.get_company_currency(doc.company):
		return 1.0
	if flt(exchange_rate) < 0:
		frappe.throw(_("Exchange Rate cannot be negative."))
	return flt(exchange_rate) or posting.get_cheque_exchange_rate(doc.currency, doc.company, date)


def validate_account(
	company: str, account: str, label: str, root_type: str | None, currency: str | None, allow_party=False
):
	"""A ledger account of the company (root type / currency when given); no Payable / Receivable account
	unless `allow_party`."""
	details = frappe.db.get_value(
		"Account",
		account,
		["company", "is_group", "root_type", "account_type", "account_currency"],
		as_dict=True,
	)
	if not details or details.company != company:
		frappe.throw(
			_("{0} {1} does not belong to company {2}.").format(
				label, frappe.bold(account), frappe.bold(company)
			)
		)
	if details.is_group:
		frappe.throw(_("{0} {1} cannot be a group account.").format(label, frappe.bold(account)))
	if root_type and details.root_type != root_type:
		frappe.throw(
			_("{0} {1} must have Root Type '{2}'.").format(label, frappe.bold(account), _(root_type))
		)
	if not allow_party and details.account_type in ("Payable", "Receivable"):
		frappe.throw(
			_("{0} {1} cannot be a Payable or Receivable account.").format(label, frappe.bold(account))
		)
	if currency and details.account_currency != currency:
		frappe.throw(_("{0} {1} must be in {2}.").format(label, frappe.bold(account), currency))
	return details


def validate_margin_and_commission(doc, facility, limit, amount_field: str = "amount"):
	"""Cash margin and commission of an LG / LC, from its percentages (default: the sub-limit's)."""
	precision = doc.precision(amount_field)
	amount = flt(doc.get(amount_field), precision)
	if doc.get("cash_margin_percent") in (None, ""):
		doc.cash_margin_percent = flt(limit.cash_margin_percent)
	if doc.get("commission_rate") in (None, ""):
		doc.commission_rate = flt(limit.commission_rate)
	for fieldname in ("cash_margin_percent", "commission_rate"):
		if not 0 <= flt(doc.get(fieldname)) <= 100:
			frappe.throw(_("{0} must be between 0 and 100.").format(_(doc.meta.get_label(fieldname))))
	doc.cash_margin_amount = flt(amount * flt(doc.cash_margin_percent) / 100, precision)
	if flt(doc.commission_rate):
		doc.commission_amount = flt(amount * flt(doc.commission_rate) / 100, precision)
	doc.commission_amount = flt(doc.commission_amount, precision)
	if doc.commission_amount < 0:
		frappe.throw(_("The commission cannot be negative."))

	if doc.cash_margin_amount:
		if not doc.margin_account:
			frappe.throw(_("Margin Account is required for the cash margin."))
		label = _("Margin Account")
		details = validate_account(doc.company, doc.margin_account, label, "Asset", doc.currency)
		if details.account_type in ("Bank", "Cash"):
			# the margin is held by the bank apart from the account balance: a bank / cash account would
			# show it as available money (and in the bank reconciliation)
			frappe.throw(_("The Margin Account cannot be a Bank or Cash account."))
	if not doc.commission_account:
		doc.commission_account = facility.commission_expense_account
	if doc.commission_amount or doc.commission_account:
		if not doc.commission_account:
			frappe.throw(_("Commission Account is required for the commission."))
		validate_expense_account(doc, doc.commission_account, _("Commission Account"))


def validate_expense_account(doc, account: str, label: str):
	validate_account(doc.company, account, label, "Expense", erpnext.get_company_currency(doc.company))


def lock_facility(facility: str):
	"""Lock the facility row and make sure the facility / ledger data read so far is current.

	Under REPEATABLE READ, what was read before the lock comes from the transaction's snapshot. Every
	instrument, drawdown, repayment and facility action refreshes the facility's utilisation stamp (or its
	status / modified), so comparing the snapshot values with the locking (current) read detects an instrument
	committed meanwhile; the user is asked to try again rather than checking the limit on stale data."""
	fields = FACILITY_STAMP_FIELDS
	seen = frappe.db.get_value(FACILITY, facility, fields, as_dict=True)
	current = frappe.db.get_value(FACILITY, facility, fields, as_dict=True, for_update=True)
	if seen != current:
		frappe.throw(
			_("Bank Facility {0} was changed by another transaction. Please try again.").format(
				frappe.bold(facility)
			),
			title=_("Concurrent Update"),
		)
	return frappe.get_doc(FACILITY, facility)


def validate_facility_open(facility, date, label: str):
	"""The facility is Active and `date` is inside its period."""
	if facility.status != ACTIVE:
		frappe.throw(
			_("Bank Facility {0} is {1}: a {2} needs an Active facility.").format(
				frappe.bold(facility.name), _(facility.status, context=FACILITY), label
			)
		)
	date = getdate(date)
	if date < getdate(facility.start_date) or date > getdate(facility.expiry_date):
		frappe.throw(
			_("The date {0} is outside the facility period {1} - {2}.").format(
				formatdate(date), formatdate(facility.start_date), formatdate(facility.expiry_date)
			)
		)


def check_available(facility, limit_type: str, amount: float, currency: str, what: str):
	"""`amount` must fit the available `limit_type` sub-limit (and the total limit) of `facility`."""
	utilisation = get_utilisation(facility, limit_type)
	available = utilisation.limits[0].available if utilisation.limits else 0
	if flt(amount) > flt(available):
		frappe.throw(
			_(
				"{0} of {1} exceeds the available {2} limit of Bank Facility {3}: available {4} (total available {5})."
			).format(
				what,
				fmt_money(amount, currency=currency),
				_(limit_type, context=FACILITY),
				frappe.bold(facility.name),
				fmt_money(available, currency=currency),
				fmt_money(utilisation.available, currency=currency),
			),
			title=_("Limit Exceeded"),
		)


# ---------------------------------------------------------------------------
# Lifecycle plumbing (submitted LG / LC)
# ---------------------------------------------------------------------------


def begin(doctype: str, name: str, system: bool = False):
	"""Lock the instrument row, reload it and check the user's write permission and the manager role.

	`system`: the daily job (no acting user) — no permission check."""
	if not name or not frappe.db.get_value(doctype, name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(doctype), name), frappe.DoesNotExistError)
	doc = frappe.get_doc(doctype, name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} {1} must be submitted first.").format(_(doctype), frappe.bold(name)))
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


def log(doc, event: str, status=None, event_date=None, voucher=None, remarks=None):
	"""Append a status log row (the open amount after the event) and apply the new status."""
	amount_field = "open_amount" if doc.meta.has_field("open_amount") else "outstanding_amount"
	row = doc.append(
		"status_log",
		{
			"datetime": now_datetime(),
			"event_date": getdate(event_date) if event_date else None,
			"user": real_user(),
			"event": event,
			"from_status": doc.status,
			"to_status": status or doc.status,
			"open_amount": flt(doc.get(amount_field)),
			"voucher_type": voucher[0] if voucher and voucher[1] else None,
			"voucher_no": voucher[1] if voucher and voucher[1] else None,
			"remarks": remarks,
		},
	)
	if status:
		doc.status = status
	return row


def log_now(doc, event: str, status=None, event_date=None, voucher=None, remarks=None, values=None):
	"""`log` for the submit / cancel hooks, where the document itself is not saved again: the row is inserted
	and the status (and `values`) written directly."""
	row = log(doc, event, status, event_date, voucher, remarks)
	insert_log_row(row)
	doc.db_set({"status": doc.status, **(values or {})})


def save(doc) -> dict:
	# the permission and role were checked by `begin`; the flag unlocks the lifecycle-only fields
	doc.flags.treasury_lifecycle = True
	doc.flags.ignore_permissions = True
	doc.save()
	refresh_utilisation(doc.facility)
	return {"name": doc.name, "status": doc.status}


def validate_protected_fields(doc, fields: tuple[str, ...], tables: tuple[str, ...] = ("status_log",)):
	"""On a submitted instrument, `fields` (allowed on submit for the actions) and the append-only `tables`
	change only through the actions (`flags.treasury_lifecycle`)."""
	before = doc.get_doc_before_save()
	if not before:
		return
	lifecycle_save = bool(doc.flags.treasury_lifecycle)
	changed = [] if lifecycle_save else changed_fields(doc, before, fields)
	changed += changed_tables(doc, before, tables, lifecycle_save)
	if changed:
		frappe.throw(
			_("{0} can only be changed by the {1} actions.").format(
				", ".join(_(doc.meta.get_label(f)) for f in changed), _(doc.doctype)
			),
			title=_("Not Allowed"),
		)


def reset_draft_fields(doc, fields: tuple[str, ...]):
	"""Draft saves and the submit itself (whose document comes from the client): the lifecycle fields are
	empty until `on_submit` sets them, and the status log keeps its stored rows (a new document's log is
	emptied by the controller's `insert` before `before_insert` writes the first row)."""
	for fieldname in fields:
		df = doc.meta.get_field(fieldname)
		doc.set(fieldname, 0 if df and df.fieldtype in ("Currency", "Float", "Int") else None)
	if doc.is_new():
		return
	before = doc.get_doc_before_save()
	if before and changed_tables(doc, before, ("status_log",), False):
		frappe.throw(
			_("{0} can only be changed by the {1} actions.").format(_("Status Log"), _(doc.doctype)),
			title=_("Not Allowed"),
		)


def action_date(value):
	return getdate(value) if value else getdate(today())


def not_before(doc, date, first_date_field: str):
	"""An action may not be dated before the instrument's first date or its latest dated event."""
	dates = [getdate(row.event_date) for row in doc.status_log if row.event_date]
	latest = max([getdate(doc.get(first_date_field)), *dates])
	if getdate(date) < latest:
		frappe.throw(_("The date ({0}) cannot be before {1}.").format(formatdate(date), formatdate(latest)))


def require_status(doc, allowed: tuple[str, ...], already: str | None = None):
	if already and doc.status == already:
		frappe.throw(
			_("{0} {1} is already {2}.").format(
				_(doc.doctype), frappe.bold(doc.name), _(already, context=doc.doctype)
			)
		)
	if doc.status not in allowed:
		frappe.throw(
			_("This action is not allowed while {0} {1} is {2}.").format(
				_(doc.doctype), frappe.bold(doc.name), _(doc.status, context=doc.doctype)
			)
		)


# ---------------------------------------------------------------------------
# Utilisation
# ---------------------------------------------------------------------------


def open_amounts(doctype: str, facility: str, open_statuses: tuple[str, ...], as_on=None) -> dict[str, float]:
	"""{instrument: open amount} of the submitted `doctype` documents of `facility` that are open (status in
	`open_statuses`) now, or on `as_on`: then the status and the open amount after the last event dated up to
	`as_on` (the log rows are dated in order: `not_before`). Cancelled documents never count."""
	docs = frappe.get_all(
		doctype, filters={"facility": facility, "docstatus": 1}, fields=["name", "status", "open_amount"]
	)
	if not as_on:
		return {d.name: flt(d.open_amount) for d in docs if d.status in open_statuses}
	if not docs:
		return {}
	rows = frappe.get_all(
		LOG_DOCTYPE,
		filters={
			"parenttype": doctype,
			"parent": ["in", [d.name for d in docs]],
			"event_date": ["<=", getdate(as_on)],
		},
		fields=["parent", "to_status", "open_amount", "idx"],
		order_by="idx asc",
	)
	last = {}
	for row in rows:
		last[row.parent] = row
	return {name: flt(row.open_amount) for name, row in last.items() if row.to_status in open_statuses}


# ---------------------------------------------------------------------------
# Journal Entries
# ---------------------------------------------------------------------------


def _reference(doc) -> str:
	return doc.get("lg_no") or doc.get("lc_no") or doc.name


def post_margin(
	doc, event: str, date, amount: float, release: bool = False, rate=None, cycle: int = 1
) -> str:
	"""Cash margin: Dr Margin / Cr Bank (`release`: Dr Bank / Cr Margin). `rate`: the bank's rate of the event
	(default the issue rate the margin is carried at)."""
	carrying = flt(doc.exchange_rate) or 1.0
	bank_rate = flt(rate) or carrying
	bank_gl = accounting.bank_gl_account(doc.bank_account)
	company, currency = doc.company, doc.currency
	if release:
		rows = [
			accounting._row(company, currency, bank_gl, bank_rate, debit=amount),
			accounting._row(company, currency, doc.margin_account, carrying, credit=amount),
		]
		remarks = _("Cash margin released of {0} {1}").format(_(doc.doctype), doc.name)
	else:
		rows = [
			accounting._row(company, currency, doc.margin_account, carrying, debit=amount),
			accounting._row(company, currency, bank_gl, bank_rate, credit=amount),
		]
		remarks = _("Cash margin of {0} {1}").format(_(doc.doctype), doc.name)
	return posting.create_journal_entry(
		source_doctype=doc.doctype,
		source_name=doc.name,
		event=event,
		company=company,
		posting_date=date,
		accounts=rows,
		cheque_no=_reference(doc),
		cheque_date=date,
		clearance_date=date,
		remarks=remarks,
		voucher_type="Bank Entry",
		cycle=cycle,
		balance_exchange_difference=True,
	)


def post_bank_expense(
	doc, event: str, date, amount: float, account: str, remarks: str, rate=None, cycle: int = 1
) -> str:
	"""Commission / charges paid from the bank: Dr Expense (company currency) / Cr Bank."""
	bank_rate = flt(rate) or flt(doc.exchange_rate) or 1.0
	bank_gl = accounting.bank_gl_account(doc.bank_account)
	return posting.create_journal_entry(
		source_doctype=doc.doctype,
		source_name=doc.name,
		event=event,
		company=doc.company,
		posting_date=date,
		accounts=[
			accounting._expense_row(doc.company, account, amount, bank_rate, accounting._precision()),
			accounting._row(doc.company, doc.currency, bank_gl, bank_rate, credit=amount),
		],
		cheque_no=_reference(doc),
		cheque_date=date,
		clearance_date=date,
		remarks=remarks,
		voucher_type="Bank Entry",
		cycle=cycle,
		balance_exchange_difference=True,
	)


def next_cycle(doc, event: str) -> int:
	"""The cycle of the next voucher of a repeatable `event` (extensions, reductions, amendments ...)."""
	return (
		cint(
			frappe.db.count(
				"Journal Entry",
				{
					"treasury_source_doctype": doc.doctype,
					"treasury_source_name": doc.name,
					"treasury_event": event,
					"docstatus": 1,
				},
			)
		)
		+ 1
	)

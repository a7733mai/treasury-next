"""ERPNext documents Treasury documents are created from (spec 7c §3.1): invoices, orders, Journal Entries,
hrms Expense Claims / Employee Advances and the party masters.

Shared by the read-only mappers (`treasury.pdc.api`, `treasury.bank_transfers.api`,
`treasury.security_cheques.api`) and by the Cheque Batch source (`treasury.pdc.batch.allocate_to_source`):
which party and direction a source stands for, whether it can still be paid, how much of it is not yet
covered by open cheques or scheduled transfers, and the defaults (bank, cheque book, due date, accounting
details) a new document gets from it. Nothing here writes.
"""

from contextlib import contextmanager

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.utils import cint, flt, getdate, today

from treasury.accounting import dimensions, posting
from treasury.utils import hrms, references
from treasury.utils.party import get_party_name

INVOICE, ORDER, JOURNAL, HRMS, PARTY = "invoice", "order", "journal", "hrms", "party"
OUTGOING, INCOMING = "Outgoing", "Incoming"
# doctype -> (kind, direction, party type, party field); direction "Outgoing": we pay, "Incoming": we receive.
# A Journal Entry takes its direction and party from the party row chosen (`resolve`).
SOURCES = {
	"Purchase Invoice": (INVOICE, OUTGOING, "Supplier", "supplier"),
	"Sales Invoice": (INVOICE, INCOMING, "Customer", "customer"),
	"Purchase Order": (ORDER, OUTGOING, "Supplier", "supplier"),
	"Sales Order": (ORDER, INCOMING, "Customer", "customer"),
	"Journal Entry": (JOURNAL, None, None, None),
	"Expense Claim": (HRMS, OUTGOING, "Employee", "employee"),
	"Employee Advance": (HRMS, OUTGOING, "Employee", "employee"),
	"Supplier": (PARTY, OUTGOING, "Supplier", None),
	"Customer": (PARTY, INCOMING, "Customer", None),
	"Employee": (PARTY, OUTGOING, "Employee", None),
}
REFERENCE_SOURCES = tuple(d for d, s in SOURCES.items() if s[0] != PARTY)
PARTY_MASTERS = tuple(d for d, s in SOURCES.items() if s[0] == PARTY)
CLOSED_ORDER_STATUSES = ("Closed", "On Hold", "Completed")
DIRECTION_BY_ACCOUNT_TYPE = {"Payable": OUTGOING, "Receivable": INCOMING}
ITEM_PROJECT_DOCTYPES = ("Sales Invoice", "Purchase Invoice", "Sales Order", "Purchase Order")
# Treasury Company Settings: the default cheque bank of each direction.
DEFAULT_CHEQUE_BANK_FIELDS = {
	OUTGOING: "default_outgoing_cheque_bank_account",
	INCOMING: "default_incoming_cheque_bank_account",
}


class TreasuryPartyAccountError(frappe.ValidationError):
	"""The source is booked on another account than the party's default one (F12). The Treasury buttons
	(public/js/treasury_payment_buttons.js) recognise it by its name (`exc_type`) to point to ERPNext's own
	payment button."""


@contextmanager
def treasury_refusals():
	"""The refusals of a mapper carry the title "Treasury" (not Frappe's default "Message"), so the user sees
	at once who refused. Messages with a title of their own ("Not Permitted" ...) keep it."""
	start = len(frappe.local.message_log)
	try:
		yield
	except frappe.ValidationError:
		default = _("Message", context="Default title of the message dialog")
		for message in frappe.local.message_log[start:]:
			if not isinstance(message, dict) or not message.get("raise_exception"):
				continue
			if message.get("title") in (None, default):
				message["title"] = _("Treasury")
		raise


# ---------------------------------------------------------------------------
# Source and party
# ---------------------------------------------------------------------------


def get_source(source_doctype: str, source_name: str, target_label: str):
	"""The source document, after checking it is a supported type and that the user may read it."""
	if source_doctype not in SOURCES:
		frappe.throw(_("{0} cannot be created from {1}.").format(target_label, _(source_doctype or "-")))
	if SOURCES[source_doctype][0] == HRMS and not hrms.hrms_installed():
		frappe.throw(_("{0} needs the HRMS app.").format(_(source_doctype)))
	if not source_name or not frappe.db.exists(source_doctype, source_name):
		frappe.throw(_("{0} {1} does not exist").format(_(source_doctype), source_name or "-"))
	doc = frappe.get_doc(source_doctype, source_name)
	doc.check_permission("read")
	return doc


def resolve(source, party_type: str | None = None, party: str | None = None) -> frappe._dict:
	"""{kind, direction, party_type, party, company} of a source (a Journal Entry also gives the `accounts`
	the chosen party is booked on). A party master's company is its own (an Employee's), else None: the caller
	sets it."""
	kind, direction, source_party_type, party_field = SOURCES[source.doctype]
	if kind == PARTY:
		return frappe._dict(
			kind=kind,
			direction=direction,
			party_type=source_party_type,
			party=source.name,
			company=source.get("company") if source.meta.has_field("company") else None,
		)
	if kind == JOURNAL:
		row = _choose_journal_party(source, party_type, party)
		return frappe._dict(
			kind=kind,
			direction=row["direction"],
			party_type=row["party_type"],
			party=row["party"],
			company=source.company,
			account=row["account"],
			accounts=row["accounts"],
		)
	return frappe._dict(
		kind=kind,
		direction=direction,
		party_type=source_party_type,
		party=source.get(party_field),
		company=source.company,
	)


def default_company(company: str | None = None) -> str | None:
	"""The company of a document created from a party master: the one given, else the user's default company
	(ERPNext's `company` default, then Global Defaults). Not `get_user_default("Company")`: "Company" is a User
	Permission key, which reads only the scrubbed `company` default or a single permitted company."""
	return company or erpnext.get_default_company()


def _choose_journal_party(je, party_type: str | None, party: str | None) -> dict:
	parties = journal_parties(je)
	if party_type and party:
		row = next((p for p in parties if (p["party_type"], p["party"]) == (party_type, party)), None)
		if not row:
			frappe.throw(
				_("{0} {1} is not an open party of Journal Entry {2}.").format(
					_(party_type), frappe.bold(party), frappe.bold(je.name)
				)
			)
		return row
	if not parties:
		frappe.throw(_("Journal Entry {0} has no open party to settle.").format(frappe.bold(je.name)))
	if len(parties) > 1:
		frappe.throw(_("Journal Entry {0} has several open parties: choose one.").format(frappe.bold(je.name)))
	return parties[0]


def source_party(source_doctype: str, source_name: str, party_type: str | None, party: str | None) -> bool:
	"""Whether a (reference) source belongs to the party: its party field, or a party row of a Journal Entry."""
	if source_doctype not in REFERENCE_SOURCES or not (party_type and party):
		return False
	kind, _direction, source_party_type, party_field = SOURCES[source_doctype]
	if kind == JOURNAL:
		return bool(
			frappe.db.exists(
				"Journal Entry Account",
				{"parenttype": "Journal Entry", "parent": source_name, "party_type": party_type, "party": party},
			)
		)
	return party_type == source_party_type and frappe.db.get_value(
		source_doctype, source_name, party_field
	) == party


@frappe.whitelist()
def get_journal_parties(journal_entry: str) -> list[dict]:
	"""The parties a submitted Journal Entry still leaves open (read only): {party_type, party, party_name,
	account, accounts, outstanding, currency, direction, supported}. `accounts`: every account the party is
	booked on in the entry (`account`: the first); `supported`: that is only the party's default account, the
	only one Treasury settles on."""
	je = frappe.get_doc("Journal Entry", journal_entry)
	je.check_permission("read")
	return journal_parties(je)


def journal_parties(je) -> list[dict]:
	if je.docstatus != 1:
		return []
	precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency") or 2
	rows: dict[tuple[str, str], list[str]] = {}
	for row in je.get("accounts"):
		if row.party_type and row.party:
			rows.setdefault((row.party_type, row.party), [])
			if row.account not in rows[(row.party_type, row.party)]:
				rows[(row.party_type, row.party)].append(row.account)
	result = []
	for (party_type, party), accounts in rows.items():
		direction = DIRECTION_BY_ACCOUNT_TYPE.get(
			frappe.get_cached_value("Party Type", party_type, "account_type")
		)
		outstanding = flt(posting.get_ledger_outstanding(je.doctype, je.name, party_type, party), precision)
		if not direction or outstanding <= 0:
			continue
		default_account = posting.get_party_account(party_type, party, je.company)
		result.append(
			{
				"party_type": party_type,
				"party": party,
				"party_name": get_party_name(party_type, party),
				"account": accounts[0],
				"accounts": accounts,
				"outstanding": outstanding,
				"currency": get_account_currency(accounts[0]),
				"direction": direction,
				"supported": accounts == [default_account],
			}
		)
	return result


# ---------------------------------------------------------------------------
# Can it still be paid, and how much
# ---------------------------------------------------------------------------


def check_payable(source, info: frappe._dict):
	"""An invoice / order / Journal Entry / hrms document Treasury may still settle; a party master that is
	enabled (an Employee: Active)."""
	if info.kind == PARTY:
		check_active_party(source)
		return
	name = frappe.bold(source.name)
	nothing_outstanding = _("{0} {1} has nothing outstanding.").format(_(source.doctype), name)
	if source.docstatus != 1:
		frappe.throw(nothing_outstanding)
	if info.kind == INVOICE:
		if source.get("is_return"):
			frappe.throw(_("{0} {1} is a return; settle it from ERPNext.").format(_(source.doctype), name))
		if source.doctype == "Purchase Invoice" and source.get("on_hold"):
			frappe.throw(_("{0} {1} is on hold.").format(_(source.doctype), name))
	elif info.kind == ORDER:
		if source.status in CLOSED_ORDER_STATUSES:
			frappe.throw(_("{0} {1} is {2}.").format(_(source.doctype), name, _(source.status)))
		allowance = flt(frappe.db.get_single_value("Accounts Settings", "over_billing_allowance"))
		if flt(source.get("per_billed")) >= 100 + allowance:
			frappe.throw(
				_("{0} {1} is fully billed: pay its invoices instead.").format(_(source.doctype), name)
			)
	elif info.kind == HRMS:
		hrms.check_supported(source.doctype)
	_check_party_account(source, info)


def check_active_party(source):
	"""A disabled Customer / Supplier, or an Employee who is not Active, gets no new Treasury document."""
	if source.meta.has_field("disabled") and cint(source.get("disabled")):
		status = "Disabled"
	elif source.doctype == "Employee" and source.get("status") and source.status != "Active":
		status = source.status
	else:
		return
	frappe.throw(_("{0} {1} is {2}.").format(_(source.doctype), frappe.bold(source.name), _(status)))


def _check_party_account(source, info: frappe._dict):
	"""Treasury posts on the party's default account (F12): a document booked on another account cannot be
	settled by it. A Journal Entry party booked on several accounts (the default one among them) cannot
	either: its outstanding sums them all, the Payment Entry would settle only the default one."""
	if info.kind == JOURNAL:
		accounts = info.get("accounts") or [info.account]
	elif info.kind == HRMS:
		accounts = [source.get(hrms.PARTY_ACCOUNT_FIELD[source.doctype])]
	else:
		return
	default_account = posting.get_party_account(info.party_type, info.party, info.company)
	account = next((a for a in accounts if a != default_account), default_account)
	if account != default_account:
		frappe.throw(
			_(
				"{0} {1} uses account {2}, but Treasury settles {3} {4} on account {5}. Settle it from ERPNext."
			).format(
				_(source.doctype),
				frappe.bold(source.name),
				frappe.bold(account or "-"),
				_(info.party_type),
				frappe.bold(info.party),
				frappe.bold(default_account or "-"),
			),
			TreasuryPartyAccountError,
		)


def reserved_amount(reference_doctype: str, reference_name: str, exclude_batch: str | None = None) -> float:
	"""What open, not posted PDC Cheques and scheduled Bank Transfers already allocate to a document."""
	from treasury.pdc import batch

	cheques = batch.get_reserved_amounts(reference_doctype, [reference_name], exclude_batch=exclude_batch)
	transfers = references.scheduled_transfer_amounts(reference_doctype, [reference_name])
	return flt(cheques.get(reference_name, 0)) + flt(transfers.get(reference_name, 0))


def available_outstanding(
	source, info: frappe._dict, party_currency: str, precision: int, exclude_batch: str | None = None
) -> float:
	"""The outstanding of the source (party account currency) not yet covered by open cheques or scheduled
	transfers; throws when nothing is left."""
	available, refusal = uncovered_outstanding(source, info, party_currency, precision, exclude_batch)
	if refusal:
		frappe.throw(refusal)
	return available


def uncovered_outstanding(
	source, info: frappe._dict, party_currency: str, precision: int, exclude_batch: str | None = None
) -> tuple[float, str | None]:
	"""(what `available_outstanding` returns, None), or (0, the reason nothing is left) without throwing."""
	name = frappe.bold(source.name)
	outstanding = posting.get_reference_outstanding(
		source.doctype, source.name, info.party_type, info.party, party_currency, precision
	)
	if outstanding <= 0:
		return 0.0, _("{0} {1} has nothing outstanding.").format(_(source.doctype), name)
	available = flt(outstanding - reserved_amount(source.doctype, source.name, exclude_batch), precision)
	if available <= 0:
		return 0.0, _("{0} {1} is already covered by open cheques or scheduled transfers.").format(
			_(source.doctype), name
		)
	return available, None


def to_document_currency(
	outstanding: float,
	party_currency: str,
	doc_currency: str | None,
	company: str,
	exchange_rate,
	bank_account: str | None,
	source,
	precision: int,
) -> float:
	"""An outstanding in the party account currency, as an amount of a document in `doc_currency` (the
	currency of `bank_account`). Without a bank there is no document currency yet: the outstanding as it is."""
	if party_currency == doc_currency or not doc_currency or not bank_account:
		return outstanding
	if party_currency == erpnext.get_company_currency(company) and flt(exchange_rate):
		return flt(outstanding / flt(exchange_rate), precision)
	# e.g. a USD supplier and an EGP bank: the amount cannot be derived from the outstanding
	frappe.throw(
		_(
			"Bank Account {0} is in {1} and cannot settle the {2} outstanding of {3} {4}. Choose a Bank Account in {2}."
		).format(
			frappe.bold(bank_account),
			doc_currency,
			party_currency,
			_(source.doctype),
			frappe.bold(source.name),
		)
	)


# ---------------------------------------------------------------------------
# Defaults of a new document
# ---------------------------------------------------------------------------


def default_due_date(source):
	"""An invoice's due date, never before today (the issue date of the new document)."""
	due_date = source.get("due_date") if SOURCES.get(source.doctype, (None,))[0] == INVOICE else None
	return max(getdate(today()), getdate(due_date)) if due_date else getdate(today())


def copy_accounting(source, target):
	"""Cost center, project (an invoice / order: else the first item project) and accounting dimensions."""
	for fieldname in ("cost_center", "project"):
		if source.meta.has_field(fieldname) and source.get(fieldname) and target.meta.has_field(fieldname):
			target.set(fieldname, source.get(fieldname))
	if source.doctype in ITEM_PROJECT_DOCTYPES and not target.get("project") and target.meta.has_field("project"):
		project = next((row.project for row in source.get("items") or [] if row.get("project")), None)
		if project:
			target.project = project
	dimensions.copy_dimensions(source, target)


def bank_currency(bank_account: str | None) -> str | None:
	"""The currency of a company bank account's GL account."""
	if not bank_account:
		return None
	account = frappe.db.get_value("Bank Account", bank_account, "account")
	return get_account_currency(account) if account else None


def default_cheque_bank(company: str, direction: str, party_currency: str) -> str | None:
	"""The bank of a new cheque: the settings default of the direction when it can settle the party's
	currency, else the first PDC Accounts bank in the party currency, else (outgoing) the only mapped bank."""
	settings = posting.get_company_settings(company)
	company_currency = erpnext.get_company_currency(company)
	default = settings.get(DEFAULT_CHEQUE_BANK_FIELDS.get(direction) or "")
	# any bank settles a party in the company currency (at the bank's rate); else only one in its currency
	if default and party_currency in (bank_currency(default), company_currency):
		return default
	rows = sorted((row for row in settings.get("pdc_accounts") if row.bank_account), key=lambda r: r.idx)
	for row in rows:
		if bank_currency(row.bank_account) == party_currency:
			return row.bank_account
	if direction == OUTGOING and len(rows) == 1:
		return rows[0].bank_account
	return None


def default_cheque_book(company: str, bank_account: str | None, count: int = 1) -> str | None:
	"""The oldest active cheque book of the bank with `count` free leaves (setting `auto_select_cheque_book`)."""
	if not bank_account:
		return None
	settings = posting.get_company_settings(company)
	if not cint(settings.get("auto_select_cheque_book")):
		return None
	from treasury.pdc import batch

	count = max(cint(count), 1)
	for book in frappe.get_all(
		"Cheque Book",
		filters={"company": company, "bank_account": bank_account, "status": "Active"},
		order_by="creation asc",
		pluck="name",
	):
		if len(batch.preview_leaves(book, count)) >= count:
			return book
	return None


def check_create_permission(doctype: str, company: str | None):
	"""The user may create `doctype` documents of `company` (role permission, then User Permissions through a
	probe document of that company)."""
	frappe.has_permission(doctype, "create", throw=True)
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist.").format(frappe.bold(company or "-")))
	probe = frappe.new_doc(doctype)
	probe.company = company
	if not frappe.has_permission(doctype, "create", doc=probe):
		frappe.throw(
			_("You are not permitted to create {0} for company {1}.").format(_(doctype), frappe.bold(company)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)

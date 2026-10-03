"""Treasury posting service — the only place that creates accounting vouchers.

Every voucher is a standard Payment Entry or Journal Entry linked back to its Treasury source through the
read-only custom fields `treasury_source_doctype`, `treasury_source_name`, `treasury_event` and
`treasury_cycle`. All creators are idempotent on (source_doctype, source_name, event, cycle): `cycle`
starts at 1 and callers increment it when the same event legitimately happens again on one source
(e.g. a bounced cheque that is re-presented posts `party_posting` again in cycle 2).

Permissions: the calling controller / lifecycle / tool checks the user's permission (and role) on the
Treasury source document first. The public creators below then build, insert, submit and cancel the ERPNext
vouchers inside `treasury.utils.context.service_context`, under the REAL session user: the voucher, its GL /
Payment Ledger rows and its Version history carry that user, and ERPNext's role-based accounting guards
(frozen accounts, accounting periods, ...) are evaluated for that user. Treasury roles hold no accounting
permission (the app grants none), so each document the service builds sets `flags.ignore_permissions` itself
(`# SECURITY:` comments), and the one ERPNext helper that checks the session user's Account permission
(`get_party_account`) is replaced by `get_party_account` below. Scheduler jobs run as Administrator.
"""

from contextlib import contextmanager
from functools import wraps

import erpnext
import frappe
import frappe.share
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.party import get_party_gle_account, get_party_gle_currency
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt, getdate

from treasury.utils import hrms
from treasury.utils.context import service_context

VOUCHER_DOCTYPES = ("Payment Entry", "Journal Entry")
INTERNAL_TRANSFER = "Internal Transfer"
INVOICE_DOCTYPES = ("Sales Invoice", "Purchase Invoice")
ORDER_DOCTYPES = ("Sales Order", "Purchase Order")
REFERENCE_DOCTYPES = (*INVOICE_DOCTYPES, *ORDER_DOCTYPES, "Journal Entry")
# Largest company-currency rounding gap a charges deduction row absorbs (see `_absorb_rounding`).
ROUNDING_TOLERANCE = 0.05
# Party payment extension point (`get_party_rules`): hooks of other apps (e.g. the custody bridge) that decide
# how a party is paid; Treasury itself has no knowledge of any of them.
PARTY_PAYMENT_HOOK = "treasury_party_payment_hooks"
RULE_DEFAULTS = {
	"party_account": None,
	"fields": {},
	"allow_references": True,
	"allow_deductions": True,
	"undo_by_cancel": False,
	"allow_journal_entry": True,
}
RULE_ACTIONS = ("payment", "journal", "undo", "check")


def _in_service_context(fn):
	"""Run a posting service function inside `service_context` (see module docstring)."""

	@wraps(fn)
	def wrapper(*args, **kwargs):
		with service_context():
			return fn(*args, **kwargs)

	return wrapper


@contextmanager
def _reference_read_access(references, party_type=None, party=None):
	"""Let the session user read the invoices / orders a Payment Entry allocates against (and the party they
	belong to), for the duration of the block only.

	ERPNext's (and HRMS') Payment Entry validation calls `frappe.has_permission(<reference / party>, "read",
	<name>, throw=True)` for the SESSION user, with no bypass flag, and Treasury roles hold no permission on
	these doctypes (the app grants none). Instead of running as Administrator or granting the roles read
	access to every invoice, the user is shared exactly the allocated documents and the party (read only)
	and the shares this block created are removed again on exit. Only called after the Treasury source-document permission + role check.
	"""
	user = frappe.session.user
	created: list[str] = []
	if user != "Administrator":
		seen = set()
		keys = [(ref.reference_doctype, ref.reference_name) for ref in references or []]
		if references and party_type and party:
			keys.append((party_type, party))
		for key in keys:
			if key in seen or not all(key):
				continue
			seen.add(key)
			if frappe.has_permission(key[0], "read", key[1], user=user):
				continue
			# SECURITY: after the source-document permission + role check; read-only, removed in `finally`
			share = frappe.share.add_docshare(
				key[0], key[1], user=user, read=1, flags={"ignore_share_permission": True}
			)
			created.append(share.name)
	try:
		yield
	finally:
		for name in created:
			# SECURITY: removes only the shares created above
			frappe.delete_doc(
				"DocShare", name, ignore_permissions=True, flags={"ignore_share_permission": True}, force=True
			)


# ---------------------------------------------------------------------------
# Settings lookups
# ---------------------------------------------------------------------------


def get_company_settings(company: str) -> Document:
	"""Return the Treasury Company Settings of `company`."""
	if not company or not frappe.db.exists("Treasury Company Settings", company):
		frappe.throw(
			_("Treasury Company Settings are missing for company {0}.").format(frappe.bold(company)),
			title=_("Missing Settings"),
		)
	return frappe.get_doc("Treasury Company Settings", company)


def get_account_mapping(company: str, bank_account: str) -> frappe._dict:
	"""Return the PDC Account Mapping row of `bank_account` in the settings of `company`."""
	settings = get_company_settings(company)
	for row in settings.pdc_accounts:
		if row.bank_account == bank_account:
			return frappe._dict(row.as_dict())

	frappe.throw(
		_("No PDC account mapping found for Bank Account {0} in Treasury Company Settings {1}.").format(
			frappe.bold(bank_account), frappe.bold(company)
		),
		title=_("Missing Settings"),
	)


def get_party_account(party_type: str, party: str, company: str) -> str | None:
	"""The party account of `party` in `company`: ERPNext's `get_party_account` resolution (party row, group
	row, company default, existing GL currency) without its final read-permission check on the account.

	ERPNext's whitelisted function ends with `frappe.has_permission("Account", "read", account)` for the
	session user, a data-leak guard for client calls. Treasury roles hold no Account permission (the app
	grants none, and the vouchers are built as the real user, not as Administrator), and the account is
	only used to build a voucher after the caller's source-document permission + role check. The resolution
	reads with `frappe.db` / cached values only; a test pins it to ERPNext's own result for Administrator.
	"""
	if not party_type:
		frappe.throw(_("Party Type is mandatory"))
	if not company:
		frappe.throw(_("Please select a Company"))

	def company_default(fieldname: str):
		return frappe.get_cached_value("Company", company, fieldname)

	receivable_or_payable = (
		"default_receivable_account" if party_type == "Customer" else "default_payable_account"
	)
	if not party and party_type in ("Customer", "Supplier"):
		account = company_default(receivable_or_payable)
	else:
		account = frappe.db.get_value(
			"Party Account", {"parenttype": party_type, "parent": party, "company": company}, "account"
		)
		if not account and party_type in ("Customer", "Supplier"):
			group_doctype = "Customer Group" if party_type == "Customer" else "Supplier Group"
			group = frappe.get_cached_value(party_type, party, frappe.scrub(group_doctype))
			account = frappe.db.get_value(
				"Party Account", {"parenttype": group_doctype, "parent": group, "company": company}, "account"
			)
		if not account and party_type in ("Customer", "Supplier"):
			account = company_default(receivable_or_payable)

		existing_currency = get_party_gle_currency(party_type, party, company)
		if existing_currency:
			account_currency = (
				frappe.get_cached_value("Account", account, "account_currency") if account else None
			)
			if not account or account_currency != existing_currency:
				account = get_party_gle_account(party_type, party, company)

		if not account:
			account_type = frappe.get_cached_value("Party Type", party_type, "account_type")
			account = company_default("default_" + account_type.lower() + "_account")
	return account


# ---------------------------------------------------------------------------
# Party payment rules (extension point)
# ---------------------------------------------------------------------------


def _party_rule_methods() -> list:
	"""The `treasury_party_payment_hooks` of every installed app, in hook order."""
	return [frappe.get_attr(method) for method in frappe.get_hooks(PARTY_PAYMENT_HOOK)]


def get_party_rules(
	*,
	source_doctype: str,
	source_name: str,
	company: str,
	party_type: str | None,
	party: str | None,
	action: str,
	payment_type: str | None = None,
) -> frappe._dict:
	"""Merged answers of the `treasury_party_payment_hooks` of every installed app for a party.

	`action`: "payment" (a party Payment Entry is about to be built), "journal" (a Treasury Journal Entry row),
	"undo" (a voucher must be undone), "check" (what a caller must do, e.g. split charges).
	Each hook: fn(ctx: frappe._dict) -> dict | None, ctx = the keyword arguments. Merge: party_account — the
	single non-empty value (two different → throw); fields — merged, two different values of one field → throw
	(only custom fields of other apps are applied, see `_apply_rule_fields`); allow_* — AND; undo_by_cancel —
	OR. No party_type → RULE_DEFAULTS without calling hooks."""
	if action not in RULE_ACTIONS:
		frappe.throw(_("Invalid party rule action {0}.").format(action))
	rules = frappe._dict(RULE_DEFAULTS, fields={})
	if not party_type:
		return rules
	ctx = frappe._dict(
		source_doctype=source_doctype,
		source_name=source_name,
		company=company,
		party_type=party_type,
		party=party,
		action=action,
		payment_type=payment_type,
	)
	for method in _party_rule_methods():
		answer = method(frappe._dict(ctx))
		if not answer:
			continue
		account = answer.get("party_account")
		if account:
			if rules.party_account and rules.party_account != account:
				frappe.throw(
					_("Conflicting party accounts for {0} {1}: {2} and {3}.").format(
						_(party_type),
						frappe.bold(party),
						frappe.bold(rules.party_account),
						frappe.bold(account),
					)
				)
			rules.party_account = account
		for fieldname, value in (answer.get("fields") or {}).items():
			if fieldname in rules.fields and rules.fields[fieldname] != value:
				frappe.throw(
					_("Conflicting values for field {0} of the payment to {1} {2}: {3} and {4}.").format(
						frappe.bold(fieldname),
						_(party_type),
						frappe.bold(party),
						frappe.bold(rules.fields[fieldname]),
						frappe.bold(value),
					)
				)
			rules.fields[fieldname] = value
		for key in ("allow_references", "allow_deductions", "allow_journal_entry"):
			if key in answer:
				rules[key] = rules[key] and bool(answer[key])
		rules.undo_by_cancel = rules.undo_by_cancel or bool(answer.get("undo_by_cancel"))
	return rules


def must_cancel(doctype: str, name: str) -> bool:
	"""A Treasury voucher whose party may only be undone by cancelling it (rules.undo_by_cancel): the PE's own
	party, or any party row of a JE (action "undo")."""
	if doctype not in VOUCHER_DOCTYPES:
		return False
	fields = ["company", "treasury_source_doctype", "treasury_source_name"]
	if doctype == "Payment Entry":
		fields += ["party_type", "party"]
	voucher = frappe.db.get_value(doctype, name, fields, as_dict=True)
	if not voucher:
		return False
	if doctype == "Payment Entry":
		parties = [(voucher.party_type, voucher.party)] if voucher.party_type else []
	else:
		parties = {
			(row.party_type, row.party)
			for row in frappe.get_all(
				"Journal Entry Account",
				filters={"parenttype": "Journal Entry", "parent": name, "party_type": ["is", "set"]},
				fields=["party_type", "party"],
			)
		}
	return any(
		get_party_rules(
			source_doctype=voucher.treasury_source_doctype,
			source_name=voucher.treasury_source_name,
			company=voucher.company,
			party_type=party_type,
			party=party,
			action="undo",
		).undo_by_cancel
		for party_type, party in parties
	)


def _check_cancel_only(doctype: str, name: str):
	"""Defensive guard of the reversal creators: such a voucher is undone by cancelling it (`must_cancel`)."""
	if must_cancel(doctype, name):
		frappe.throw(
			_("{0} {1} can only be undone by cancelling it.").format(_(doctype), frappe.bold(name)),
			title=_("Reversal Not Possible"),
		)


def rule_field_refusal(meta, fieldname: str) -> str | None:
	"""Why the party rules may not set `fieldname` of a Payment Entry, None when they may. Only a custom field
	of another app is accepted: never a standard field (amounts, accounts, party, references ...), a Treasury
	field or an Accounting Dimension, which the posting service owns."""
	df = meta.get_field(fieldname)
	# `is None`, not falsiness: a standard field whose definition has no custom flag (an empty-looking dict) is
	# an existing field and gets the refusal below, not "no field"
	if df is None:
		return _("Payment Entry has no field {0}.").format(frappe.bold(fieldname))
	if (
		not df.get("is_custom_field")
		or fieldname.startswith("treasury_")
		or fieldname in _all_dimension_fieldnames()
	):
		return _("Party rules cannot set field {0} of Payment Entry: the posting service sets it.").format(
			frappe.bold(fieldname)
		)
	return None


def _all_dimension_fieldnames() -> list[str]:
	"""The fields of every Accounting Dimension, disabled ones included (their custom fields stay)."""
	return frappe.get_all("Accounting Dimension", pluck="fieldname")


def _apply_rule_fields(pe: Document, fields: dict | None):
	"""Field values the party rules require on the Payment Entry (e.g. a link another app needs): custom fields
	of other apps only (`rule_field_refusal`)."""
	for fieldname, value in (fields or {}).items():
		if reason := rule_field_refusal(pe.meta, fieldname):
			frappe.throw(reason)
		pe.set(fieldname, value)


def get_cheque_exchange_rate(currency: str, company: str, posting_date) -> float:
	"""Rate converting `currency` to the company currency on `posting_date` (1 for the company currency)."""
	company_currency = erpnext.get_company_currency(company)
	if not currency or currency == company_currency:
		return 1.0
	rate = flt(get_exchange_rate(currency, company_currency, getdate(posting_date)))
	if not rate:
		frappe.throw(_("Exchange rate from {0} to {1} is required.").format(currency, company_currency))
	return rate


# ---------------------------------------------------------------------------
# Linked vouchers
# ---------------------------------------------------------------------------


def get_linked_vouchers(source_doctype: str, source_name: str) -> list[dict]:
	"""All vouchers (any docstatus) created for a Treasury source, oldest first."""
	vouchers = []
	for doctype in VOUCHER_DOCTYPES:
		fields = ["name", "docstatus", "posting_date", "treasury_event", "treasury_cycle", "creation"]
		if doctype == "Payment Entry":
			fields.append("payment_type")
		rows = frappe.get_all(
			doctype,
			filters={"treasury_source_doctype": source_doctype, "treasury_source_name": source_name},
			fields=fields,
			order_by="creation asc",
		)
		for row in rows:
			row["doctype"] = doctype
			vouchers.append(row)
	vouchers.sort(key=lambda d: d["creation"])
	return vouchers


def get_voucher_rows(source_doctype: str, source_names: list[str]) -> list[dict]:
	"""All vouchers (any docstatus) of several sources of one doctype, oldest first, with the details a
	voucher summary shows; `amount` is the paid amount of a Payment Entry (in `currency`, its paid-from account
	currency), the total debit of a Journal Entry (company currency, `currency` None)."""
	if not source_names:
		return []
	common = [
		"name",
		"docstatus",
		"company",
		"posting_date",
		"treasury_event",
		"treasury_cycle",
		"treasury_source_name",
		"clearance_date",
		"creation",
	]
	specific = {
		"Payment Entry": [
			"reference_no",
			"payment_type",
			"mode_of_payment",
			"paid_amount as amount",
			"paid_from_account_currency as currency",
		],
		"Journal Entry": ["cheque_no as reference_no", "total_debit as amount"],
	}
	rows = []
	for doctype in VOUCHER_DOCTYPES:
		for row in frappe.get_all(
			doctype,
			filters={"treasury_source_doctype": source_doctype, "treasury_source_name": ["in", source_names]},
			fields=common + specific[doctype],
			order_by="creation asc",
		):
			row.doctype = doctype
			row.setdefault("payment_type", None)
			row.setdefault("mode_of_payment", None)
			row.setdefault("currency", None)
			rows.append(row)
	rows.sort(key=lambda d: d.creation)
	return rows


def find_voucher(
	doctype: str, source_doctype: str, source_name: str, event: str, cycle: int = 1
) -> str | None:
	"""Return the draft/submitted voucher already created for (source, event, cycle), if any."""
	names = frappe.get_all(
		doctype,
		filters={
			"treasury_source_doctype": source_doctype,
			"treasury_source_name": source_name,
			"treasury_event": event,
			"treasury_cycle": _validate_cycle(cycle),
			"docstatus": ["<", 2],
		},
		order_by="creation desc",
		limit=1,
		pluck="name",
	)
	return names[0] if names else None


def _validate_cycle(cycle: int) -> int:
	if cint(cycle) < 1:
		frappe.throw(_("Treasury cycle must be a positive whole number."))
	return cint(cycle)


def _lock_source(source_doctype: str, source_name: str):
	"""Lock the source row so concurrent calls (e.g. a double click) cannot both create a voucher."""
	if not frappe.db.get_value(source_doctype, source_name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(source_doctype), source_name))


def _return_existing(doctype: str, name: str, submit: bool) -> str:
	doc = frappe.get_doc(doctype, name)
	if submit and doc.docstatus == 0:
		doc.flags.ignore_permissions = True  # SECURITY: after the source-document permission + role check
		doc.submit()
	return name


def _set_treasury_fields(doc: Document, source_doctype: str, source_name: str, event: str, cycle: int):
	doc.treasury_source_doctype = source_doctype
	doc.treasury_source_name = source_name
	doc.treasury_event = event
	doc.treasury_cycle = cycle


# ---------------------------------------------------------------------------
# Payment Entry
# ---------------------------------------------------------------------------


@_in_service_context
def create_payment_entry(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	payment_type: str,
	company: str,
	posting_date,
	party_type: str,
	party: str,
	amount: float,
	bank_account_head: str,
	reference_no: str,
	reference_date,
	currency: str | None = None,
	exchange_rate: float | None = None,
	references: list[dict] | None = None,
	remarks: str | None = None,
	cycle: int = 1,
	submit: bool = True,
	clearance_date=None,
	details: dict | None = None,
	charges: float = 0,
	charges_account: str | None = None,
) -> str:
	"""Create (or return the existing) Payment Entry whose bank side is `bank_account_head`.

	`amount` is expressed in the currency of `bank_account_head` (the cheque currency) and
	`exchange_rate` converts that currency to the company currency. `clearance_date` (the bank already
	moved the money) requires `submit=True`. `details`: see `_apply_details`.

	`charges` (bank currency, e.g. a transfer fee) on `charges_account` (company currency): `amount` stays the
	principal settled with the party; the bank moves `amount + charges` (Pay) or `amount - charges`
	(Receive), and the charges are a normal deduction row (see `_charges_row`).

	The party rules of the installed apps (`get_party_rules`, action "payment") may give the party account and
	Payment Entry fields, and refuse references or charges (e.g. a payment to a custodian).
	"""
	if clearance_date and not submit:
		frappe.throw(_("Clearance Date can only be set on a submitted Payment Entry."))

	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		_return_existing("Payment Entry", existing, submit)
		if clearance_date and not frappe.db.get_value("Payment Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date, "Payment Entry")
		return existing

	if payment_type not in ("Pay", "Receive"):
		frappe.throw(_("Payment Type must be Pay or Receive."))

	precision = frappe.get_precision("Payment Entry", "paid_amount")
	rules = get_party_rules(
		source_doctype=source_doctype,
		source_name=source_name,
		company=company,
		party_type=party_type,
		party=party,
		action="payment",
		payment_type=payment_type,
	)
	if references and not rules.allow_references:
		frappe.throw(
			_("A payment to {0} {1} cannot be allocated to references.").format(
				_(party_type), frappe.bold(party)
			)
		)
	if flt(charges, precision) and not rules.allow_deductions:
		frappe.throw(
			_("The charges of a payment to {0} {1} must be posted on their own voucher.").format(
				_(party_type), frappe.bold(party)
			)
		)
	charges = _validate_charges(company, amount, charges, charges_account, payment_type, precision)
	principal = flt(amount, precision)
	bank_amount = principal + charges if payment_type == "Pay" else principal - charges
	amounts = _get_payment_amounts(
		company=company,
		posting_date=posting_date,
		party_type=party_type,
		party=party,
		amount=bank_amount,
		bank_account_head=bank_account_head,
		currency=currency,
		exchange_rate=exchange_rate,
		party_account=rules.party_account,
	)
	# the party is settled with the principal only (without charges: the same as `amounts.party_amount`)
	if amounts.party_currency == amounts.bank_currency:
		party_cap = principal
	else:
		party_cap = flt(principal * amounts.bank_rate, amounts.precision)

	pe = _new_payment_entry(
		payment_type=payment_type,
		company=company,
		posting_date=posting_date,
		party_type=party_type,
		party=party,
		reference_no=reference_no,
		reference_date=reference_date,
		bank_account_head=bank_account_head,
		amounts=amounts,
		remarks=remarks,
	)
	for ref in _allocate_references(
		references, party_type, party, amounts.party_currency, party_cap, amounts.precision
	):
		pe.append("references", ref)

	if charges:
		pe.append("deductions", _charges_row(company, charges_account, charges * amounts.bank_rate, details))
	_apply_details(pe, details)
	_apply_rule_fields(pe, rules.fields)
	_set_treasury_fields(pe, source_doctype, source_name, event, cycle)
	pe.flags.ignore_permissions = True  # SECURITY: after the source-document permission + role check
	with _reference_read_access(pe.get("references"), party_type, party):
		pe.insert()
		if charges:
			_absorb_rounding(pe, charges_account, party_cap, amounts.precision)
		if submit:
			pe.submit()
			if clearance_date:
				_set_clearance_date(pe.name, clearance_date, "Payment Entry")
	return pe.name


@_in_service_context
def create_bank_charge_payment(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	payment_type: str,
	company: str,
	posting_date,
	party_type: str,
	party: str,
	amount: float,
	bank_account_head: str,
	expense_account: str,
	reference_no: str,
	reference_date,
	currency: str | None = None,
	exchange_rate: float | None = None,
	remarks: str | None = None,
	details: dict | None = None,
	cycle: int = 1,
	clearance_date=None,
) -> str:
	"""Create (or return the existing) Payment Entry of a bank charge (Pay) or its refund (Receive) whose whole
	`amount` (bank currency) is a deduction on `expense_account` (company currency). Spec 7d D1-D3
	(payment_entry.py:995-1001, 1103-1213, 1350-1474, 1597-1632, 1699-1721):
	- Pay: paid = X (bank), received = X (party side, converted to the party account currency) and one unflagged
	  deduction of X x rate: unallocated = base received - deductions = 0, difference = 0, base paid == base
	  received, so ERPNext adds no flagged exchange row;
	- Receive: paid = X (party side), received = X (bank) and a negative deduction of X x rate, which
	  `toggle_debit_credit_if_negative` books as a credit on the expense account;
	- no references and no unallocated amount: the party gets no GL row and no Payment Ledger Entry, so the
	  supplier's balance is untouched. The GL is Cr Bank / Dr Expense (Receive: the reverse).
	"""
	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		_return_existing("Payment Entry", existing, True)
		if clearance_date and not frappe.db.get_value("Payment Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date, "Payment Entry")
		return existing

	if payment_type not in ("Pay", "Receive"):
		frappe.throw(_("Payment Type must be Pay or Receive."))
	_company_currency_account(expense_account, company)
	amounts = _get_payment_amounts(
		company=company,
		posting_date=posting_date,
		party_type=party_type,
		party=party,
		amount=amount,
		bank_account_head=bank_account_head,
		currency=currency,
		exchange_rate=exchange_rate,
	)

	pe = _new_payment_entry(
		payment_type=payment_type,
		company=company,
		posting_date=posting_date,
		party_type=party_type,
		party=party,
		reference_no=reference_no,
		reference_date=reference_date,
		bank_account_head=bank_account_head,
		amounts=amounts,
		remarks=remarks,
	)
	sign = 1 if payment_type == "Pay" else -1
	pe.append(
		"deductions",
		_charges_row(company, expense_account, sign * amounts.bank_amount * amounts.bank_rate, details),
	)
	_apply_details(pe, details)
	_set_treasury_fields(pe, source_doctype, source_name, event, cycle)
	pe.flags.ignore_permissions = True  # SECURITY: after the source-document permission + role check
	pe.insert()
	if flt(pe.difference_amount) or flt(pe.unallocated_amount):
		frappe.throw(
			_("The Payment Entry {0} is not balanced (difference {1}).").format(
				pe.name, flt(pe.difference_amount) or flt(pe.unallocated_amount)
			)
		)
	pe.submit()
	if clearance_date:
		_set_clearance_date(pe.name, clearance_date, "Payment Entry")
	return pe.name


@_in_service_context
def create_deductions_payment(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	payment_type: str,
	company: str,
	posting_date,
	party_type: str,
	party: str,
	bank_account_head: str,
	deductions: list[dict],
	reference_no: str,
	reference_date,
	remarks: str | None = None,
	details: dict | None = None,
	cycle: int = 1,
	clearance_date=None,
) -> str:
	"""Create (or return the existing) Pay / Receive Payment Entry whose whole amount is in deduction rows
	(spec 7g §3.2): e.g. a facility repayment (Pay: principal on the liability account, interest on the
	expense or accrued interest account) or a drawdown (Receive: the liability as a negative row, the upfront
	fees as a positive one). `deductions`: [{account, amount, cost_center?}], company currency.

	Why it balances (payment_entry.py:1103-1142, 1192-1213, 1699-1721; spec 7g F1-F4):
	- F1: several unflagged deductions summing to the paid amount leave unallocated = base received -
	  deductions - allocated = 0 and difference = base paid - party (0) - deductions = 0;
	- F2: deduction rows are booked in the company currency: every account (the bank side too) must be in it,
	  a foreign currency facility keeps its Journal Entries;
	- F3: no references and nothing unallocated: no party GL row and no Payment Ledger Entry, the party's
	  balance is untouched;
	- F4: Receive takes rows signed by the caller; a negative row is booked as a credit
	  (`toggle_debit_credit_if_negative`), so Receive [-Liability A, +Fees F] with paid = received = A - F
	  books Dr Bank A - F, Dr Fees F, Cr Liability A.
	"""
	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		_return_existing("Payment Entry", existing, True)
		if clearance_date and not frappe.db.get_value("Payment Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date, "Payment Entry")
		return existing

	if payment_type not in ("Pay", "Receive"):
		frappe.throw(_("Payment Type must be Pay or Receive."))
	company_currency = erpnext.get_company_currency(company)
	precision = frappe.get_precision("Payment Entry Deduction", "amount") or 2
	for account in (bank_account_head, *(d.get("account") for d in deductions or [])):
		details_ = _transfer_account(account, company) if account else None
		if not details_ or details_.account_currency != company_currency:
			frappe.throw(
				_(
					"Payment Entry deductions must be in the company currency {0}; use a Journal Entry."
				).format(company_currency)
			)
	rows = [d for d in deductions or [] if flt(d.get("amount"), precision)]
	amount = flt(sum(flt(d.get("amount"), precision) for d in rows), precision)
	if (payment_type == "Pay" and amount <= 0) or (payment_type == "Receive" and -amount <= 0):
		frappe.throw(_("Amount must be greater than zero."))

	amounts = _get_payment_amounts(
		company=company,
		posting_date=posting_date,
		party_type=party_type,
		party=party,
		amount=abs(amount),
		bank_account_head=bank_account_head,
		currency=company_currency,
		exchange_rate=1.0,
	)
	pe = _new_payment_entry(
		payment_type=payment_type,
		company=company,
		posting_date=posting_date,
		party_type=party_type,
		party=party,
		reference_no=reference_no,
		reference_date=reference_date,
		bank_account_head=bank_account_head,
		amounts=amounts,
		remarks=remarks,
	)
	details = frappe._dict(details or {})
	for row in rows:
		pe.append(
			"deductions",
			_charges_row(
				company,
				row["account"],
				row["amount"],
				{**details, "cost_center": row.get("cost_center") or details.cost_center},
			),
		)
	_apply_details(pe, details)
	_set_treasury_fields(pe, source_doctype, source_name, event, cycle)
	pe.flags.ignore_permissions = True  # SECURITY: after the source-document permission + role check
	pe.insert()
	if flt(pe.difference_amount) or flt(pe.unallocated_amount):
		frappe.throw(
			_("The Payment Entry {0} is not balanced (difference {1}).").format(
				pe.name, flt(pe.difference_amount) or flt(pe.unallocated_amount)
			)
		)
	pe.submit()
	if clearance_date:
		_set_clearance_date(pe.name, clearance_date, "Payment Entry")
	return pe.name


def _new_payment_entry(
	*,
	payment_type: str,
	company: str,
	posting_date,
	party_type: str,
	party: str,
	reference_no: str,
	reference_date,
	bank_account_head: str,
	amounts: frappe._dict,
	remarks: str | None,
) -> Document:
	"""An unsaved Pay / Receive Payment Entry between `bank_account_head` and the party account of `amounts`
	(`_get_payment_amounts`): Pay goes from the bank to the party, Receive from the party to the bank."""
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = payment_type
	pe.company = company
	pe.posting_date = getdate(posting_date)
	pe.party_type = party_type
	pe.party = party
	pe.reference_no = reference_no
	pe.reference_date = getdate(reference_date)

	bank_side = {
		"account": bank_account_head,
		"currency": amounts.bank_currency,
		"type": frappe.get_cached_value("Account", bank_account_head, "account_type"),
		"amount": amounts.bank_amount,
		"rate": amounts.bank_rate,
	}
	party_side = {
		"account": amounts.party_account,
		"currency": amounts.party_currency,
		"type": frappe.get_cached_value("Account", amounts.party_account, "account_type"),
		"amount": amounts.party_amount,
		"rate": amounts.party_rate,
	}
	source, target = (bank_side, party_side) if payment_type == "Pay" else (party_side, bank_side)
	pe.paid_from = source["account"]
	pe.paid_from_account_currency = source["currency"]
	pe.paid_from_account_type = source["type"]
	pe.paid_amount = source["amount"]
	pe.source_exchange_rate = source["rate"]
	pe.paid_to = target["account"]
	pe.paid_to_account_currency = target["currency"]
	pe.paid_to_account_type = target["type"]
	pe.received_amount = target["amount"]
	pe.target_exchange_rate = target["rate"]

	if remarks:
		pe.custom_remarks = 1
		pe.remarks = remarks
	return pe


def _validate_charges(
	company: str, amount: float, charges, charges_account: str | None, payment_type: str, precision: int
) -> float:
	charges = flt(charges, precision)
	if charges < 0:
		frappe.throw(_("Charges cannot be negative."))
	if not charges:
		return 0.0
	if not charges_account:
		frappe.throw(_("An account for the charges is required."))
	_company_currency_account(charges_account, company)
	if payment_type == "Receive" and charges >= flt(amount, precision):
		frappe.throw(_("Charges must be less than the amount."))
	return charges


def _charges_row(company: str, account: str, amount: float, details: dict | None) -> dict:
	"""The charges of a Pay / Receive Payment Entry as a normal (unflagged) deduction row.

	Spec 7b G1 / G2 (payment_entry.py:995-1001, 1103-1213): the bank side carries the charges (Pay: paid =
	principal + charges; Receive: paid = received = principal - charges), so base_paid == base_received by
	construction and ERPNext adds no flagged exchange row; `set_unallocated_amount` counts the unflagged row,
	which balances the party side to the principal and leaves difference_amount at zero.
	"""
	details = frappe._dict(details or {})
	row = {
		"account": account,
		"cost_center": details.cost_center or erpnext.get_default_cost_center(company),
		"amount": flt(amount, frappe.get_precision("Payment Entry Deduction", "amount")),
		"is_exchange_gain_loss": 0,
	}
	row.update(_dimension_values(frappe.get_meta("Payment Entry Deduction"), details.dimensions))
	return row


def _absorb_rounding(pe: Document, charges_account: str, party_cap: float, party_precision: int):
	"""A company-currency rounding gap of a foreign-currency entry (the party side, the bank side and the
	charges are rounded separately) goes into the charges row, up to ROUNDING_TOLERANCE.

	Two forms of the gap (payment_entry.py set_unallocated_amount / set_difference_amount):
	- the party side is short (allocated + unallocated < bank side - charges): a difference amount;
	- the party side is over: the unflagged charges row feeds `set_unallocated_amount`, and the gap would stay
	  as a tiny unallocated advance. It is rounding only when the principal (`party_cap`) is fully allocated;
	  Pay: unallocated = base received - charges - allocated (the row grows by it); Receive: unallocated =
	  base paid + charges - allocated (the row shrinks by it)."""
	precision = frappe.get_precision("Payment Entry Deduction", "amount")
	row = next(
		(d for d in pe.deductions if d.account == charges_account and not d.is_exchange_gain_loss), None
	)
	allocated = flt(sum(flt(r.allocated_amount) for r in pe.references), party_precision)
	if row and pe.references and allocated == flt(party_cap, party_precision):
		rate = pe.source_exchange_rate if pe.payment_type == "Receive" else pe.target_exchange_rate
		unallocated = flt(flt(pe.unallocated_amount) * flt(rate), precision)
		sign = -1 if pe.payment_type == "Receive" else 1
		new_amount = flt(flt(row.amount) + sign * unallocated, precision)
		if unallocated and abs(unallocated) <= ROUNDING_TOLERANCE and new_amount > 0:
			row.amount = new_amount
			pe.save()
	difference = flt(pe.difference_amount, precision)
	if difference and row and abs(difference) <= ROUNDING_TOLERANCE:
		row.amount = flt(flt(row.amount) + difference, precision)
		pe.save()
	if flt(pe.difference_amount, precision):
		frappe.throw(
			_("The Payment Entry {0} is not balanced (difference {1}).").format(pe.name, pe.difference_amount)
		)


def _apply_details(pe: Document, details: dict | None):
	"""Accounting details of a Payment Entry, from its Treasury source.

	Keys: `naming_series` (must already be a Payment Entry series), `mode_of_payment` (ignored when missing or
	disabled: the settings validate it), `cost_center`, `project`, `bank_account` (the company Bank Account),
	`dimensions` ({fieldname: value}), and for Pay / Receive only `party_bank_account` and `contact_person`.
	"""
	if not details:
		return
	details = frappe._dict(details)
	series = (details.naming_series or "").strip()
	if series:
		if series not in frappe.get_meta("Payment Entry").get_naming_series_options():
			frappe.throw(
				_(
					"Series {0} is not a Payment Entry series. A System Manager must add it in Treasury Company Settings (Add Series to Payment Entry)."
				).format(frappe.bold(series)),
				title=_("Missing Settings"),
			)
		pe.naming_series = series
	if details.mode_of_payment and frappe.db.get_value("Mode of Payment", details.mode_of_payment, "enabled"):
		pe.mode_of_payment = details.mode_of_payment
	for fieldname in ("cost_center", "project", "bank_account"):
		if details.get(fieldname):
			pe.set(fieldname, details[fieldname])
	for fieldname, value in _dimension_values(pe.meta, details.dimensions).items():
		pe.set(fieldname, value)
	if pe.payment_type != INTERNAL_TRANSFER:
		for fieldname in ("party_bank_account", "contact_person"):
			if details.get(fieldname):
				pe.set(fieldname, details[fieldname])


def _dimension_values(meta, dimensions: dict | None) -> dict:
	"""The set values of `dimensions` whose fieldname is an enabled Accounting Dimension field of `meta`."""
	if not dimensions:
		return {}
	enabled = set(get_accounting_dimensions())
	return {f: v for f, v in dimensions.items() if v and f in enabled and meta.has_field(f)}


@_in_service_context
def create_internal_transfer(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	company: str,
	posting_date,
	paid_from: str,
	paid_to: str,
	paid_amount: float,
	reference_no: str,
	reference_date,
	received_amount: float | None = None,
	exchange_rate: float | None = None,
	deduction_account: str | None = None,
	remarks: str | None = None,
	details: dict | None = None,
	cycle: int = 1,
	submit: bool = True,
	clearance_date=None,
	charges: float | None = None,
) -> str:
	"""Create (or return the existing) Internal Transfer Payment Entry `paid_from` → `paid_to`.

	Same currency: `paid_amount` / `received_amount` are in that currency and `exchange_rate` converts it to
	the company currency (one rate for both sides: ERPNext forces the target rate to the source rate when the
	currencies are equal). A `received_amount` below `paid_amount` books the difference (e.g. bank charges) on
	`deduction_account`; `charges` is ignored (it is paid - received).

	Different currencies (spec 7b G4): `received_amount` (target currency) is required and `charges` (source
	currency, included in `paid_amount`) go to `deduction_account`. The rates are derived from the bank's own
	conversion of the principal (paid - charges) into the received amount, so the entry books no exchange
	difference: the flagged deduction row holds only the charges (± 0.01 rounding). Later rate movements are
	left to Exchange Rate Revaluation. `clearance_date` requires `submit=True`.
	"""
	if clearance_date and not submit:
		frappe.throw(_("Clearance Date can only be set on a submitted Payment Entry."))

	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		_return_existing("Payment Entry", existing, submit)
		if clearance_date and not frappe.db.get_value("Payment Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date, "Payment Entry")
		return existing

	if paid_from == paid_to:
		frappe.throw(_("Paid From and Paid To accounts must be different for an Internal Transfer."))
	source, target = _transfer_account(paid_from, company), _transfer_account(paid_to, company)
	precision = frappe.get_precision("Payment Entry", "paid_amount")
	company_currency = erpnext.get_company_currency(company)
	paid = flt(paid_amount, precision)
	if source.account_currency != target.account_currency:
		if received_amount in (None, ""):
			frappe.throw(
				_("Received Amount is required for a transfer between {0} and {1}.").format(
					source.account_currency, target.account_currency
				)
			)
		received = flt(received_amount, precision)
		if paid <= 0 or received <= 0:
			frappe.throw(_("Amount must be greater than zero."))
		fee = flt(charges, precision)
		if fee < 0:
			frappe.throw(_("Charges cannot be negative."))
		if fee >= paid:
			frappe.throw(_("Charges must be less than the amount."))
		rate, target_rate = _cross_currency_rates(
			source,
			target,
			company_currency,
			flt(paid - fee, precision),
			received,
			exchange_rate,
			posting_date,
		)
		difference = flt(fee * rate)
	else:
		received = flt(paid if received_amount is None else received_amount, precision)
		if paid <= 0 or received <= 0:
			frappe.throw(_("Amount must be greater than zero."))
		if received > paid:
			frappe.throw(_("Received Amount cannot be greater than Paid Amount"))
		if source.account_currency == company_currency:
			rate = 1.0
		else:
			rate = flt(exchange_rate) or flt(
				get_exchange_rate(source.account_currency, company_currency, getdate(posting_date))
			)
		if rate <= 0:
			frappe.throw(
				_("Exchange rate from {0} to {1} is required.").format(
					source.account_currency, company_currency
				)
			)
		target_rate = rate
		difference = flt((paid - received) * rate) if received < paid else 0

	details = frappe._dict(details or {})
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = INTERNAL_TRANSFER
	pe.company = company
	pe.posting_date = getdate(posting_date)
	pe.paid_from = paid_from
	pe.paid_from_account_currency = source.account_currency
	pe.paid_from_account_type = source.account_type
	pe.paid_to = paid_to
	pe.paid_to_account_currency = target.account_currency
	pe.paid_to_account_type = target.account_type
	pe.paid_amount = paid
	pe.received_amount = received
	pe.source_exchange_rate = rate
	pe.target_exchange_rate = target_rate
	pe.reference_no = reference_no
	pe.reference_date = getdate(reference_date)
	if remarks:
		pe.custom_remarks = 1
		pe.remarks = remarks

	if difference:
		_append_difference_row(pe, company, deduction_account, difference, details)

	_apply_details(pe, details)
	_set_treasury_fields(pe, source_doctype, source_name, event, cycle)
	pe.flags.ignore_permissions = True  # SECURITY: after the source-document permission + role check
	pe.insert()
	if flt(pe.difference_amount):
		frappe.throw(
			_("The Internal Transfer {0} is not balanced (difference {1}).").format(
				pe.name, pe.difference_amount
			)
		)
	if submit:
		pe.submit()
		if clearance_date:
			_set_clearance_date(pe.name, clearance_date, "Payment Entry")
	return pe.name


def _cross_currency_rates(
	source, target, company_currency: str, principal: float, received: float, exchange_rate, posting_date
) -> tuple[float, float]:
	"""(source rate, target rate) of a transfer between two currencies from the bank's conversion of
	`principal` (source currency) into `received` (target currency): base principal == base received."""
	if source.account_currency == company_currency:
		return 1.0, flt(principal / received, 9)
	if target.account_currency == company_currency:
		return flt(received / principal, 9), 1.0
	rate = flt(exchange_rate) or flt(
		get_exchange_rate(source.account_currency, company_currency, getdate(posting_date))
	)
	if rate <= 0:
		frappe.throw(
			_("Exchange rate from {0} to {1} is required.").format(source.account_currency, company_currency)
		)
	return rate, flt(flt(principal * rate, 2) / received, 9)


def _transfer_account(account: str, company: str) -> frappe._dict:
	details = frappe.db.get_value(
		"Account", account, ["company", "is_group", "account_currency", "account_type"], as_dict=True
	)
	if not details or details.company != company:
		frappe.throw(_("Account {0} does not belong to company {1}.").format(frappe.bold(account), company))
	if details.is_group:
		frappe.throw(_("Account {0} is a group account.").format(frappe.bold(account)))
	return details


def _append_difference_row(pe: Document, company: str, account: str | None, amount: float, details):
	"""The deduction row of an Internal Transfer whose received amount is below the paid amount.

	ERPNext 16 (PaymentEntry.set_exchange_gain_loss, run on every validate for every payment type) keeps
	exactly one row flagged `is_exchange_gain_loss` and sets its amount to base_paid - base_received, then
	subtracts *all* deductions in set_difference_amount: an unflagged charges row would be counted twice and
	the entry could not be submitted ("Difference Amount must be zero"). The difference is therefore put on the
	flagged row itself, with its account changed to `account`; ERPNext keeps the account of that first
	flagged row and only rewrites its amount (spec 7a F1, guarded by the PE settlement tests).
	"""
	if not account:
		frappe.throw(_("An account for the difference between the paid and the received amount is required."))
	_company_currency_account(account, company)
	row = {
		"account": account,
		"cost_center": details.get("cost_center") or erpnext.get_default_cost_center(company),
		"amount": flt(amount, frappe.get_precision("Payment Entry Deduction", "amount")),
		"is_exchange_gain_loss": 1,
	}
	row.update(_dimension_values(frappe.get_meta("Payment Entry Deduction"), details.get("dimensions")))
	pe.append("deductions", row)


def _company_currency_account(account: str, company: str) -> frappe._dict:
	"""A ledger account of `company` in the company currency (a deduction row is booked in it: G3)."""
	details = _transfer_account(account, company)
	company_currency = erpnext.get_company_currency(company)
	if details.account_currency != company_currency:
		frappe.throw(
			_("Account {0} must be in the company currency {1}.").format(
				frappe.bold(account), company_currency
			)
		)
	return details


def _get_payment_amounts(
	*,
	company: str,
	posting_date,
	party_type: str,
	party: str,
	amount: float,
	bank_account_head: str,
	currency: str | None,
	exchange_rate: float | None,
	party_account: str | None = None,
) -> frappe._dict:
	"""Split a cheque amount into the bank side and the party side of a Payment Entry.

	`party_account`: the account the party rules require (`get_party_rules`), else ERPNext's party account."""
	precision = frappe.get_precision("Payment Entry", "paid_amount")
	company_currency = erpnext.get_company_currency(company)
	bank_currency = get_account_currency(bank_account_head)
	if currency and currency != bank_currency:
		frappe.throw(
			_("Currency {0} does not match the currency {1} of account {2}.").format(
				currency, bank_currency, frappe.bold(bank_account_head)
			)
		)

	if party_account:
		_transfer_account(party_account, company)
	else:
		party_account = get_party_account(party_type, party, company)
	if not party_account:
		frappe.throw(_("No party account found for {0} {1}.").format(_(party_type), frappe.bold(party)))
	party_currency = get_account_currency(party_account)

	if bank_currency == company_currency:
		exchange_rate = 1.0
	elif not exchange_rate:
		exchange_rate = get_exchange_rate(bank_currency, company_currency, posting_date)
	if not flt(exchange_rate):
		frappe.throw(_("Exchange rate from {0} to {1} is required.").format(bank_currency, company_currency))

	bank_amount = flt(amount, precision)
	if bank_amount <= 0:
		frappe.throw(_("Amount must be greater than zero."))

	if party_currency == bank_currency:
		party_amount, party_rate = bank_amount, flt(exchange_rate)
	elif party_currency == company_currency:
		party_amount, party_rate = flt(bank_amount * flt(exchange_rate), precision), 1.0
	else:
		frappe.throw(
			_("Party account currency {0} must be either {1} or the company currency {2}.").format(
				party_currency, bank_currency, company_currency
			)
		)

	return frappe._dict(
		precision=precision,
		bank_currency=bank_currency,
		bank_amount=bank_amount,
		bank_rate=flt(exchange_rate),
		party_account=party_account,
		party_currency=party_currency,
		party_amount=party_amount,
		party_rate=party_rate,
	)


def _allocate_references(
	references: list[dict] | None,
	party_type: str,
	party: str,
	party_currency: str,
	party_amount: float,
	precision: int,
) -> list[dict]:
	"""Trim requested allocations to the current outstanding and to the payment amount.

	Amounts are in the party account currency. Whatever cannot be allocated stays unallocated (advance) and the user is warned.
	A document listed more than once is merged into one row (ERPNext rejects duplicate references), and its
	combined allocation never exceeds its outstanding.
	"""
	allocations: dict[tuple[str, str], dict] = {}
	remaining = flt(party_amount, precision)
	short_outstanding, over_payment = [], []

	for ref in references or []:
		doctype, name = ref.get("reference_doctype"), ref.get("reference_name")
		key = (doctype, name)
		requested = flt(ref.get("allocated_amount"), precision)
		outstanding = _get_reference_outstanding(doctype, name, party_type, party, party_currency, precision)
		if key in allocations:
			outstanding = max(flt(outstanding - allocations[key]["allocated_amount"], precision), 0)

		allowed = min(requested, outstanding)
		if allowed < requested and name not in short_outstanding:
			short_outstanding.append(name)
		if allowed > remaining:
			if name not in over_payment:
				over_payment.append(name)
			allowed = remaining
		if allowed <= 0:
			continue

		if key in allocations:
			allocations[key]["allocated_amount"] = flt(
				allocations[key]["allocated_amount"] + allowed, precision
			)
		else:
			allocations[key] = {
				"reference_doctype": doctype,
				"reference_name": name,
				"allocated_amount": allowed,
			}
		remaining = flt(remaining - allowed, precision)

	messages = []
	if short_outstanding:
		messages.append(
			_(
				"The following references are no longer outstanding enough and were reduced or skipped: {0}. The difference is kept as an unallocated advance."
			).format(", ".join(frappe.bold(d) for d in short_outstanding))
		)
	if over_payment:
		messages.append(
			_(
				"The following references were reduced or skipped because the payment amount is already fully allocated: {0}."
			).format(", ".join(frappe.bold(d) for d in over_payment))
		)
	if messages:
		frappe.msgprint("<br>".join(messages), title=_("Allocation Adjusted"), indicator="orange")
	return list(allocations.values())


def get_reference_outstanding(
	doctype: str, name: str, party_type: str, party: str, party_currency: str, precision: int
) -> float:
	"""Outstanding of a reference document in the party account currency, as the Payment Entry allocation
	will see it (used to validate allocations before any voucher exists, e.g. Cheque Batch)."""
	return _get_reference_outstanding(doctype, name, party_type, party, party_currency, precision)


def _get_reference_outstanding(
	doctype: str, name: str, party_type: str, party: str, party_currency: str, precision: int
) -> float:
	"""Outstanding of a reference document in the party account currency (0 if not submitted)."""
	if doctype in hrms.HRMS_DOCTYPES and doctype in hrms.supported_reference_doctypes():
		# Expense Claim / Employee Advance: only where the Payment Entry class accepts them (hrms)
		return hrms.outstanding(doctype, name, party_currency)
	if doctype not in REFERENCE_DOCTYPES:
		frappe.throw(_("Reference type {0} is not supported.").format(_(doctype)))
	if frappe.db.get_value(doctype, name, "docstatus") != 1:
		return 0

	if doctype in INVOICE_DOCTYPES:
		# ERPNext keeps invoice outstanding_amount in the party account currency (from the Payment Ledger).
		outstanding = frappe.db.get_value(doctype, name, "outstanding_amount")
	elif doctype in ORDER_DOCTYPES:
		outstanding = _get_order_outstanding(doctype, name, party_currency)
	else:
		outstanding = _get_ledger_outstanding(doctype, name, party_type, party)
	return max(flt(outstanding, precision), 0)


def _get_order_outstanding(doctype: str, name: str, party_currency: str) -> float:
	"""Order total not yet covered by advances (as ERPNext's `get_reference_details` computes it)."""
	fields = ["currency", "grand_total", "rounded_total", "base_grand_total", "base_rounded_total"]
	order = frappe.db.get_value(doctype, name, [*fields, "advance_paid"], as_dict=True)
	if order.currency == party_currency:
		total = flt(order.rounded_total) or flt(order.grand_total)
	else:
		total = flt(order.base_rounded_total) or flt(order.base_grand_total)
	return total - flt(order.advance_paid)


def get_ledger_outstanding(voucher_type: str, voucher_no: str, party_type: str, party: str) -> float:
	"""Open amount (party account currency) of a voucher for a party, from the Payment Ledger."""
	return _get_ledger_outstanding(voucher_type, voucher_no, party_type, party)


def _get_ledger_outstanding(voucher_type: str, voucher_no: str, party_type: str, party: str) -> float:
	"""Open amount (party account currency) of a voucher for a party, from the Payment Ledger."""
	ple = frappe.qb.DocType("Payment Ledger Entry")
	result = (
		frappe.qb.from_(ple)
		.select(Sum(ple.amount_in_account_currency))
		.where(
			(ple.against_voucher_type == voucher_type)
			& (ple.against_voucher_no == voucher_no)
			& (ple.party_type == party_type)
			& (ple.party == party)
			& (ple.delinked == 0)
		)
	).run()
	return flt(result[0][0]) if result else 0


# ---------------------------------------------------------------------------
# Journal Entry
# ---------------------------------------------------------------------------


@_in_service_context
def create_journal_entry(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	company: str,
	posting_date,
	accounts: list[dict],
	cheque_no: str | None = None,
	cheque_date=None,
	clearance_date=None,
	remarks: str | None = None,
	multi_currency: bool = False,
	voucher_type: str = "Journal Entry",
	reversal_of: str | None = None,
	cycle: int = 1,
	submit: bool = True,
	balance_exchange_difference: bool = False,
) -> str:
	"""Create (or return the existing) Journal Entry for (source, event, cycle).

	`accounts` rows accept the Journal Entry Account fields (account, debit_in_account_currency,
	credit_in_account_currency, party_type, party, exchange_rate, reference_type, reference_name,
	cost_center, user_remark). `clearance_date` requires `submit=True`.

	With `balance_exchange_difference`, rows booked at different exchange rates (e.g. a foreign currency
	cheque carried at the party posting rate and cleared at today's rate) are balanced in the company
	currency by a row on the company's Exchange Gain / Loss account: a Journal Entry does not book that
	difference by itself, it rejects the unbalanced entry.
	"""
	if clearance_date and not submit:
		frappe.throw(_("Clearance Date can only be set on a submitted Journal Entry."))

	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Journal Entry", source_doctype, source_name, event, cycle):
		_return_existing("Journal Entry", existing, submit)
		if clearance_date and not frappe.db.get_value("Journal Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date)
		return existing

	if not accounts:
		frappe.throw(_("At least one account row is required."))
	_check_journal_parties(source_doctype, source_name, company, accounts)

	if balance_exchange_difference and (row := _exchange_difference_row(company, accounts)):
		accounts = [*accounts, row]

	company_currency = erpnext.get_company_currency(company)
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = voucher_type
	je.company = company
	je.posting_date = getdate(posting_date)
	je.cheque_no = cheque_no
	je.cheque_date = getdate(cheque_date) if cheque_date else None
	je.reversal_of = reversal_of
	if remarks:
		je.user_remark = remarks

	for row in accounts:
		row = dict(row)
		if get_account_currency(row["account"]) != company_currency:
			multi_currency = True
		je.append("accounts", row)
	je.multi_currency = 1 if multi_currency else 0

	_set_treasury_fields(je, source_doctype, source_name, event, cycle)
	je.flags.ignore_permissions = True  # SECURITY: after the source-document permission + role check
	je.insert()
	if submit:
		je.submit()
		if clearance_date:
			_set_clearance_date(je.name, clearance_date)
	return je.name


def _check_journal_parties(source_doctype: str, source_name: str, company: str, accounts: list[dict]):
	"""Every party of the rows may be posted by a Journal Entry (party rules, action "journal")."""
	parties = []
	for row in accounts:
		key = (row.get("party_type"), row.get("party"))
		if key[0] and key not in parties:
			parties.append(key)
	for party_type, party in parties:
		rules = get_party_rules(
			source_doctype=source_doctype,
			source_name=source_name,
			company=company,
			party_type=party_type,
			party=party,
			action="journal",
		)
		if not rules.allow_journal_entry:
			frappe.throw(
				_("{0} {1} cannot be posted by a Journal Entry.").format(_(party_type), frappe.bold(party))
			)


def _exchange_difference_row(company: str, accounts: list[dict]) -> dict | None:
	"""Row on the Exchange Gain / Loss account that balances `accounts` in the company currency, if needed.

	Company-currency amounts are rounded per row exactly as JournalEntry.set_amounts_in_company_currency does,
	and a foreign-currency row against an invoice at the invoice's rate, which JournalEntry.set_exchange_rate
	forces on it (e.g. the return of a payment made at another rate).
	"""
	precision = frappe.get_precision("Journal Entry Account", "debit")
	company_currency = erpnext.get_company_currency(company)
	difference = 0.0
	for row in accounts:
		if get_account_currency(row["account"]) == company_currency:
			rate = 1.0
		elif row.get("reference_type") in INVOICE_DOCTYPES and row.get("reference_name"):
			rate = flt(frappe.db.get_value(row["reference_type"], row["reference_name"], "conversion_rate"))
		else:
			rate = flt(row.get("exchange_rate"))
			if not rate:
				frappe.throw(
					_("Exchange rate is required for account {0}.").format(frappe.bold(row["account"]))
				)
		debit = flt(flt(row.get("debit_in_account_currency")) * rate, precision)
		credit = flt(flt(row.get("credit_in_account_currency")) * rate, precision)
		difference += debit - credit

	difference = flt(difference, precision)
	if not difference:
		return None

	account = frappe.get_cached_value("Company", company, "exchange_gain_loss_account")
	if not account:
		frappe.throw(
			_("Set the Exchange Gain / Loss Account in company {0}.").format(frappe.bold(company)),
			title=_("Missing Settings"),
		)
	row = {"account": account, "cost_center": erpnext.get_default_cost_center(company)}
	if difference > 0:
		row["credit_in_account_currency"] = difference
	else:
		row["debit_in_account_currency"] = -difference
	return row


def _set_clearance_date(name: str, clearance_date, doctype: str = "Journal Entry"):
	# JournalEntry.validate() clears clearance_date, so it can only be set after submit
	# (the same way the Bank Reconciliation Tool does); a Payment Entry is cleared the same way.
	frappe.db.set_value(doctype, name, "clearance_date", getdate(clearance_date))


def set_clearance_date(doctype: str, name: str, clearance_date):
	"""Set (or, with an empty date, clear) the clearance date of a submitted Treasury voucher, e.g. when the
	bank confirmed a transfer. The caller checked the user's permission on the Treasury source document."""
	if doctype not in VOUCHER_DOCTYPES:
		frappe.throw(_("Only Payment Entry and Journal Entry can be cleared here."))
	voucher = frappe.db.get_value(doctype, name, ["docstatus", "treasury_source_name"], as_dict=True)
	if not voucher or voucher.docstatus != 1 or not voucher.treasury_source_name:
		frappe.throw(_("{0} {1} is not managed by Treasury.").format(_(doctype), name))
	if clearance_date:
		_set_clearance_date(name, clearance_date, doctype)
	else:
		frappe.db.set_value(doctype, name, "clearance_date", None)


# ---------------------------------------------------------------------------
# Cancellation and reversal
# ---------------------------------------------------------------------------


@_in_service_context
def cancel_voucher(doctype: str, name: str, ignore_linked_doctypes: tuple[str, ...] = ()):
	"""Cancel (or delete, if still draft) a Treasury-managed voucher, bypassing the voucher guard.

	`ignore_linked_doctypes`: Treasury doctypes whose submitted documents still link the voucher by design (e.g.
	a settled Letter of Credit's status log row of its Trust Receipt voucher) and must not block its
	cancellation; added to ERPNext's own list by `voucher_guard.on_cancel`."""
	if doctype not in VOUCHER_DOCTYPES:
		frappe.throw(_("Only Payment Entry and Journal Entry can be cancelled here."))

	doc = frappe.get_doc(doctype, name)
	previous = frappe.flags.treasury_allow_cancel
	frappe.flags.treasury_allow_cancel = True
	try:
		doc.flags.ignore_permissions = True  # SECURITY: after the source-document permission + role check
		doc.flags.treasury_ignore_linked_doctypes = tuple(ignore_linked_doctypes)
		if doc.docstatus == 1:
			doc.cancel()
		elif doc.docstatus == 0:
			# SECURITY: after the source-document permission + role check
			frappe.delete_doc(doctype, name, ignore_permissions=True)
	finally:
		frappe.flags.treasury_allow_cancel = previous


@_in_service_context
def make_reversal(
	doctype: str,
	name: str,
	posting_date,
	event: str,
	remarks: str | None = None,
	cycle: int = 1,
	kept_charges: dict | None = None,
) -> str:
	"""Create a Journal Entry that mirrors a submitted voucher (debit <-> credit), linked to the same source.

	`kept_charges` (Payment Entry only): {"account", "bank_account_head", "amount_in_account_currency"} — the
	charges the bank does not refund stay booked: their rows are not reversed and the bank row gets back the
	amount less the charges (a foreign-currency difference goes to Exchange Gain / Loss)."""
	original = frappe.get_doc(doctype, name)
	if original.docstatus != 1:
		frappe.throw(_("{0} {1} must be submitted to be reversed.").format(_(doctype), name))
	if not original.get("treasury_source_name"):
		frappe.throw(_("{0} {1} is not managed by Treasury.").format(_(doctype), name))
	_check_cancel_only(doctype, name)

	source_doctype, source_name = original.treasury_source_doctype, original.treasury_source_name
	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Journal Entry", source_doctype, source_name, event, cycle):
		return _return_existing("Journal Entry", existing, True)

	if doctype == "Journal Entry":
		rows = _reverse_journal_rows(original)
	else:
		rows = _reverse_gl_rows(doctype, name)
		if kept_charges:
			rows = _keep_charges(rows, frappe._dict(kept_charges))

	return create_journal_entry(
		source_doctype=source_doctype,
		source_name=source_name,
		event=event,
		company=original.company,
		posting_date=posting_date,
		accounts=rows,
		cheque_no=original.get("cheque_no") or original.get("reference_no"),
		cheque_date=original.get("cheque_date") or original.get("reference_date"),
		remarks=remarks or _("Reversal of {0} {1}").format(_(doctype), name),
		multi_currency=bool(original.get("multi_currency")),
		reversal_of=name if doctype == "Journal Entry" else None,
		cycle=cycle,
		balance_exchange_difference=bool(kept_charges),
	)


def _keep_charges(rows: list[dict], kept: frappe._dict) -> list[dict]:
	"""Reversal rows without the charges rows, the bank getting back its credit less the charges."""
	precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")
	rows = [row for row in rows if row["account"] != kept.account]
	bank_row = next(
		(
			row
			for row in rows
			if row["account"] == kept.bank_account_head and flt(row["debit_in_account_currency"]) > 0
		),
		None,
	)
	if not bank_row:
		frappe.throw(
			_("The bank account {0} is not credited by the voucher to reverse.").format(
				frappe.bold(kept.bank_account_head)
			)
		)
	bank_row["debit_in_account_currency"] = flt(
		flt(bank_row["debit_in_account_currency"]) - flt(kept.amount_in_account_currency), precision
	)
	if bank_row["debit_in_account_currency"] <= 0:
		rows.remove(bank_row)
	return rows


def reverse_journal_entry(
	name: str, posting_date, event: str, remarks: str | None = None, cycle: int = 1
) -> str:
	"""Shortcut for `make_reversal("Journal Entry", ...)`."""
	return make_reversal("Journal Entry", name, posting_date, event, remarks, cycle)


def can_reverse_by_payment_entry(pe: Document) -> bool:
	"""A submitted Treasury Internal Transfer without deductions: its mirror is an Internal Transfer too.

	A party Payment Entry cannot be reversed by a Payment Entry (ERPNext rejects a reference to another
	Payment Entry, and a Payment Entry cannot re-open an invoice), and a negative exchange gain / loss row is
	not supported: those are reversed by a Journal Entry.
	"""
	return bool(
		pe.docstatus == 1
		and pe.get("treasury_source_name")
		and pe.payment_type == INTERNAL_TRANSFER
		and not pe.get("deductions")
	)


@_in_service_context
def make_reverse_payment_entry(
	name: str, posting_date, event: str, remarks: str | None = None, cycle: int = 1
) -> str:
	"""Create the Internal Transfer that mirrors the Internal Transfer `name` (same source, accounts swapped,
	same rate, series, mode and accounting details). No clearance date."""
	original = frappe.get_doc("Payment Entry", name)
	if not can_reverse_by_payment_entry(original):
		frappe.throw(_("Payment Entry {0} cannot be reversed by a Payment Entry.").format(frappe.bold(name)))

	source_doctype, source_name = original.treasury_source_doctype, original.treasury_source_name
	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		return _return_existing("Payment Entry", existing, True)

	series = original.naming_series
	details = {
		# a series removed from Payment Entry since then must not block the reversal
		"naming_series": series
		if series in frappe.get_meta("Payment Entry").get_naming_series_options()
		else None,
		"mode_of_payment": original.mode_of_payment,
		"cost_center": original.cost_center,
		"project": original.project,
		"bank_account": original.bank_account,
		"dimensions": {f: original.get(f) for f in get_accounting_dimensions() if original.meta.has_field(f)},
	}
	return create_internal_transfer(
		source_doctype=source_doctype,
		source_name=source_name,
		event=event,
		company=original.company,
		posting_date=posting_date,
		paid_from=original.paid_to,
		paid_to=original.paid_from,
		paid_amount=original.received_amount,
		# a transfer between two currencies: the mirror converts back at the same bank conversion
		received_amount=original.paid_amount
		if original.paid_from_account_currency != original.paid_to_account_currency
		else None,
		exchange_rate=original.target_exchange_rate,
		reference_no=original.reference_no,
		reference_date=original.reference_date,
		remarks=remarks or _("Reversal of Payment Entry {0}").format(name),
		details=details,
		cycle=cycle,
	)


@_in_service_context
def reverse_voucher(
	doctype: str,
	name: str,
	posting_date,
	event: str,
	remarks: str | None = None,
	cycle: int = 1,
	kept_charges: dict | None = None,
) -> tuple[str, str]:
	"""Reverse a Treasury voucher with the fitting voucher: an Internal Transfer without deductions by a
	reverse Payment Entry, anything else (and any reversal keeping charges, see `make_reversal`) by a Journal
	Entry. Returns (doctype, name) of the reversal; the one already created for (source, event, cycle), of
	either doctype, is returned as it is."""
	if doctype not in VOUCHER_DOCTYPES:
		frappe.throw(_("Only Payment Entries and Journal Entries can be reversed here."))
	_check_cancel_only(doctype, name)
	source = frappe.db.get_value(
		doctype, name, ["treasury_source_doctype", "treasury_source_name"], as_dict=True
	)
	if source and source.treasury_source_name:
		for voucher_doctype in VOUCHER_DOCTYPES:
			existing = find_voucher(
				voucher_doctype, source.treasury_source_doctype, source.treasury_source_name, event, cycle
			)
			if existing:
				return voucher_doctype, _return_existing(voucher_doctype, existing, True)

	if (
		not kept_charges
		and doctype == "Payment Entry"
		and can_reverse_by_payment_entry(frappe.get_doc(doctype, name))
	):
		return "Payment Entry", make_reverse_payment_entry(name, posting_date, event, remarks, cycle)
	return "Journal Entry", make_reversal(doctype, name, posting_date, event, remarks, cycle, kept_charges)


def _reverse_journal_rows(je: Document) -> list[dict]:
	rows = []
	dimensions = _row_dimension_fields("Journal Entry Account")
	for d in je.accounts:
		row = {
			"account": d.account,
			"party_type": d.party_type,
			"party": d.party,
			"cost_center": d.cost_center,
			"exchange_rate": d.exchange_rate,
			"debit_in_account_currency": d.credit_in_account_currency,
			"credit_in_account_currency": d.debit_in_account_currency,
			**{f: d.get(f) for f in dimensions if d.get(f)},
		}
		if d.reference_type in INVOICE_DOCTYPES:
			row.update(reference_type=d.reference_type, reference_name=d.reference_name)
		rows.append(row)
	return rows


def _reverse_gl_rows(voucher_type: str, voucher_no: str) -> list[dict]:
	"""Build reversal rows from the GL Entries of a voucher (used for Payment Entries)."""
	gl = frappe.qb.DocType("GL Entry")
	# accounting dimensions that both GL Entry and Journal Entry Account carry are kept on the reversal
	dimensions = [
		f for f in _row_dimension_fields("Journal Entry Account") if frappe.get_meta("GL Entry").has_field(f)
	]
	entries = (
		frappe.qb.from_(gl)
		.select(
			gl.account,
			gl.party_type,
			gl.party,
			gl.cost_center,
			gl.against_voucher_type,
			gl.against_voucher,
			gl.debit,
			gl.credit,
			gl.debit_in_account_currency,
			gl.credit_in_account_currency,
			*(getattr(gl, f) for f in dimensions),
		)
		.where((gl.voucher_type == voucher_type) & (gl.voucher_no == voucher_no) & (gl.is_cancelled == 0))
		.orderby(gl.creation)
	).run(as_dict=True)

	order_advances = _get_order_advances(voucher_type, voucher_no)
	rows = []
	for e in entries:
		if e.against_voucher_type == "Journal Entry":
			# ERPNext's Journal Entry validation rejects a reference back to a Journal Entry on this row
			# (journal_entry.py validate_against_jv), so the reversal could not settle it.
			frappe.throw(
				_(
					"{0} {1} is allocated against Journal Entry {2} and cannot be reversed by a Journal Entry. Cancel it instead."
				).format(_(voucher_type), voucher_no, frappe.bold(e.against_voucher)),
				title=_("Reversal Not Possible"),
			)
		amount_in_account_currency = flt(e.debit_in_account_currency) or flt(e.credit_in_account_currency)
		amount = flt(e.debit) or flt(e.credit)
		row = {
			"account": e.account,
			"party_type": e.party_type,
			"party": e.party,
			"cost_center": e.cost_center,
			"exchange_rate": (amount / amount_in_account_currency) if amount_in_account_currency else 1,
			"debit_in_account_currency": flt(e.credit_in_account_currency),
			"credit_in_account_currency": flt(e.debit_in_account_currency),
			**{f: e.get(f) for f in dimensions if e.get(f)},
		}
		if e.against_voucher_type in INVOICE_DOCTYPES and e.against_voucher:
			row.update(reference_type=e.against_voucher_type, reference_name=e.against_voucher)
		elif (
			e.party_type
			and voucher_type == "Payment Entry"
			and e.against_voucher_type in (None, "", "Payment Entry")
		):
			# Unallocated and order-allocated parts: the Payment Ledger books both against the Payment Entry
			# itself, so match the reversal against it too; otherwise both would stay open.
			row.update(reference_type="Payment Entry", reference_name=e.against_voucher or voucher_no)
			rows.extend(_split_order_advances(row, order_advances))
			continue
		rows.append(row)
	return rows


def _row_dimension_fields(doctype: str) -> list[str]:
	"""Enabled Accounting Dimension fieldnames present on `doctype`."""
	meta = frappe.get_meta(doctype)
	return [f for f in get_accounting_dimensions() if meta.has_field(f)]


def _get_order_advances(voucher_type: str, voucher_no: str) -> list[dict]:
	"""Sales / Purchase Order allocations of a Payment Entry (party account currency), oldest first.

	GL Entry does not store the order (`advance_voucher_*` only exist on the voucher rows and on the Advance
	Payment Ledger Entry), so the orders are read from the Payment Entry's own references.
	"""
	if voucher_type != "Payment Entry":
		return []
	return [
		{"doctype": d.reference_doctype, "name": d.reference_name, "remaining": flt(d.allocated_amount)}
		for d in frappe.get_all(
			"Payment Entry Reference",
			filters={
				"parenttype": "Payment Entry",
				"parent": voucher_no,
				"reference_doctype": ["in", ORDER_DOCTYPES],
			},
			fields=["reference_doctype", "reference_name", "allocated_amount"],
			order_by="idx asc",
		)
		if flt(d.allocated_amount) > 0
	]


def _split_order_advances(row: dict, order_advances: list[dict]) -> list[dict]:
	"""Split a reversal party row into one row per order advance it covers, plus the unallocated rest.

	Each order row carries `advance_voucher_type` / `advance_voucher_no`, so ERPNext books an opposite
	Advance Payment Ledger Entry and recomputes the order's `advance_paid` (as it does for a Journal Entry
	row referencing an order). `order_advances` is consumed so a later party row does not reuse it.
	"""
	precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")
	debit = flt(row["debit_in_account_currency"])
	side = "debit_in_account_currency" if debit else "credit_in_account_currency"
	left = flt(row[side], precision)
	rows = []
	for order in order_advances:
		take = flt(min(order["remaining"], left), precision)
		if take <= 0:
			continue
		rows.append(
			{
				**row,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": 0,
				side: take,
				"advance_voucher_type": order["doctype"],
				"advance_voucher_no": order["name"],
			}
		)
		order["remaining"] = flt(order["remaining"] - take, precision)
		left = flt(left - take, precision)
	if left > 0 or not rows:
		rows.append({**row, "debit_in_account_currency": 0, "credit_in_account_currency": 0, side: left})
	return rows

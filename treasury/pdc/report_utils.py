"""Shared helpers of the PDC script reports, number cards and dashboard charts.

Everything here only reads data: reports never post. Cheques are always read with `frappe.get_list`, so
the user's permissions and User Permissions (e.g. per company) apply. GL Entries are read with `frappe.qb`
only after `validate_company` has checked that the user may see the company's PDC data: Treasury users
are not expected to hold any accounting permission (see README "Roles and permissions").
"""

import json

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt, getdate, today

from treasury.accounting import posting
from treasury.utils.party import validate_party_type

DOCTYPE = "PDC Cheque"
# Translation context of generic words in the reports (e.g. "Party", "Total"), so their Arabic entries in
# locale/ar.po never change the translation of the same words elsewhere in ERPNext. The other generic
# financial words (Amount, Bank Account, Due Date, Status ...) use the app-wide context "Treasury".
CTX = "Treasury Report"
OUTGOING = "Outgoing"
INCOMING = "Incoming"
BANK_POSTED = "Bank Posted"
NOT_POSTED = "Not Posted"

# Cheques whose money has not reached the bank GL yet (their bank posting is still to come). `Cleared`
# covers the bank posting mode `Manual`: cleared by the bank, bank Journal Entry not posted yet. A
# `Discounted` cheque is never expected: the bank already credited its proceeds on the discount date
# (treasury.bank_facilities.discounting); its settlement at maturity never touches the bank account.
EXPECTED_STATUSES = {
	OUTGOING: ("Delivered", "Cleared"),
	INCOMING: ("Received", "Deposited", "Cleared"),
}
# Cheques still in circulation (maturity / aging): not cleared, bounced, cancelled, replaced, returned,
# endorsed or discounted.
OPEN_STATUSES = ("Created", "Delivered", "Received", "Deposited")
# Handed over / received but not posted to the party yet (projected party statement).
UNPOSTED_STATUSES = ("Delivered", "Received", "Deposited")
# Posted to the party (PDC account) and not cleared by the bank yet; `Created` covers `On Creation` mode.
# A discounted cheque stays in PDC Receivable until the drawer pays the bank at maturity.
POSTED_NOT_CLEARED_STATUSES = ("Created", "Delivered", "Received", "Deposited", "Discounted")
POSTED_NOT_CLEARED_ACCOUNTING = ("Party Posted", "Under Collection Posted")

ALL_OPERATIONAL_STATUSES = (
	"Created",
	"Delivered",
	"Received",
	"Deposited",
	"Cleared",
	"Bounced",
	"Returned",
	"Endorsed",
	"Discounted",
	"Replaced",
	"Cancelled",
)

CHEQUE_FIELDS = [
	"name",
	"direction",
	"company",
	"party_type",
	"party",
	"party_name",
	"bank_account",
	"cheque_no",
	"amount",
	"currency",
	"exchange_rate",
	"issue_date",
	"handover_date",
	"original_due_date",
	"effective_due_date",
	"actual_clearance_date",
	"bank_transaction_date",
	"operational_status",
	"accounting_status",
	"postponement_count",
]


# Payment Entry links of the current cycle on a cheque (the paper-filing numbers).
PAYMENT_ENTRY_FIELDS = [
	"party_payment_entry",
	"under_collection_payment_entry",
	"bank_payment_entry",
	"reversal_payment_entry",
]
# Voucher links of the current cycle on a cheque.
VOUCHER_FIELDS = [
	"party_payment_entry",
	"under_collection_payment_entry",
	"bank_payment_entry",
	"reversal_payment_entry",
	"under_collection_journal",
	"bank_journal",
	"bank_charges_journal",
	"reversal_journal",
	"bounce_charges_journal",
]


def report_today():
	"""Today's date for the reports (a single place, so tests can run fixed-date scenarios)."""
	return getdate(today())


def as_list(value) -> list[str]:
	"""A MultiSelectList filter value (list, JSON list or comma separated string) as a list."""
	if not value:
		return []
	if isinstance(value, str):
		value = value.strip()
		if value.startswith("["):
			value = json.loads(value)
		else:
			value = [v.strip() for v in value.split(",")]
	return [v for v in value if v]


def get_precision() -> int:
	return frappe.get_precision(DOCTYPE, "amount") or 2


def column(fieldname: str, label: str, fieldtype: str = "Data", width: int = 110, **extra) -> dict:
	"""A script report column definition."""
	return {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "width": width, **extra}


def cheque_columns() -> list[dict]:
	"""Columns identifying a cheque, shared by the list-style reports."""
	return [
		column("cheque", _("PDC Cheque"), "Link", 160, options=DOCTYPE),
		column("direction", _("Direction"), width=90),
		column("party_type", _("Party Type"), width=90, hidden=1),
		column("party", _("Party", context=CTX), "Dynamic Link", 150, options="party_type"),
		column("party_name", _("Party Name"), width=160),
		column("bank_account", _("Bank Account", context="Treasury"), "Link", 170, options="Bank Account"),
		column("cheque_no", _("Cheque No")),
		column("amount", _("Amount", context="Treasury"), "Currency", 120, options="currency"),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
	]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def is_permitted_value(doctype: str, value: str, applicable_for: str = DOCTYPE) -> bool:
	"""False when the user's User Permissions on `doctype` (for `applicable_for`) exclude `value`."""
	restrictions = [
		p
		for p in get_user_permissions().get(doctype, [])
		if not p.get("applicable_for") or p.get("applicable_for") == applicable_for
	]
	return not restrictions or value in {p.get("doc") for p in restrictions}


def validate_company(company: str | None, doctype: str = DOCTYPE) -> str:
	"""The company filter is required, and the user must be allowed to read the company's `doctype`
	records (PDC cheques by default): read permission plus no User Permission excluding the company."""
	if not company:
		frappe.throw(_("Company is required."))
	if not frappe.db.exists("Company", company):
		frappe.throw(_("{0} {1} does not exist").format(_("Company"), company), frappe.DoesNotExistError)
	frappe.has_permission(doctype, "read", throw=True)
	if not is_permitted_value("Company", company, doctype):
		frappe.throw(
			_("You are not permitted to view the cheques of company {0}.").format(frappe.bold(company)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	return company


def validate_party(party_type: str | None, party: str | None):
	validate_party_type(party_type)  # any Party Type: a cheque may be of an Employee, a Shareholder ...
	if not party:
		frappe.throw(_("Party is required."))
	if not frappe.db.exists(party_type, party):
		frappe.throw(_("{0} {1} does not exist").format(_(party_type), party), frappe.DoesNotExistError)
	if not is_permitted_value(party_type, party):
		frappe.throw(
			_("You are not permitted to view {0} {1}.").format(_(party_type), frappe.bold(party)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)


def validate_date_range(from_date, to_date):
	if from_date and to_date and getdate(from_date) > getdate(to_date):
		frappe.throw(_("From Date cannot be after To Date."))


# ---------------------------------------------------------------------------
# Banks, currencies and amounts
# ---------------------------------------------------------------------------


def get_bank_accounts(company: str, names: list[str] | None = None) -> list[frappe._dict]:
	"""Company bank accounts (all, or `names`) the user may see, with their GL account and its currency.

	User Permissions on Bank Account (applicable to PDC Cheque) apply: the others are left out, and naming
	one of them in `names` raises a PermissionError. A bank account without a GL account has `account` and
	`currency` None (it has no GL balance)."""
	filters = {"company": company, "is_company_account": 1}
	if names:
		filters["name"] = ["in", names]
	rows = frappe.get_all("Bank Account", filters=filters, fields=["name", "account"], order_by="name asc")
	if names:
		missing = set(names) - {r.name for r in rows}
		if missing:
			frappe.throw(
				_("Bank Account {0} does not belong to company {1}.").format(
					", ".join(frappe.bold(m) for m in sorted(missing)), frappe.bold(company)
				)
			)
		forbidden = [r.name for r in rows if not is_permitted_value("Bank Account", r.name)]
		if forbidden:
			frappe.throw(
				_("You are not permitted to view Bank Account {0}.").format(
					", ".join(frappe.bold(f) for f in forbidden)
				),
				frappe.PermissionError,
				title=_("Not Permitted"),
			)
	rows = [r for r in rows if is_permitted_value("Bank Account", r.name)]
	for row in rows:
		row.currency = get_account_currency(row.account) if row.account else None
	return rows


def forecast_date(cheque):
	"""The date a cheque's bank posting is expected on (Bank Position Forecast, its PDC Register drill-down).

	Cleared but not bank posted yet (Manual mode): the bank already moved the money on the bank transaction
	date (else the actual clearance date), whatever the due date. Otherwise the effective due date."""
	if cheque.operational_status == "Cleared":
		cleared_on = cheque.bank_transaction_date or cheque.actual_clearance_date
		if cleared_on:
			return getdate(cleared_on)
	return getdate(cheque.effective_due_date)


class AmountConverter:
	"""Converts cheque amounts to the company currency.

	A cheque carries the rate it was (or will be) posted at in `exchange_rate`; a foreign currency cheque
	not posted yet may have none, and then today's rate is used.
	"""

	def __init__(self, company: str, date=None):
		self.company = company
		self.company_currency = erpnext.get_company_currency(company)
		self.date = getdate(date or report_today())
		self.precision = get_precision()
		self._rates = {}

	def rate(self, row) -> float:
		if not row.currency or row.currency == self.company_currency:
			return 1.0
		if flt(row.exchange_rate):
			return flt(row.exchange_rate)
		if row.currency not in self._rates:
			self._rates[row.currency] = posting.get_cheque_exchange_rate(
				row.currency, self.company, self.date
			)
		return self._rates[row.currency]

	def company_amount(self, row) -> float:
		return flt(flt(row.amount) * self.rate(row), self.precision)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_cheques(
	filters: dict, fields: list[str] | None = None, order_by: str = "effective_due_date asc, name asc"
):
	"""PDC Cheques the user may read (`frappe.get_list`: permissions and User Permissions apply)."""
	return frappe.get_list(DOCTYPE, filters=filters, fields=fields or CHEQUE_FIELDS, order_by=order_by)


def expected_statuses(direction: str, include_created: bool = False) -> list[str]:
	statuses = list(EXPECTED_STATUSES[direction])
	if include_created and direction == OUTGOING:
		statuses.insert(0, "Created")
	return statuses


def get_gl_balance(company: str, accounts: list[str], upto_date, in_account_currency: bool = False) -> float:
	"""Balance (debit - credit) of `accounts` up to and including `upto_date`, from non-cancelled GL."""
	if not accounts:
		return 0.0
	gle = frappe.qb.DocType("GL Entry")
	debit, credit = _gl_amount_fields(gle, in_account_currency)
	result = (
		frappe.qb.from_(gle)
		.select(Sum(debit - credit))
		.where(
			(gle.company == company)
			& (gle.account.isin(list(set(accounts))))
			& (gle.is_cancelled == 0)
			& (gle.posting_date <= getdate(upto_date))
		)
	).run()
	return flt(result[0][0]) if result else 0.0


def get_gl_movements(
	company: str, accounts: list[str], from_date, to_date, in_account_currency: bool = False
) -> list[frappe._dict]:
	"""Debit / credit of `accounts` per posting date within [from_date, to_date]."""
	if not accounts or getdate(from_date) > getdate(to_date):
		return []
	gle = frappe.qb.DocType("GL Entry")
	debit, credit = _gl_amount_fields(gle, in_account_currency)
	return (
		frappe.qb.from_(gle)
		.select(gle.posting_date, Sum(debit).as_("debit"), Sum(credit).as_("credit"))
		.where(
			(gle.company == company)
			& (gle.account.isin(list(set(accounts))))
			& (gle.is_cancelled == 0)
			& (gle.posting_date >= getdate(from_date))
			& (gle.posting_date <= getdate(to_date))
		)
		.groupby(gle.posting_date)
		.orderby(gle.posting_date)
	).run(as_dict=True)


def _gl_amount_fields(gle, in_account_currency: bool):
	if in_account_currency:
		return gle.debit_in_account_currency, gle.credit_in_account_currency
	return gle.debit, gle.credit


def get_linked_vouchers(row) -> str:
	"""Comma separated vouchers linked on a cheque row (the current cycle; fields VOUCHER_FIELDS)."""
	return ", ".join(row.get(f) for f in VOUCHER_FIELDS if row.get(f))


def payment_entries_by_cheque(cheque_names) -> dict[str, str]:
	"""{cheque: comma separated submitted Payment Entries of its current cycle, oldest first} (the "Payment
	Entry" report column: the numbers the cheque's papers are filed under). Two queries for any number of
	cheques; the caller already read the cheques with the user's permissions."""
	names = list(dict.fromkeys(n for n in cheque_names or () if n))
	if not names:
		return {}
	cycles = {
		d.name: cint(d.cycle) or 1
		for d in frappe.get_all(DOCTYPE, filters={"name": ["in", names]}, fields=["name", "cycle"])
	}
	result = {}
	for pe in frappe.get_all(
		"Payment Entry",
		filters={
			"treasury_source_doctype": DOCTYPE,
			"treasury_source_name": ["in", names],
			"docstatus": 1,
		},
		fields=["name", "treasury_source_name", "treasury_cycle"],
		order_by="creation asc, name asc",
	):
		if (cint(pe.treasury_cycle) or 1) == cycles.get(pe.treasury_source_name):
			result.setdefault(pe.treasury_source_name, []).append(pe.name)
	return {cheque: ", ".join(entries) for cheque, entries in result.items()}


def payment_entry_column(width: int = 200) -> dict:
	"""The "Payment Entry" column (Data: several numbers; the report scripts turn each into a link)."""
	return column("payment_entry", _("Payment Entry", context=CTX), "Data", width)

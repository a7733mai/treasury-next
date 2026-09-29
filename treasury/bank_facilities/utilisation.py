"""Utilisation of a Bank Facility, computed from the ledger (never from stored numbers).

Per limit type:

- Loans (Short / Long Term Loan, Murabaha, Tawarruq, Other): the principal outstanding of the facility's
  submitted drawdowns of that type = the balance (credit - debit, facility currency) of each drawdown's
  liability account in the GL rows of the Journal Entries linked to the drawdown or to its repayments
  (`treasury_source_*`). Other postings on the same liability account never count.
- Overdraft: max(0, - GL balance of the GL account of the facility's Default Bank Account), in its currency.
- Anything else: the functions listed in the `treasury_facility_utilisation_sources` hook of any app. Each
  receives (facility doc, as_on date or None) and returns a list of {"limit_type": ..., "amount": ...} in the
  facility currency. Treasury registers the Letters of Guarantee (lg.py), Letters of Credit (lc.py) and
  Cheque Discounting (discounting.py) — see their docstrings:

	treasury_facility_utilisation_sources = ["treasury.bank_facilities.lg.get_lg_utilisation"]

Availability: the sub-limits share the total limit (their sum may exceed it), so a sub-limit's available
amount is min(its limit - its utilisation, total limit - total utilisation), never negative.

`refresh_utilisation` stores the figures in the facility's display fields (read-only); reports, number cards
and the drawdown limit check always call `get_utilisation`.
"""

import frappe
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import flt, getdate, now_datetime

HOOK = "treasury_facility_utilisation_sources"
FACILITY = "Bank Facility"
DRAWDOWN = "Facility Drawdown"
REPAYMENT = "Facility Repayment"
OVERDRAFT = "Overdraft"
# Limit types drawn through a Facility Drawdown (a liability account per sub-limit).
LOAN_TYPES = ("Short Term Loan", "Long Term Loan", "Murabaha", "Tawarruq", "Other")


def get_utilisation(facility, limit_type: str | None = None, as_on=None) -> frappe._dict:
	"""Utilisation of `facility` (name or document) up to `as_on` (None: everything posted).

	Returns {facility, currency, total_limit, utilised, available, percent_used, limits: [{limit_type,
	limit_amount, utilised, available, percent_used}]}; with `limit_type`, `limits` only holds that type (the
	totals stay the facility's)."""
	doc = facility if isinstance(facility, Document) else frappe.get_doc(FACILITY, facility)
	precision = doc.precision("total_limit") or 2
	used = get_utilised_by_type(doc, as_on)
	total_limit = flt(doc.total_limit, precision)
	total_used = flt(sum(used.values()), precision)
	total_available = flt(max(total_limit - total_used, 0), precision)

	limits = []
	for row in doc.limits:
		if limit_type and row.limit_type != limit_type:
			continue
		utilised = flt(used.get(row.limit_type, 0), precision)
		limit_amount = flt(row.limit_amount, precision)
		limits.append(
			frappe._dict(
				limit_type=row.limit_type,
				limit_amount=limit_amount,
				utilised=utilised,
				available=flt(max(min(limit_amount - utilised, total_available), 0), precision),
				percent_used=_percent(utilised, limit_amount),
			)
		)
	return frappe._dict(
		facility=doc.name,
		currency=doc.currency,
		total_limit=total_limit,
		utilised=total_used,
		available=total_available,
		percent_used=_percent(total_used, total_limit),
		limits=limits,
	)


def get_available(facility, limit_type: str, as_on=None) -> float:
	"""Available amount of one sub-limit (0 when the facility has no such sub-limit)."""
	rows = get_utilisation(facility, limit_type, as_on).limits
	return rows[0].available if rows else 0.0


def get_utilised_by_type(doc, as_on=None) -> dict[str, float]:
	used: dict[str, float] = {}
	limit_types = {row.limit_type for row in doc.limits}

	drawdowns = frappe.get_all(
		DRAWDOWN,
		filters={"facility": doc.name, "docstatus": 1},
		fields=["name", "limit_type", "liability_account"],
	)
	outstanding = get_drawdown_outstanding(drawdowns, as_on)
	for drawdown in drawdowns:
		used[drawdown.limit_type] = used.get(drawdown.limit_type, 0) + outstanding.get(drawdown.name, 0)

	if OVERDRAFT in limit_types and doc.default_bank_account:
		used[OVERDRAFT] = used.get(OVERDRAFT, 0) + get_overdraft_utilisation(doc, as_on)

	for method in frappe.get_hooks(HOOK) or []:
		for row in frappe.get_attr(method)(doc, as_on) or []:
			used[row["limit_type"]] = used.get(row["limit_type"], 0) + flt(row.get("amount"))
	return used


def get_overdraft_utilisation(doc, as_on=None) -> float:
	bank_gl = frappe.db.get_value("Bank Account", doc.default_bank_account, "account")
	if not bank_gl:
		return 0.0
	gle = frappe.qb.DocType("GL Entry")
	query = (
		frappe.qb.from_(gle)
		.select(Sum(gle.debit_in_account_currency - gle.credit_in_account_currency))
		.where((gle.company == doc.company) & (gle.account == bank_gl) & (gle.is_cancelled == 0))
	)
	if as_on:
		query = query.where(gle.posting_date <= getdate(as_on))
	balance = flt(query.run()[0][0])
	return max(-balance, 0.0)


def get_drawdown_outstanding(drawdowns: list[dict], as_on=None) -> dict[str, float]:
	"""{drawdown: principal outstanding (facility currency)} from the GL of each drawdown's liability account.

	`drawdowns`: dicts with `name` and `liability_account`."""
	if not drawdowns:
		return {}
	accounts = {d["name"]: d["liability_account"] for d in drawdowns}
	sources = {(DRAWDOWN, name): name for name in accounts}
	for row in frappe.get_all(
		REPAYMENT, filters={"drawdown": ["in", list(accounts)]}, fields=["name", "drawdown"]
	):
		sources[(REPAYMENT, row.name)] = row.drawdown

	balances = get_source_balances(
		list(sources), sorted({a for a in accounts.values() if a}), as_on, credit_minus_debit=True
	)
	outstanding: dict[str, float] = {}
	for (source_doctype, source_name, account), amount in balances.items():
		drawdown = sources.get((source_doctype, source_name))
		if drawdown and accounts.get(drawdown) == account:
			outstanding[drawdown] = outstanding.get(drawdown, 0) + amount
	return outstanding


def get_source_balances(
	sources: list[tuple[str, str]],
	accounts: list[str],
	as_on=None,
	credit_minus_debit: bool = False,
	event_prefix: str | None = None,
) -> dict[tuple[str, str, str], float]:
	"""{(source doctype, source name, account): balance in the account currency} of the non-cancelled GL rows
	of the Journal Entries linked to `sources` (Treasury source links), on `accounts`, up to `as_on`.

	`event_prefix`: only the Journal Entries whose `treasury_event` starts with it."""
	if not sources or not accounts:
		return {}
	gle = frappe.qb.DocType("GL Entry")
	je = frappe.qb.DocType("Journal Entry")
	amount = (
		gle.credit_in_account_currency - gle.debit_in_account_currency
		if credit_minus_debit
		else gle.debit_in_account_currency - gle.credit_in_account_currency
	)
	query = (
		frappe.qb.from_(gle)
		.join(je)
		.on(je.name == gle.voucher_no)
		.select(je.treasury_source_doctype, je.treasury_source_name, gle.account, Sum(amount).as_("balance"))
		.where(
			(gle.voucher_type == "Journal Entry")
			& (gle.is_cancelled == 0)
			& (gle.account.isin(accounts))
			& (je.treasury_source_doctype.isin(sorted({s[0] for s in sources})))
			& (je.treasury_source_name.isin(sorted({s[1] for s in sources})))
		)
		.groupby(je.treasury_source_doctype, je.treasury_source_name, gle.account)
	)
	if as_on:
		query = query.where(gle.posting_date <= getdate(as_on))
	if event_prefix:
		query = query.where(je.treasury_event.like(f"{event_prefix}%"))
	wanted = set(sources)
	return {
		(r.treasury_source_doctype, r.treasury_source_name, r.account): flt(r.balance)
		for r in query.run(as_dict=True)
		if (r.treasury_source_doctype, r.treasury_source_name) in wanted
	}


def refresh_utilisation(facility: str) -> frappe._dict:
	"""Store the utilisation in the facility's read-only display fields (no document save, no version)."""
	doc = frappe.get_doc(FACILITY, facility)
	utilisation = get_utilisation(doc)
	by_type = {row.limit_type: row for row in utilisation.limits}
	for row in doc.limits:
		figures = by_type.get(row.limit_type)
		frappe.db.set_value(
			"Bank Facility Limit",
			row.name,
			{"utilised": figures.utilised, "available": figures.available},
			update_modified=False,
		)
	frappe.db.set_value(
		FACILITY,
		doc.name,
		{
			"total_utilised": utilisation.utilised,
			"total_available": utilisation.available,
			"utilisation_refreshed_on": now_datetime(),
		},
		update_modified=False,
	)
	return utilisation


def _percent(part: float, whole: float) -> float:
	return flt(flt(part) * 100 / flt(whole), 2) if flt(whole) else 0.0

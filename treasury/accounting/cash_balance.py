"""CB-14: a Cash account (a cash box) never goes negative on the posting date of a voucher.

Wired through `doc_events` (`before_submit` of Payment Entry and Journal Entry); validation only, the
voucher's own postings are not touched. A company switches the check off with `block_negative_cash_balance`
on its Treasury Company Settings (default on, also for a company without a settings document).

Rules:
- Only accounts of type Cash count; Bank accounts are covered by the bank balance alerts and guards.
- The balance is the general ledger balance (debit - credit, company currency) up to and including the
  posting date, cancelled entries excluded. Later-dated entries are not looked at.
- A voucher is refused only when it takes money OUT of the account (its net credit on the account is
  positive) and the balance after it is below zero.
- Opening Journal Entries are exempt.
- The Account row is locked first, so two vouchers on one box cannot both pass on the same balance.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Coalesce, Sum
from frappe.utils import flt, fmt_money, formatdate, getdate

SETTINGS = "Treasury Company Settings"
SETTING = "block_negative_cash_balance"
CASH = "Cash"
EPSILON = 0.005


def is_enabled(company: str) -> bool:
	"""The company's setting; on when the company has no Treasury Company Settings."""
	value = frappe.db.get_value(SETTINGS, company, SETTING)
	return True if value is None else bool(value)


def cash_credits(doc) -> dict[str, float]:
	"""{cash account: net amount the voucher credits (company currency)} for the accounts it takes money from."""
	credits: dict[str, float] = {}
	if doc.doctype == "Payment Entry":
		if doc.payment_type in ("Pay", "Internal Transfer") and doc.paid_from:
			credits[doc.paid_from] = flt(doc.base_paid_amount)
		if doc.payment_type == "Internal Transfer" and doc.paid_to:
			credits[doc.paid_to] = credits.get(doc.paid_to, 0.0) - flt(doc.base_received_amount)
	elif doc.doctype == "Journal Entry":
		for row in doc.get("accounts") or []:
			if row.account:
				credits[row.account] = credits.get(row.account, 0.0) + flt(row.credit) - flt(row.debit)
	return {
		account: amount
		for account, amount in credits.items()
		if amount > EPSILON and frappe.get_cached_value("Account", account, "account_type") == CASH
	}


def get_balance(account: str, posting_date) -> float:
	"""General ledger balance (debit - credit) of the account up to and including `posting_date`."""
	gle = frappe.qb.DocType("GL Entry")
	rows = (
		frappe.qb.from_(gle)
		.select(Coalesce(Sum(gle.debit - gle.credit), 0))
		.where(
			(gle.account == account) & (gle.is_cancelled == 0) & (gle.posting_date <= getdate(posting_date))
		)
		.run()
	)
	return flt(rows[0][0]) if rows else 0.0


def before_submit(doc, method=None):
	if doc.get("is_opening") == "Yes" or not doc.get("company") or not is_enabled(doc.company):
		return
	credits = cash_credits(doc)
	if not credits:
		return
	currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	for account in sorted(credits):
		# serialise the vouchers of one box (the lock is held until the transaction ends)
		frappe.db.get_value("Account", account, "name", for_update=True)
		balance = get_balance(account, doc.posting_date)
		if balance - credits[account] < -EPSILON:
			frappe.throw(
				_(
					"Cash account {0} would be negative on {1}: its balance is {2} and this voucher takes out {3}. "
					"Fund the cash box first, or turn off Block negative cash balance in the Treasury Company Settings of {4}."
				).format(
					frappe.bold(account),
					formatdate(doc.posting_date),
					fmt_money(balance, currency=currency),
					fmt_money(credits[account], currency=currency),
					frappe.bold(doc.company),
				),
				title=_("Negative Cash Balance"),
			)

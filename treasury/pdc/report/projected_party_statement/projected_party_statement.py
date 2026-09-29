# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Projected Party Statement (PLAN.md §5.2).

The party's actual ledger from GL (opening balance, then its GL movements per voucher with a running
balance), followed by the cheques of the party that are not posted to the party yet, dated at their
effective due date as if settled then, with a running projected balance.

Balances are debit - credit in the company currency: positive = the party owes us (Dr), negative = we owe
the party (Cr). An outgoing cheque settles as a debit of the party, an incoming one as a credit.

Summary (exposure): actual balance; cheques handed over / received but not posted yet; cheques posted to
the PDC accounts but not cleared by the bank yet (already in the actual balance, but usually not yet in the
party's own books); the net balance after all cheques.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, add_months, cint, flt, getdate

from treasury.pdc.report_utils import (
	CTX,
	DOCTYPE,
	NOT_POSTED,
	OUTGOING,
	POSTED_NOT_CLEARED_ACCOUNTING,
	POSTED_NOT_CLEARED_STATUSES,
	UNPOSTED_STATUSES,
	AmountConverter,
	get_cheques,
	get_precision,
	report_today,
	validate_company,
	validate_date_range,
	validate_party,
)

OPENING, ACTUAL, ACTUAL_CLOSING, PROJECTED, PROJECTED_CLOSING = (
	"opening",
	"actual",
	"actual_closing",
	"projected",
	"projected_closing",
)


def execute(filters=None):
	return ProjectedPartyStatement(filters).run()


class ProjectedPartyStatement:
	def __init__(self, filters=None):
		filters = frappe._dict(filters or {})
		self.company = validate_company(filters.company)
		validate_party(filters.party_type, filters.party)
		self.party_type, self.party = filters.party_type, filters.party
		today = report_today()
		self.from_date = getdate(filters.from_date or add_months(today, -1))
		self.to_date = getdate(filters.to_date or add_days(today, 90))
		validate_date_range(self.from_date, self.to_date)
		self.include_created = cint(filters.include_created_outgoing)
		self.converter = AmountConverter(self.company, today)
		self.currency = self.converter.company_currency
		self.precision = get_precision()
		self.summary_values = frappe._dict()

	def run(self):
		opening = self.get_gl_balance(add_days(self.from_date, -1))
		data = [self.make_row(OPENING, _("Opening Balance", context=CTX), balance=opening, projected=opening)]

		balance = opening
		for entry in self.get_gl_entries():
			balance = flt(balance + flt(entry.debit) - flt(entry.credit), self.precision)
			data.append(
				self.make_row(
					ACTUAL,
					None,
					date=entry.posting_date,
					voucher_type=entry.voucher_type,
					voucher_no=entry.voucher_no,
					debit=entry.debit,
					credit=entry.credit,
					balance=balance,
					projected=balance,
				)
			)
		data.append(
			self.make_row(ACTUAL_CLOSING, _("Actual Closing Balance"), balance=balance, projected=balance)
		)

		projected = balance
		unposted_effect = 0.0
		for cheque in self.get_unposted_cheques():
			amount = self.converter.company_amount(cheque)
			debit, credit = (amount, 0.0) if cheque.direction == OUTGOING else (0.0, amount)
			projected = flt(projected + debit - credit, self.precision)
			unposted_effect += debit - credit
			description = _("{0} cheque {1} ({2})").format(
				_(cheque.direction), cheque.cheque_no, _(cheque.operational_status)
			)
			data.append(
				self.make_row(
					PROJECTED,
					description,
					date=cheque.effective_due_date,
					voucher_type=DOCTYPE,
					voucher_no=cheque.name,
					debit=debit,
					credit=credit,
					projected=projected,
				)
			)
		data.append(
			self.make_row(PROJECTED_CLOSING, _("Projected Closing Balance"), projected=projected)
		)

		summary = self.get_summary(balance, unposted_effect)
		return self.get_columns(), data, None, None, summary

	# ------------------------------------------------------------------ queries

	def gl_conditions(self, gle):
		return (
			(gle.company == self.company)
			& (gle.party_type == self.party_type)
			& (gle.party == self.party)
			& (gle.is_cancelled == 0)
		)

	def get_gl_balance(self, upto_date) -> float:
		gle = frappe.qb.DocType("GL Entry")
		result = (
			frappe.qb.from_(gle)
			.select(Sum(gle.debit - gle.credit))
			.where(self.gl_conditions(gle) & (gle.posting_date <= getdate(upto_date)))
		).run()
		return flt(result[0][0] if result else 0, self.precision)

	def get_gl_entries(self) -> list[frappe._dict]:
		"""The party's GL movements in the range, one row per voucher."""
		gle = frappe.qb.DocType("GL Entry")
		return (
			frappe.qb.from_(gle)
			.select(
				gle.posting_date,
				gle.voucher_type,
				gle.voucher_no,
				Sum(gle.debit).as_("debit"),
				Sum(gle.credit).as_("credit"),
			)
			.where(
				self.gl_conditions(gle)
				& (gle.posting_date >= self.from_date)
				& (gle.posting_date <= self.to_date)
			)
			.groupby(gle.posting_date, gle.voucher_type, gle.voucher_no)
			.orderby(gle.posting_date)
			.orderby(gle.voucher_type)
			.orderby(gle.voucher_no)
		).run(as_dict=True)

	def party_filters(self) -> dict:
		return {"company": self.company, "party_type": self.party_type, "party": self.party}

	def get_unposted_cheques(self) -> list[frappe._dict]:
		"""Cheques of the party not posted to it yet, due up to To Date (overdue ones included)."""
		statuses = list(UNPOSTED_STATUSES)
		if self.include_created:
			statuses.append("Created")
		cheques = get_cheques(
			{
				**self.party_filters(),
				"accounting_status": NOT_POSTED,
				"operational_status": ["in", statuses],
				"effective_due_date": ["<=", self.to_date],
			}
		)
		# "Created" is only meaningful for outgoing cheques (an incoming one is not received yet)
		return [c for c in cheques if c.operational_status != "Created" or c.direction == OUTGOING]

	def get_posted_not_cleared(self) -> list[frappe._dict]:
		return get_cheques(
			{
				**self.party_filters(),
				"accounting_status": ["in", list(POSTED_NOT_CLEARED_ACCOUNTING)],
				"operational_status": ["in", list(POSTED_NOT_CLEARED_STATUSES)],
			}
		)

	# ------------------------------------------------------------------ output

	def signed_amount(self, cheque) -> float:
		amount = self.converter.company_amount(cheque)
		return amount if cheque.direction == OUTGOING else -amount

	def get_summary(self, actual_balance: float, unposted_effect: float) -> list[dict]:
		posted_effect = sum(self.signed_amount(c) for c in self.get_posted_not_cleared())
		values = self.summary_values
		values.actual_balance = flt(actual_balance, self.precision)
		values.unposted_cheques = flt(unposted_effect, self.precision)
		values.posted_not_cleared = flt(posted_effect, self.precision)
		# what the party's own books show if it records cheques only when they clear
		values.balance_before_uncleared = flt(actual_balance - posted_effect, self.precision)
		values.net_after_cheques = flt(actual_balance + unposted_effect, self.precision)

		def card(value, label, indicator="Blue"):
			return {
				"value": value,
				"label": label,
				"datatype": "Currency",
				"currency": self.currency,
				"indicator": indicator,
			}

		return [
			card(values.actual_balance, _("Actual Balance")),
			card(values.unposted_cheques, _("Cheques Handed Over / Received, Not Posted"), "Orange"),
			card(values.posted_not_cleared, _("Cheques Posted, Not Cleared"), "Orange"),
			card(values.balance_before_uncleared, _("Balance Excluding Uncleared Cheques"), "Grey"),
			card(values.net_after_cheques, _("Net Balance After Cheques"), "Green"),
		]

	def make_row(
		self,
		row_type,
		description,
		date=None,
		voucher_type=None,
		voucher_no=None,
		debit=None,
		credit=None,
		balance=None,
		projected=None,
	) -> frappe._dict:
		def amount(value):
			return None if value is None else flt(value, self.precision)

		return frappe._dict(
			row_type=row_type,
			posting_date=date,
			voucher_type=voucher_type,
			voucher_no=voucher_no,
			description=description,
			debit=amount(debit),
			credit=amount(credit),
			balance=amount(balance),
			projected_balance=amount(projected),
			currency=self.currency,
			bold=0 if row_type in (ACTUAL, PROJECTED) else 1,
		)

	def get_columns(self) -> list[dict]:
		def currency(fieldname, label, width=130):
			return {
				"fieldname": fieldname,
				"label": label,
				"fieldtype": "Currency",
				"options": "currency",
				"width": width,
			}

		return [
			{"fieldname": "posting_date", "label": _("Date"), "fieldtype": "Date", "width": 100},
			{"fieldname": "voucher_type", "label": _("Voucher Type"), "fieldtype": "Data", "width": 120},
			{
				"fieldname": "voucher_no",
				"label": _("Voucher No"),
				"fieldtype": "Dynamic Link",
				"options": "voucher_type",
				"width": 170,
			},
			{
				"fieldname": "description",
				"label": _("Description", context=CTX),
				"fieldtype": "Data",
				"width": 240,
			},
			currency("debit", _("Debit", context=CTX)),
			currency("credit", _("Credit", context=CTX)),
			currency("balance", _("Actual Balance"), 140),
			currency("projected_balance", _("Projected Balance"), 150),
			{
				"fieldname": "currency",
				"label": _("Currency"),
				"fieldtype": "Link",
				"options": "Currency",
				"hidden": 1,
			},
		]

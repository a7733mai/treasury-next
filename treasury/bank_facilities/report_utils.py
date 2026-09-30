"""Shared helpers of the Bank Facilities reports and number cards (read only).

Facilities and drawdowns are read with `frappe.get_list` (permissions and User Permissions, e.g. per company,
apply); utilisation always comes from treasury.bank_facilities.utilisation (the ledger), never from the
stored display fields. Schedule rows are read with `frappe.qb` for the drawdowns the user may read only.
Amounts in the company currency are converted at the rate of the reports' today (`report_today`).
"""

import erpnext
import frappe
from frappe.utils import flt, getdate

from treasury.accounting import posting
from treasury.bank_facilities.repayment import PAID, row_status
from treasury.pdc.report_utils import CTX, get_precision, report_today

FACILITY = "Bank Facility"
DRAWDOWN = "Facility Drawdown"
SCHEDULE = "Facility Repayment Schedule"


class CompanyConverter:
	"""Facility currency -> company currency at one date (default: the reports' today)."""

	def __init__(self, company: str, date=None):
		self.company = company
		self.company_currency = erpnext.get_company_currency(company)
		self.date = getdate(date or report_today())
		self.precision = get_precision()
		self._rates: dict[str, float] = {}

	def rate(self, currency: str) -> float:
		if not currency or currency == self.company_currency:
			return 1.0
		if currency not in self._rates:
			self._rates[currency] = posting.get_cheque_exchange_rate(currency, self.company, self.date)
		return self._rates[currency]

	def convert(self, amount: float, currency: str) -> float:
		return flt(flt(amount) * self.rate(currency), self.precision)


def get_facilities(filters: dict, fields: list[str] | None = None) -> list[frappe._dict]:
	return frappe.get_list(
		FACILITY,
		filters=filters,
		fields=fields
		or [
			"name",
			"facility_name",
			"company",
			"bank",
			"lender_type",
			"lender_name",
			"is_islamic",
			"currency",
			"total_limit",
			"expiry_date",
			"status",
		],
		order_by="bank asc, name asc",
	)


def get_readable_drawdowns(filters: dict) -> set[str]:
	return set(frappe.get_list(DRAWDOWN, filters={**filters, "docstatus": 1}, pluck="name"))


def get_installments(
	company: str,
	to_date=None,
	from_date=None,
	facility: str | None = None,
	bank: str | None = None,
	drawdown: str | None = None,
	unpaid_only: bool = True,
	as_of=None,
	repayment_mode: str | None = None,
	lender_type: str | None = None,
) -> list[frappe._dict]:
	"""Schedule rows (prepayment rows excluded) of the submitted drawdowns the user may read, with their unpaid
	amounts, their status on `as_of` (default the reports' today), the repayment mode, the installment cheque
	and its status, and the payment voucher."""
	filters = {"company": company}
	for fieldname, value in (
		("facility", facility),
		("bank", bank),
		("name", drawdown),
		("repayment_mode", repayment_mode),
		("lender_type", lender_type),
	):
		if value:
			filters[fieldname] = value
	readable = get_readable_drawdowns(filters)
	if not readable:
		return []
	dd = frappe.qb.DocType(DRAWDOWN)
	row = frappe.qb.DocType(SCHEDULE)
	query = (
		frappe.qb.from_(row)
		.join(dd)
		.on((dd.name == row.parent) & (row.parenttype == DRAWDOWN))
		.select(
			dd.name.as_("drawdown"),
			dd.facility,
			dd.bank,
			dd.bank_account,
			dd.limit_type,
			dd.currency,
			dd.repayment_mode,
			dd.is_islamic,
			dd.lender_name,
			row.installment_no,
			row.due_date,
			row.principal,
			row.interest,
			row.total,
			row.paid_principal,
			row.paid_interest,
			row.pdc_cheque,
			row.payment_voucher_type,
			row.payment_journal,
		)
		.where((dd.docstatus == 1) & (dd.name.isin(sorted(readable))) & (row.is_prepayment == 0))
		.orderby(row.due_date)
		.orderby(dd.name)
		.orderby(row.installment_no)
	)
	if to_date:
		query = query.where(row.due_date <= getdate(to_date))
	if from_date:
		query = query.where(row.due_date >= getdate(from_date))
	precision = get_precision()
	as_of = getdate(as_of or report_today())
	rows = []
	result = query.run(as_dict=True)
	cheque_status = {
		c.name: c.operational_status
		for c in frappe.get_all(
			"PDC Cheque",
			filters={"name": ["in", sorted({r.pdc_cheque for r in result if r.pdc_cheque}) or [""]]},
			fields=["name", "operational_status"],
		)
	}
	for r in result:
		r.cheque_status = cheque_status.get(r.pdc_cheque) if r.pdc_cheque else None
		r.unpaid_principal = flt(max(flt(r.principal) - flt(r.paid_principal), 0), precision)
		r.unpaid_interest = flt(max(flt(r.interest) - flt(r.paid_interest), 0), precision)
		r.unpaid = flt(r.unpaid_principal + r.unpaid_interest, precision)
		r.status = row_status(r, as_of)
		if unpaid_only and r.status == PAID:
			continue
		rows.append(r)
	return rows

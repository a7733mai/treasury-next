"""Custody Requests in the Bank Position Forecast (hook `treasury_forecast_sources`, spec 9 §6.1).

The money Treasury will pay out on the open requests of the company, from the custody payout bank:
- a temporary request: what is approved and not paid yet, on the request's posting date (a past date shows as
  overdue);
- a float awaiting its initial funding: the funding, on the posting date;
- a float due for replenishment (not closing): the replenishment, today — only with the company's "Include
  custody replenishment in forecast" option.
Open cheques of the request not posted to the custodian yet and its Scheduled transfers are already flows of
their own (PDC Cheques, Bank Transfers): they are subtracted. A request owed back to its custodian (overspent)
is not forecast. The 7e balance alerts read these flows through the same hook.

Nothing without the custody app or without read permission on Custody Request (custody's row permissions
apply to the requests listed).
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from treasury.integrations.custody import REQUEST, documents, is_installed
from treasury.pdc.report_utils import can_read, get_precision, read_list, report_today
from treasury.utils import source_documents as sd

TEMPORARY, PERMANENT = "Temporary", "Permanent"
AWAITING_FUNDING = "Awaiting Funding"
# cheques the PDC forecast already shows (outgoing, not posted to the custodian yet)
FORECAST_CHEQUE_STATUSES = ("Created", "Delivered")
FIELDS = [
	"name",
	"custody_type",
	"custodian_name",
	"posting_date",
	"status",
	"remaining_to_pay",
	"replenishment_amount",
	"replenishment_due",
	"closing_requested",
]


def get_flows(filters) -> list[dict]:
	filters = frappe._dict(filters)
	if not is_installed() or not filters.bank_accounts or not can_read(REQUEST, filters.system):
		return []
	settings = _settings(filters.company)
	if not settings:
		return []
	bank_account = _custody_bank(filters.company, settings)
	if bank_account and bank_account not in set(filters.bank_accounts):
		return []
	include_replenishment = cint(settings.include_custody_replenishment_in_forecast)
	today = getdate(report_today())
	precision = get_precision()
	flows = []
	for row in read_list(
		REQUEST,
		filters.system,
		filters={"company": filters.company, "docstatus": 1, "is_closed": 0},
		fields=FIELDS,
		order_by="posting_date asc, name asc",
	):
		base, date = _base(row, include_replenishment, today)
		if base <= 0:
			continue
		amount = flt(base - reserved_in_forecast(row.name), precision)
		if amount <= 0:
			continue
		flows.append(
			{
				"date": date,
				"direction": "out",
				"bank_account": bank_account,
				"amount": amount,
				"amount_in_account_currency": amount,
				"reference_doctype": REQUEST,
				"reference_name": row.name,
				"party": row.custodian_name,
				"description": _("Custody Request ({0})").format(_(row.custody_type)),
			}
		)
	return flows


def _base(row, include_replenishment: bool, today) -> tuple[float, object]:
	"""(amount still to pay out, its date) of an open request (0 when nothing is forecast)."""
	if row.custody_type == TEMPORARY:
		return flt(row.remaining_to_pay), getdate(row.posting_date)
	if row.custody_type == PERMANENT:
		if row.status == AWAITING_FUNDING:
			return flt(row.replenishment_amount), getdate(row.posting_date)
		if include_replenishment and cint(row.replenishment_due) and not cint(row.closing_requested):
			return flt(row.replenishment_amount), today
	return 0.0, None


def reserved_in_forecast(request: str) -> float:
	"""The request's outgoing cheques not posted to the custodian yet (Created / Delivered) and its Scheduled
	transfers: flows of their own already. Draft transfers are not forecast flows, so they are not subtracted."""
	total = sum(
		flt(amount)
		for amount in frappe.get_all(
			"PDC Cheque",
			filters={
				"source_doctype": REQUEST,
				"source_name": request,
				"direction": documents.OUTGOING,
				"accounting_status": documents.NOT_POSTED,
				"operational_status": ["in", FORECAST_CHEQUE_STATUSES],
			},
			pluck="amount",
		)
	)
	if frappe.get_meta(documents.TRANSFER).has_field("source_name"):
		total += sum(
			flt(amount)
			for amount in frappe.get_all(
				documents.TRANSFER,
				filters={
					"source_doctype": REQUEST,
					"source_name": request,
					"transfer_type": documents.PARTY_PAYMENT,
					"docstatus": 1,
					"status": "Scheduled",
				},
				pluck="amount",
			)
		)
	return total


def _settings(company: str | None):
	if not company or not frappe.db.exists("Treasury Company Settings", company):
		return None
	return frappe.db.get_value(
		"Treasury Company Settings",
		company,
		[
			"custody_bank_account",
			"default_outgoing_cheque_bank_account",
			"include_custody_replenishment_in_forecast",
		],
		as_dict=True,
	)


def _custody_bank(company: str, settings) -> str | None:
	"""The custody payout bank, else the default outgoing cheque bank, in the company currency; None (an
	unassigned flow) without one."""
	bank = settings.get("custody_bank_account") or settings.get("default_outgoing_cheque_bank_account")
	if not bank or sd.bank_currency(bank) != erpnext.get_company_currency(company):
		return None
	return bank

# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""PDC Register (PLAN.md §5.3): incoming and outgoing cheques with every filter and status.

`expected_only` restricts the register to the cheques the Bank Position Forecast counts as expected
(bank posting still to come), so its figures drill down here. The drill-down dates the cheques like the
forecast does: `date_based_on` = "Forecast Date" (report_utils.forecast_date), filtered in Python since it
is the effective due date or, for cleared cheques not bank posted yet, the bank's date. `currency`
(hidden) keeps the cheques of one currency (the forecast's Bank Currency view). `cheque_batch` keeps the
cheques of one Cheque Batch (its "Cheques in PDC Register" button).
"""

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate

from treasury.pdc.lifecycle import FINAL_STATUSES
from treasury.pdc.timing import validate_timing
from treasury.pdc.report_utils import (
	ALL_OPERATIONAL_STATUSES,
	BANK_POSTED,
	CHEQUE_FIELDS,
	CTX,
	INCOMING,
	OUTGOING,
	PAYMENT_ENTRY_FIELDS,
	VOUCHER_FIELDS,
	AmountConverter,
	as_list,
	cheque_columns,
	column,
	expected_statuses,
	forecast_date,
	get_cheques,
	get_linked_vouchers,
	get_precision,
	payment_entries_by_cheque,
	payment_entry_column,
	report_today,
	validate_company,
	validate_date_range,
)

FORECAST_DATE = "Forecast Date"
DATE_FIELDS = {
	"Issue Date": "issue_date",
	"Handover / Receipt Date": "handover_date",
	"Original Due Date": "original_due_date",
	"Effective Due Date": "effective_due_date",
	"Clearance Date": "actual_clearance_date",
	# not a column: see the module docstring
	FORECAST_DATE: "effective_due_date",
}
GROUP_FIELDS = {
	"Party": "party",
	"Bank Account": "bank_account",
	"Operational Status": "operational_status",
}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company)
	validate_date_range(filters.from_date, filters.to_date)
	group_field = GROUP_FIELDS.get(filters.group_by)

	rows = get_rows(filters, company)
	data = group_rows(rows, group_field) if group_field else rows
	data.append(total_row(rows, group_field))
	return get_columns(group_field), data


def get_filters(filters: frappe._dict, company: str) -> list:
	conditions = [["company", "=", company]]
	if filters.direction:
		conditions.append(["direction", "=", filters.direction])
	statuses = as_list(filters.operational_status)
	if invalid := set(statuses) - set(ALL_OPERATIONAL_STATUSES):
		frappe.throw(_("Invalid Operational Status: {0}").format(", ".join(sorted(invalid))))
	if statuses:
		conditions.append(["operational_status", "in", statuses])
	if filters.accounting_status:
		conditions.append(["accounting_status", "=", filters.accounting_status])
	if timing := validate_timing(filters.cheque_timing):
		conditions.append(["cheque_timing", "=", timing])
	if filters.party_type:
		conditions.append(["party_type", "=", filters.party_type])
	if filters.party:
		conditions.append(["party", "=", filters.party])
	if filters.bank_account:
		conditions.append(["bank_account", "=", filters.bank_account])
	if filters.currency:
		conditions.append(["currency", "=", filters.currency])
	if filters.cheque_batch:
		conditions.append(["cheque_batch", "=", filters.cheque_batch])
	if cint(filters.postponed_only):
		conditions.append(["is_postponed", "=", 1])
	if filters.payment_entry:
		# the cheque of a filed paper Payment Entry: any of its Payment Entry links (this cycle's), or the
		# source of the Payment Entry (an earlier cycle's)
		names = frappe.get_all(
			"PDC Cheque",
			filters={"company": company},
			or_filters=[[f, "=", filters.payment_entry] for f in PAYMENT_ENTRY_FIELDS],
			pluck="name",
		)
		source = frappe.db.get_value(
			"Payment Entry", filters.payment_entry, ["treasury_source_doctype", "treasury_source_name"]
		)
		if source and source[0] == "PDC Cheque" and source[1]:
			names.append(source[1])
		conditions.append(["name", "in", names or [""]])

	date_field = DATE_FIELDS.get(filters.date_based_on or "Effective Due Date")
	if not date_field:
		frappe.throw(_("Invalid Date Based On: {0}").format(filters.date_based_on))
	if filters.date_based_on != FORECAST_DATE:
		if filters.from_date:
			conditions.append([date_field, ">=", getdate(filters.from_date)])
		if filters.to_date:
			conditions.append([date_field, "<=", getdate(filters.to_date)])

	if cint(filters.expected_only):
		conditions.append(["accounting_status", "!=", BANK_POSTED])
		if filters.direction in (OUTGOING, INCOMING):
			allowed = expected_statuses(filters.direction, cint(filters.include_created))
		else:
			allowed = sorted(
				set(expected_statuses(OUTGOING, cint(filters.include_created)))
				| set(expected_statuses(INCOMING))
			)
		conditions.append(["operational_status", "in", allowed])
	return conditions


def get_rows(filters: frappe._dict, company: str) -> list[frappe._dict]:
	date_field = DATE_FIELDS.get(filters.date_based_on or "Effective Due Date")
	fields = [*CHEQUE_FIELDS, "is_postponed", *VOUCHER_FIELDS]
	cheques = get_cheques(get_filters(filters, company), fields, order_by=f"{date_field} asc, name asc")

	# `expected_only` with "Include Created": Created cheques are outgoing only
	if cint(filters.expected_only) and not filters.direction:
		cheques = [c for c in cheques if not (c.direction == INCOMING and c.operational_status == "Created")]
	if filters.date_based_on == FORECAST_DATE:
		cheques = [c for c in cheques if in_range(forecast_date(c), filters.from_date, filters.to_date)]
		cheques.sort(key=lambda c: (forecast_date(c), c.name))

	converter = AmountConverter(company)
	today = report_today()
	precision = get_precision()
	payment_entries = payment_entries_by_cheque([c.name for c in cheques])
	rows = []
	for cheque in cheques:
		is_open = cheque.operational_status not in FINAL_STATUSES
		rows.append(
			frappe._dict(
				cheque=cheque.name,
				direction=cheque.direction,
				party_type=cheque.party_type,
				party=cheque.party,
				party_name=cheque.party_name,
				bank_account=cheque.bank_account,
				cheque_no=cheque.cheque_no,
				amount=flt(cheque.amount, precision),
				currency=cheque.currency,
				base_amount=converter.company_amount(cheque),
				company_currency=converter.company_currency,
				issue_date=cheque.issue_date,
				handover_date=cheque.handover_date,
				original_due_date=cheque.original_due_date,
				cheque_timing=cheque.cheque_timing,
				effective_due_date=cheque.effective_due_date,
				actual_clearance_date=cheque.actual_clearance_date,
				days_to_due=date_diff(cheque.effective_due_date, today) if is_open else None,
				operational_status=cheque.operational_status,
				accounting_status=cheque.accounting_status,
				payment_entry=payment_entries.get(cheque.name, ""),
				postponement_count=cint(cheque.postponement_count),
				vouchers=get_linked_vouchers(cheque),
				indent=0,
			)
		)
	return rows


def in_range(date, from_date, to_date) -> bool:
	return (not from_date or date >= getdate(from_date)) and (not to_date or date <= getdate(to_date))


def group_rows(rows: list[frappe._dict], group_field: str) -> list[frappe._dict]:
	"""A header row per group (with its count and company currency subtotal), followed by its cheques."""
	groups: dict[str, list] = {}
	for row in rows:
		groups.setdefault(row.get(group_field) or "", []).append(row)

	data = []
	for key in sorted(groups):
		members = groups[key]
		data.append(
			frappe._dict(
				group_label=group_label(group_field, key, members[0]),
				group_value=key,
				is_group=1,
				indent=0,
				cheque_count=len(members),
				base_amount=flt(sum(r.base_amount for r in members), get_precision()),
				company_currency=members[0].company_currency,
			)
		)
		for row in members:
			row.indent = 1
			data.append(row)
	return data


def total_row(rows: list[frappe._dict], group_field: str | None) -> frappe._dict:
	row = frappe._dict(
		is_total=1,
		indent=0,
		cheque_count=len(rows),
		base_amount=flt(sum(r.base_amount for r in rows), get_precision()),
		company_currency=rows[0].company_currency if rows else None,
	)
	row["group_label" if group_field else "cheque_no"] = _("Total", context=CTX)
	return row


def group_label(group_field: str, key: str, row: frappe._dict) -> str:
	if group_field == "bank_account":
		return key or _("Unassigned Bank")
	if group_field == "operational_status":
		return _(key)
	return f"{row.party_name} ({key})" if row.party_name and row.party_name != key else key


def get_columns(group_field: str | None) -> list[dict]:
	group_labels = {
		"party": _("Party Name"),
		"bank_account": _("Bank Account", context="Treasury"),
		"operational_status": _("Operational Status"),
	}
	columns = [column("group_label", group_labels[group_field], width=220)] if group_field else []
	columns += [
		*cheque_columns(),
		column("base_amount", _("Amount (Company Currency)"), "Currency", 140, options="company_currency"),
		column("cheque_count", _("Cheques"), "Int", 70),
		column("issue_date", _("Issue Date", context="Treasury"), "Date", 100),
		column("handover_date", _("Handover / Receipt Date"), "Date"),
		column("original_due_date", _("Original Due Date"), "Date"),
		column("cheque_timing", _("Cheque Timing"), width=100),
		column("effective_due_date", _("Effective Due Date"), "Date"),
		column("actual_clearance_date", _("Actual Clearance Date"), "Date"),
		column("days_to_due", _("Days to Due"), "Int", 90),
		column("operational_status", _("Operational Status")),
		column("accounting_status", _("Accounting Status"), width=130),
		payment_entry_column(),
		column("postponement_count", _("Postponement Count"), "Int", 90),
		column("vouchers", _("Linked Vouchers"), width=260),
		column(
			"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
		),
	]
	return columns

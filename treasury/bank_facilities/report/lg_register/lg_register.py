# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""LG Register: the submitted Letters of Guarantee with their open amount, the cash margin held and the days to
expiry. "Open Only" (default) shows the guarantees the bank is still committed to (Issued / Extended /
Reduced) and the expired ones whose margin is still held; "Expiring Within (Days)" narrows to the guarantees
expiring by today + that many days. Company currency columns at the reports' today rate.

Guarantees are read with `frappe.get_list` (permissions and User Permissions apply).
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, getdate

from treasury.bank_facilities import lg
from treasury.bank_facilities.report_utils import CompanyConverter, report_today
from treasury.pdc.report_utils import CTX, column, get_precision, validate_company

FIELDS = [
	"name",
	"lg_no",
	"lg_type",
	"facility",
	"bank",
	"beneficiary_type",
	"beneficiary",
	"beneficiary_name",
	"currency",
	"amount",
	"open_amount",
	"margin_held",
	"issue_date",
	"effective_expiry_date",
	"release_date",
	"claim_date",
	"claim_amount",
	"claim_outstanding",
	"status",
]


def execute(filters=None, today=None):
	"""`today` (default: the reports' today) is for the callers of the code, e.g. the Treasury Dashboard."""
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, lg.DOCTYPE)
	today = getdate(today) if today else report_today()
	converter = CompanyConverter(company, today)
	precision = get_precision()

	conditions = {"company": company, "docstatus": 1}
	for fieldname in ("facility", "bank", "lg_type", "status"):
		if filters.get(fieldname):
			conditions[fieldname] = filters.get(fieldname)
	if cint(filters.expiring_within):
		conditions["effective_expiry_date"] = ["<=", add_days(today, cint(filters.expiring_within))]
	rows = frappe.get_list(
		lg.DOCTYPE, filters=conditions, fields=FIELDS, order_by="effective_expiry_date asc, name asc"
	)

	data = []
	for row in rows:
		is_open = row.status in lg.OPEN_STATUSES
		if cint(filters.open_only) and not (is_open or (row.status == lg.EXPIRED and flt(row.margin_held))):
			continue
		if cint(filters.expiring_within) and not is_open:
			continue
		rate = converter.rate(row.currency)
		row.update(
			letter_of_guarantee=row.name,
			status_label=_(row.status, context=lg.DOCTYPE),
			lg_type_label=_(row.lg_type, context=lg.DOCTYPE),
			days_to_expiry=date_diff(row.effective_expiry_date, today) if is_open else None,
			open_company=flt(flt(row.open_amount) * rate, precision) if is_open else 0,
			margin_company=flt(flt(row.margin_held) * rate, precision),
			company_currency=converter.company_currency,
		)
		data.append(row)
	return get_columns(), data, None, None, get_summary(data, converter.company_currency)


def get_summary(data: list, currency: str) -> list[dict]:
	precision = get_precision()

	def card(value, label, indicator, datatype="Currency"):
		item = {"value": value, "label": label, "datatype": datatype, "indicator": indicator}
		if datatype == "Currency":
			item["currency"] = currency
		return item

	return [
		card(flt(sum(r.open_company for r in data), precision), _("Open Guarantees"), "Blue"),
		card(flt(sum(r.margin_company for r in data), precision), _("Cash Margins Held"), "Orange"),
		card(
			len([r for r in data if r.days_to_expiry is not None and r.days_to_expiry <= 30]),
			_("Expiring in 30 Days"),
			"Red",
			"Int",
		),
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, options="currency", width=130):
		return column(fieldname, label, "Currency", width, options=options)

	return [
		column("letter_of_guarantee", _("Letter of Guarantee"), "Link", 150, options=lg.DOCTYPE),
		column("lg_no", _("LG No"), width=110),
		column("lg_type_label", _("LG Type"), width=110),
		column("facility", _("Bank Facility"), "Link", 140, options="Bank Facility"),
		column("bank", _("Bank", context="Treasury"), "Link", 120, options="Bank"),
		column("beneficiary_name", _("Beneficiary Name"), width=160),
		money("amount", _("Amount", context="Treasury")),
		money("open_amount", _("Open Amount")),
		money("margin_held", _("Margin Held")),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		column("issue_date", _("Issue Date", context=lg.DOCTYPE), "Date", 100),
		column("effective_expiry_date", _("Expiry Date", context="Treasury"), "Date", 100),
		column("days_to_expiry", _("Days to Expiry"), "Int", 90),
		column("status_label", _("Status", context="Treasury"), width=100),
		money("claim_outstanding", _("Claim Owed to the Bank")),
		money("open_company", _("Open (Company Currency)"), "company_currency", 150),
		money("margin_company", _("Margin (Company Currency)"), "company_currency", 150),
		column(
			"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
		),
	]

# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""LC Register: the submitted Letters of Credit with their status, amounts (current, open, documents, settled),
the cash margin held and the maturity of accepted usance credits. "Open Only" (default) shows the credits the
bank is still committed to (Opened / Amended / Documents Received / Accepted) and the expired ones whose
margin is still held. Company currency columns at the reports' today rate.

Credits are read with `frappe.get_list` (permissions and User Permissions apply).
"""

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt

from treasury.bank_facilities import lc
from treasury.bank_facilities.report_utils import CompanyConverter, report_today
from treasury.pdc.report_utils import CTX, column, get_precision, validate_company

FIELDS = [
	"name",
	"lc_no",
	"lc_type",
	"facility",
	"bank",
	"supplier",
	"supplier_name",
	"purchase_order",
	"currency",
	"amount",
	"current_amount",
	"open_amount",
	"documents_amount",
	"settled_amount",
	"margin_held",
	"issue_date",
	"effective_expiry_date",
	"documents_date",
	"maturity_date",
	"settlement_date",
	"payment_entry",
	"status",
]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, lc.DOCTYPE)
	today = report_today()
	converter = CompanyConverter(company)
	precision = get_precision()

	conditions = {"company": company, "docstatus": 1}
	for fieldname in ("facility", "bank", "supplier", "lc_type", "status"):
		if filters.get(fieldname):
			conditions[fieldname] = filters.get(fieldname)
	rows = frappe.get_list(lc.DOCTYPE, filters=conditions, fields=FIELDS, order_by="issue_date asc, name asc")

	data = []
	for row in rows:
		is_open = row.status in lc.OPEN_STATUSES
		if cint(filters.open_only) and not (is_open or (row.status == lc.EXPIRED and flt(row.margin_held))):
			continue
		rate = converter.rate(row.currency)
		row.update(
			letter_of_credit=row.name,
			status_label=_(row.status, context=lc.DOCTYPE),
			lc_type_label=_(row.lc_type, context=lc.DOCTYPE),
			days_to_expiry=date_diff(row.effective_expiry_date, today)
			if row.status in lc.AMENDABLE_STATUSES
			else None,
			days_to_maturity=date_diff(row.maturity_date, today) if row.status == lc.ACCEPTED else None,
			open_company=flt(flt(row.open_amount) * rate, precision) if is_open else 0,
			margin_company=flt(flt(row.margin_held) * rate, precision),
			company_currency=converter.company_currency,
		)
		data.append(row)
	return get_columns(), data, None, None, get_summary(data, converter.company_currency)


def get_summary(data: list, currency: str) -> list[dict]:
	precision = get_precision()

	def card(value, label, indicator):
		return {
			"value": flt(value, precision),
			"label": label,
			"datatype": "Currency",
			"currency": currency,
			"indicator": indicator,
		}

	accepted = [r for r in data if r.status == lc.ACCEPTED]
	return [
		card(sum(r.open_company for r in data), _("Open Credits"), "Blue"),
		card(sum(r.margin_company for r in data), _("Cash Margins Held"), "Orange"),
		card(sum(r.open_company - r.margin_company for r in accepted), _("Accepted, to Pay"), "Red"),
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, options="currency", width=130):
		return column(fieldname, label, "Currency", width, options=options)

	return [
		column("letter_of_credit", _("Letter of Credit"), "Link", 150, options=lc.DOCTYPE),
		column("lc_no", _("LC No"), width=110),
		column("lc_type_label", _("LC Type"), width=80),
		column("facility", _("Bank Facility"), "Link", 140, options="Bank Facility"),
		column("bank", _("Bank", context="Treasury"), "Link", 120, options="Bank"),
		column("supplier_name", _("Supplier"), width=160),
		money("current_amount", _("Amount", context="Treasury")),
		money("open_amount", _("Open Amount")),
		money("documents_amount", _("Documents Amount")),
		money("settled_amount", _("Settled Amount")),
		money("margin_held", _("Margin Held")),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		column("issue_date", _("Opening Date"), "Date", 100),
		column("effective_expiry_date", _("Expiry Date", context="Treasury"), "Date", 100),
		column("days_to_expiry", _("Days to Expiry"), "Int", 90),
		column("maturity_date", _("Maturity Date"), "Date", 100),
		column("days_to_maturity", _("Days to Maturity"), "Int", 90),
		column("status_label", _("Status", context="Treasury"), width=130),
		column("payment_entry", _("Payment Entry"), "Link", 150, options="Payment Entry"),
		money("open_company", _("Open (Company Currency)"), "company_currency", 150),
		money("margin_company", _("Margin (Company Currency)"), "company_currency", 150),
		column(
			"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
		),
	]

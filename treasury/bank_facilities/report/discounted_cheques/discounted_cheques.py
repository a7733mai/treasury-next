# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Discounted Cheques: the cheques of the submitted Cheque Discounting batches, grouped by batch (tree), with
their maturity, discount charges and outcome (Discounted = the bank still waits for the drawer, Settled =
paid at maturity, Bounced = taken back from the company). Filters on the item status and the due date.

Batches are read with `frappe.get_list` (permissions and User Permissions apply); their rows with
`frappe.qb` for those batches only.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate

from treasury.bank_facilities import discounting
from treasury.bank_facilities.report_utils import CompanyConverter, report_today
from treasury.pdc.report_utils import (
	CTX,
	column,
	get_precision,
	payment_entries_by_cheque,
	payment_entry_column,
	validate_company,
	validate_date_range,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, discounting.DOCTYPE)
	validate_date_range(filters.from_date, filters.to_date)
	today = report_today()
	converter = CompanyConverter(company)
	precision = get_precision()

	conditions = {"company": company, "docstatus": 1}
	for fieldname in ("facility", "bank"):
		if filters.get(fieldname):
			conditions[fieldname] = filters.get(fieldname)
	if filters.cheque_discounting:
		conditions["name"] = filters.cheque_discounting
	batches = {
		b.name: b
		for b in frappe.get_list(
			discounting.DOCTYPE,
			filters=conditions,
			fields=["name", "facility", "bank", "discount_date", "currency", "status", "discount_rate"],
			order_by="discount_date asc, name asc",
		)
	}
	rows = get_items(list(batches), filters)
	payment_entries = payment_entries_by_cheque([r.pdc_cheque for r in rows])

	data, parents = [], {}
	for item in rows:
		batch = batches[item.parent]
		parent = parents.get(batch.name)
		if not parent:
			parent = parents[batch.name] = frappe._dict(
				row_id=batch.name,
				indent=0,
				label=batch.name,
				cheque_discounting=batch.name,
				facility=batch.facility,
				bank=batch.bank,
				discount_date=batch.discount_date,
				currency=batch.currency,
				status_label=_(batch.status, context=discounting.DOCTYPE),
				amount=0.0,
				discount_charges=0.0,
				outstanding=0.0,
				outstanding_company=0.0,
				company_currency=converter.company_currency,
				children=[],
			)
		is_open = item.status == discounting.ITEM_DISCOUNTED
		outstanding = flt(item.amount, precision) if is_open else 0
		child = frappe._dict(
			row_id=f"{batch.name}-{item.idx}",
			parent_row_id=batch.name,
			indent=1,
			label=item.cheque_no,
			cheque_discounting=batch.name,
			pdc_cheque=item.pdc_cheque,
			party_type=item.party_type,
			party=item.party,
			party_name=item.party_name,
			due_date=item.due_date,
			days=item.days,
			days_to_maturity=date_diff(item.due_date, today) if is_open else None,
			amount=flt(item.amount, precision),
			discount_charges=flt(item.discount_charges, precision),
			outstanding=outstanding,
			outstanding_company=converter.convert(outstanding, batch.currency),
			status_label=_(item.status, context=discounting.ITEM_DOCTYPE),
			payment_entry=payment_entries.get(item.pdc_cheque, ""),
			closed_date=item.closed_date,
			closing_journal=item.closing_journal,
			currency=batch.currency,
			company_currency=converter.company_currency,
		)
		for field in ("amount", "discount_charges", "outstanding", "outstanding_company"):
			parent[field] = flt(parent[field] + child[field], precision)
		parent.children.append(child)

	for parent in parents.values():
		data += [parent, *parent.pop("children")]
	return get_columns(), data, None, None, get_summary(rows, batches, converter)


def get_items(batches: list[str], filters) -> list[frappe._dict]:
	if not batches:
		return []
	item = frappe.qb.DocType(discounting.ITEM_DOCTYPE)
	query = (
		frappe.qb.from_(item)
		.select(
			item.parent,
			item.idx,
			item.pdc_cheque,
			item.cheque_no,
			item.party_type,
			item.party,
			item.party_name,
			item.due_date,
			item.days,
			item.amount,
			item.discount_charges,
			item.status,
			item.closed_date,
			item.closing_journal,
		)
		.where((item.parenttype == discounting.DOCTYPE) & (item.parent.isin(batches)))
		.orderby(item.due_date)
		.orderby(item.parent)
		.orderby(item.idx)
	)
	if filters.status:
		query = query.where(item.status == filters.status)
	if filters.from_date:
		query = query.where(item.due_date >= getdate(filters.from_date))
	if filters.to_date:
		query = query.where(item.due_date <= getdate(filters.to_date))
	rows = query.run(as_dict=True)
	order = {name: index for index, name in enumerate(batches)}
	return sorted(rows, key=lambda r: (order[r.parent], r.idx))


def get_summary(rows: list, batches: dict, converter) -> list[dict]:
	precision = get_precision()

	def total(status):
		return flt(
			sum(converter.convert(r.amount, batches[r.parent].currency) for r in rows if r.status == status),
			precision,
		)

	def card(value, label, indicator):
		return {
			"value": value,
			"label": label,
			"datatype": "Currency",
			"currency": converter.company_currency,
			"indicator": indicator,
		}

	return [
		card(total(discounting.ITEM_DISCOUNTED), _("Discounted, Outstanding"), "Blue"),
		card(total(discounting.ITEM_SETTLED), _("Paid at Maturity"), "Green"),
		card(total(discounting.ITEM_BOUNCED), _("Bounced (Recourse)"), "Red"),
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, options="currency", width=120):
		return column(fieldname, label, "Currency", width, options=options)

	return [
		column("label", _("Batch / Cheque No"), width=180),
		column("cheque_discounting", _("Cheque Discounting"), "Link", 150, options=discounting.DOCTYPE),
		column("pdc_cheque", _("PDC Cheque"), "Link", 150, options="PDC Cheque"),
		column("facility", _("Bank Facility"), "Link", 140, options="Bank Facility"),
		column("bank", _("Bank", context="Treasury"), "Link", 110, options="Bank"),
		column("party_type", _("Party Type"), width=90, hidden=1),
		column("party", _("Party", context=CTX), "Dynamic Link", 140, options="party_type"),
		column("party_name", _("Party Name"), width=150),
		column("discount_date", _("Discount Date"), "Date", 100),
		column("due_date", _("Due Date", context="Treasury"), "Date", 100),
		column("days", _("Days", context=CTX), "Int", 60),
		column("days_to_maturity", _("Days to Maturity"), "Int", 90),
		money("amount", _("Amount", context="Treasury")),
		money("discount_charges", _("Discount Charges")),
		money("outstanding", _("Outstanding", context=CTX)),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		column("status_label", _("Status", context="Treasury"), width=110),
		payment_entry_column(),
		column("closed_date", _("Closed Date"), "Date", 100),
		column("closing_journal", _("Closing Journal Entry"), "Link", 150, options="Journal Entry"),
		money("outstanding_company", _("Outstanding (Company Currency)"), "company_currency", 150),
		column(
			"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
		),
	]

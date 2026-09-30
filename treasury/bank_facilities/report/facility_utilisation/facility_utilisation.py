# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Facility Utilisation (PLAN.md §8): limit, utilised, available and % used per facility and sub-limit.

Tree: Lender, a bank or a finance company (company currency totals) → Facility (its totals) → one row per
sub-limit; an Istisna'a row carries its undisbursed stages (`commitment`, the row's tooltip). Figures come from
treasury.bank_facilities.utilisation.get_utilisation (the ledger up to As On), never from the stored display
fields. Facility figures are in the facility currency, with company currency columns converted at the
reports' today rate; the bank rows and the summary add the company currency columns.

Facilities: of the company, not Closed unless "Include Closed"; Draft ones are left out (nothing may be drawn).
"""

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate

from treasury.bank_facilities import istisna
from treasury.bank_facilities.facility import CLOSED, DRAFT
from treasury.bank_facilities.report_utils import FACILITY, CompanyConverter, get_facilities, report_today
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc.report_utils import CTX, column, get_precision, validate_company


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, FACILITY)
	as_on = getdate(filters.as_on) if filters.as_on else None
	today = report_today()
	converter = CompanyConverter(company)
	precision = get_precision()

	conditions = {"company": company}
	excluded = [DRAFT] if cint(filters.include_closed) else [DRAFT, CLOSED]
	conditions["status"] = ["not in", excluded]
	for fieldname in ("bank", "name", "lender_type"):
		value = filters.get("facility" if fieldname == "name" else fieldname)
		if value:
			conditions[fieldname] = value

	data, banks = [], {}
	for facility in get_facilities(conditions):
		utilisation = get_utilisation(facility.name, filters.limit_type or None, as_on)
		# top level (design 7g P22): the lender, a bank or a finance company
		lender = facility.lender_name or facility.bank
		bank = banks.get(lender)
		if not bank:
			bank = banks[lender] = frappe._dict(
				row_id=f"lender-{lender}",
				indent=0,
				label=lender,
				bank=facility.bank,
				lender_type=_(facility.lender_type or "Bank", context=FACILITY),
				lender_name=lender,
				company_currency=converter.company_currency,
				limit_company=0.0,
				utilised_company=0.0,
				available_company=0.0,
				children=[],
			)
		rate = converter.rate(facility.currency)
		common = {"currency": facility.currency, "company_currency": converter.company_currency}
		facility_row = frappe._dict(
			row_id=f"facility-{facility.name}",
			parent_row_id=bank.row_id,
			indent=1,
			label=facility.facility_name,
			facility=facility.name,
			bank=facility.bank,
			lender_type=_(facility.lender_type or "Bank", context=FACILITY),
			lender_name=facility.lender_name or facility.bank,
			status=_(facility.status, context=FACILITY),
			limit_amount=utilisation.total_limit,
			utilised=utilisation.utilised,
			available=utilisation.available,
			percent_used=utilisation.percent_used,
			expiry_date=facility.expiry_date,
			days_to_expiry=date_diff(facility.expiry_date, today),
			exchange_rate=rate,
			**common,
		)
		limit_rows = []
		for limit in utilisation.limits:
			limit_rows.append(
				frappe._dict(
					row_id=f"{facility_row.row_id}-{limit.limit_type}",
					parent_row_id=facility_row.row_id,
					indent=2,
					label=_(limit.limit_type, context=FACILITY),
					facility=facility.name,
					limit_type=limit.limit_type,
					limit_amount=limit.limit_amount,
					utilised=limit.utilised,
					available=limit.available,
					percent_used=limit.percent_used,
					commitment=_commitment(facility, limit.limit_type, as_on),
					**common,
				)
			)
		if filters.limit_type and not limit_rows:
			continue
		for row in (facility_row, *limit_rows):
			for field in ("limit_amount", "utilised", "available"):
				row[f"{field.replace('_amount', '')}_company"] = flt(flt(row[field]) * rate, precision)
		# with a Limit Type filter the bank totals are those of the sub-limits shown
		source = limit_rows[0] if filters.limit_type else facility_row
		for field in ("limit_company", "utilised_company", "available_company"):
			bank[field] = flt(bank[field] + source[field], precision)
		bank.children += [facility_row, *limit_rows]

	for name in sorted(banks):
		bank = banks[name]
		bank.percent_used = (
			flt(bank.utilised_company * 100 / bank.limit_company, 2) if bank.limit_company else 0
		)
		data += [bank, *bank.pop("children")]
	return get_columns(), data, None, None, get_summary(list(banks.values()), converter.company_currency)


def _commitment(facility, limit_type: str, as_on) -> float:
	"""Design 7g P23: the undisbursed Istisna'a stages included in the utilised amount (the row's tooltip)."""
	if limit_type != istisna.LIMIT_TYPE:
		return 0.0
	return flt(sum(flt(r["amount"]) for r in istisna.get_commitment_utilisation(facility, as_on)))


def get_summary(banks: list, currency: str) -> list[dict]:
	precision = get_precision()

	def card(field, label, indicator):
		return {
			"label": label,
			"value": flt(sum(b[field] for b in banks), precision),
			"datatype": "Currency",
			"currency": currency,
			"indicator": indicator,
		}

	return [
		card("limit_company", _("Total Limits"), "Blue"),
		card("utilised_company", _("Total Utilised"), "Orange"),
		card("available_company", _("Total Available"), "Green"),
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, options="currency", width=130):
		return column(fieldname, label, "Currency", width, options=options)

	return [
		column("label", _("Lender / Facility / Limit Type"), width=240),
		column("facility", _("Bank Facility"), "Link", 150, options=FACILITY),
		column("bank", _("Bank", context="Treasury"), "Link", 130, options="Bank", hidden=1),
		column("lender_type", _("Lender Type"), width=100),
		column("lender_name", _("Lender"), width=160),
		column("limit_type", _("Limit Type"), width=130, hidden=1),
		column("status", _("Status", context="Treasury"), width=90),
		money("limit_amount", _("Limit", context="Treasury")),
		money("utilised", _("Utilised")),
		money("available", _("Available")),
		column("percent_used", _("% Used"), "Percent", 80),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		column("expiry_date", _("Expiry Date", context="Treasury"), "Date", 100),
		column("days_to_expiry", _("Days to Expiry"), "Int", 90),
		money("limit_company", _("Limit (Company Currency)"), "company_currency", 150),
		money("utilised_company", _("Utilised (Company Currency)"), "company_currency", 150),
		money("available_company", _("Available (Company Currency)"), "company_currency", 150),
		column(
			"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
		),
		column("commitment", _("Undisbursed Stages"), "Currency", 130, options="currency", hidden=1),
	]

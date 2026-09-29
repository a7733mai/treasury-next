"""Number Card sources of the Bank Facilities section of the Treasury workspace (read only).

`get_instrument_card`: open Letters of Guarantee / Credit and outstanding discounted cheques (see there).

`get_facility_card`: one method for the four facility cards (`scope`), in the company currency (today's rate):
- limits: the total limits of the Active / Suspended facilities;
- utilised: their utilisation, from the ledger (treasury.bank_facilities.utilisation);
- due_30: the unpaid installments due from today to today + 30 days (sum);
- overdue: the number of unpaid installments due before today.
Facilities and drawdowns are read with `frappe.get_list` (permissions and User Permissions apply).
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate

import erpnext
from treasury.bank_facilities import discounting, lc, lg
from treasury.bank_facilities.facility import OPEN_STATUSES
from treasury.bank_facilities.report_utils import (
	FACILITY,
	CompanyConverter,
	get_facilities,
	get_installments,
	report_today,
)
from treasury.bank_facilities.repayment import OVERDUE
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc.report_utils import get_precision, validate_company

LIMITS, UTILISED, DUE_30, OVERDUE_SCOPE = "limits", "utilised", "due_30", "overdue"
DUE_WINDOW_DAYS = 30
UTILISATION = ["query-report", "Facility Utilisation"]
SCHEDULE = ["query-report", "Facility Repayment Schedule Report"]


@frappe.whitelist()
def get_facility_card(filters: str | dict | None = None) -> dict:
	"""filters: company (default: the user's default company) and scope (limits / utilised / due_30 /
	overdue). Without a company and without a default company the card shows 0."""
	filters = frappe._dict(frappe.parse_json(filters) or {})
	if filters.scope not in (LIMITS, UTILISED, DUE_30, OVERDUE_SCOPE):
		frappe.throw(_("Invalid scope: {0}").format(filters.scope))
	is_count = filters.scope == OVERDUE_SCOPE
	company = filters.company or erpnext.get_default_company()
	if not company:
		return {"value": 0, "fieldtype": "Int" if is_count else "Currency"}

	if filters.scope in (LIMITS, UTILISED):
		company = validate_company(company, FACILITY)
		return _facility_total(company, filters.scope)
	company = validate_company(company, "Facility Drawdown")
	return _installments(company, filters.scope)


def _facility_total(company: str, scope: str) -> dict:
	converter = CompanyConverter(company)
	total = 0.0
	for facility in get_facilities({"company": company, "status": ["in", OPEN_STATUSES]}):
		if scope == LIMITS:
			amount = facility.total_limit
		else:
			amount = get_utilisation(facility.name).utilised
		total += converter.convert(amount, facility.currency)
	return {
		"value": flt(total, get_precision()),
		"fieldtype": "Currency",
		"options": converter.company_currency,
		"route": UTILISATION,
		"route_options": {"company": company},
	}


INSTRUMENT_SCOPES = {
	# scope: (doctype, open statuses, amount field, report)
	"open_lgs": (lg.DOCTYPE, lg.OPEN_STATUSES, "open_amount", "LG Register"),
	"open_lcs": (lc.DOCTYPE, lc.OPEN_STATUSES, "open_amount", "LC Register"),
	"discounted": (discounting.DOCTYPE, discounting.OPEN_STATUSES, "outstanding_amount", "Discounted Cheques"),
}


@frappe.whitelist()
def get_instrument_card(filters: str | dict | None = None) -> dict:
	"""filters: company (default: the user's default company) and scope: open_lgs (open amount of the open
	Letters of Guarantee), open_lcs (open amount of the open Letters of Credit) or discounted (discounted
	cheques not paid or bounced yet). Company currency at the reports' today rate."""
	filters = frappe._dict(frappe.parse_json(filters) or {})
	if filters.scope not in INSTRUMENT_SCOPES:
		frappe.throw(_("Invalid scope: {0}").format(filters.scope))
	doctype, statuses, amount_field, report = INSTRUMENT_SCOPES[filters.scope]
	company = filters.company or erpnext.get_default_company()
	if not company:
		return {"value": 0, "fieldtype": "Currency"}
	company = validate_company(company, doctype)
	converter = CompanyConverter(company)
	rows = frappe.get_list(
		doctype,
		filters={"company": company, "docstatus": 1, "status": ["in", statuses]},
		fields=["currency", amount_field],
	)
	total = sum(converter.convert(row.get(amount_field), row.currency) for row in rows)
	return {
		"value": flt(total, get_precision()),
		"fieldtype": "Currency",
		"options": converter.company_currency,
		"route": ["query-report", report],
		"route_options": {"company": company},
	}


def _installments(company: str, scope: str) -> dict:
	today = report_today()
	if scope == OVERDUE_SCOPE:
		rows = [r for r in get_installments(company, to_date=add_days(today, -1)) if r.status == OVERDUE]
		return {
			"value": len(rows),
			"fieldtype": "Int",
			"route": SCHEDULE,
			"route_options": {"company": company, "status": OVERDUE},
		}
	until = getdate(add_days(today, DUE_WINDOW_DAYS))
	converter = CompanyConverter(company)
	rows = get_installments(company, to_date=until, from_date=today)
	return {
		"value": flt(sum(converter.convert(r.unpaid, r.currency) for r in rows), get_precision()),
		"fieldtype": "Currency",
		"options": converter.company_currency,
		"route": SCHEDULE,
		"route_options": {
			"company": company,
			"from_date": str(today),
			"to_date": str(until),
			"include_overdue": 0,
		},
	}

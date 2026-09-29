"""Number Card source of the Security Cheques section of the Treasury workspace (read only).

One method for the four cards (`scope`): the company currency totals of the security held (incoming,
Received / In Custody) and given (outgoing, Issued) — Open / Blank cheques carry no amount — and the counts
of the cheques still held / given expiring within 30 days (today included) or already expired.
Cheques are read with `frappe.get_list` (permissions and User Permissions apply).
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt

import erpnext
from treasury.pdc.report_utils import get_precision, report_today, validate_company
from treasury.security_cheques.lifecycle import INCOMING, LIVE_BY_DIRECTION, LIVE_STATUSES, OPEN, OUTGOING
from treasury.security_cheques.report_utils import DOCTYPE, AmountConverter, get_security_cheques

HELD, GIVEN, EXPIRING, EXPIRED = "held", "given", "expiring", "expired"
EXPIRING_WINDOW_DAYS = 30
REGISTER = ["query-report", "Security Cheque Register"]


@frappe.whitelist()
def get_security_card(filters: str | dict | None = None) -> dict:
	"""filters: company (default: the user's default company) and scope (held / given / expiring / expired).
	Without a company and without a default company the card shows 0."""
	filters = frappe._dict(frappe.parse_json(filters) or {})
	if filters.scope not in (HELD, GIVEN, EXPIRING, EXPIRED):
		frappe.throw(_("Invalid scope: {0}").format(filters.scope))
	is_total = filters.scope in (HELD, GIVEN)
	company = filters.company or erpnext.get_default_company()
	if not company:
		return {"value": 0, "fieldtype": "Currency" if is_total else "Int"}
	company = validate_company(company, DOCTYPE)

	if is_total:
		return _total(company, INCOMING if filters.scope == HELD else OUTGOING)
	return _count(company, filters.scope)


def _total(company: str, direction: str) -> dict:
	statuses = list(LIVE_BY_DIRECTION[direction])
	cheques = get_security_cheques(
		{
			"company": company,
			"direction": direction,
			"status": ["in", statuses],
			"amount_type": ["!=", OPEN],
		},
		fields=["name", "amount", "currency"],
	)
	converter = AmountConverter(company)
	return {
		"value": flt(sum(converter.company_amount(c) for c in cheques), get_precision()),
		"fieldtype": "Currency",
		"options": converter.company_currency,
		"route": REGISTER,
		"route_options": {"company": company, "direction": direction, "status": statuses},
	}


def _count(company: str, scope: str) -> dict:
	today = report_today()
	conditions = {"company": company, "status": ["in", LIVE_STATUSES]}
	route_options = {"company": company, "status": list(LIVE_STATUSES)}
	if scope == EXPIRING:
		window = [today, add_days(today, EXPIRING_WINDOW_DAYS)]
		conditions["expiry_date"] = ["between", window]
		route_options.update(expiry_from=str(window[0]), expiry_to=str(window[1]))
	else:
		conditions["expiry_date"] = ["<", today]
		route_options.update(expiry_to=str(add_days(today, -1)))
	# counted by the database; `frappe.get_list`: permissions and User Permissions apply
	count = frappe.get_list(DOCTYPE, filters=conditions, fields=[{"COUNT": "*", "as": "count"}])
	return {
		"value": cint(count[0].count) if count else 0,
		"fieldtype": "Int",
		"route": REGISTER,
		"route_options": route_options,
	}

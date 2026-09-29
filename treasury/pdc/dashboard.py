"""Custom Number Card sources of the Treasury workspace (read-only).

The Document Type cards (counts) filter PDC Cheque directly. Sums need the company currency (a company may
hold cheques in several currencies), so they come from `get_cheque_total`, which reads the cheques with
`frappe.get_list` (permissions and User Permissions apply) and converts them like the reports do.
"""

import frappe
from frappe import _
from frappe.utils import add_days

import erpnext
from treasury.pdc.report_utils import (
	BANK_POSTED,
	INCOMING,
	OUTGOING,
	AmountConverter,
	get_cheques,
	report_today,
	validate_company,
)

# Cheques still to be paid / collected, as the number cards count them (also in the card JSON filters).
CARD_STATUSES = {
	OUTGOING: ("Created", "Delivered"),
	INCOMING: ("Received", "Deposited"),
}
DUE_7_DAYS, OUTSTANDING = "due_7_days", "outstanding"
DUE_WINDOW_DAYS = 7


@frappe.whitelist()
def get_cheque_total(filters: str | dict | None = None) -> dict:
	"""Company currency total of open cheques.

	filters: company (default: the user's default company), direction (Outgoing / Incoming) and scope:
	`due_7_days` (effective due date from today to today + 7) or `outstanding` (all open cheques).
	"""
	filters = frappe._dict(frappe.parse_json(filters) or {})
	company = validate_company(filters.company or erpnext.get_default_company())
	if filters.direction not in CARD_STATUSES:
		frappe.throw(_("Direction must be one of: {0}.").format(", ".join((_(OUTGOING), _(INCOMING)))))
	if filters.scope not in (DUE_7_DAYS, OUTSTANDING):
		frappe.throw(_("Invalid scope: {0}").format(filters.scope))

	statuses = list(CARD_STATUSES[filters.direction])
	conditions = {
		"company": company,
		"direction": filters.direction,
		"operational_status": ["in", statuses],
		"accounting_status": ["!=", BANK_POSTED],
	}
	route_options = {
		"company": company,
		"direction": filters.direction,
		"operational_status": statuses,
		"date_based_on": "Effective Due Date",
	}
	if filters.scope == DUE_7_DAYS:
		today = report_today()
		window = [today, add_days(today, DUE_WINDOW_DAYS)]
		conditions["effective_due_date"] = ["between", window]
		route_options.update(from_date=str(window[0]), to_date=str(window[1]))

	converter = AmountConverter(company)
	total = sum(converter.company_amount(c) for c in get_cheques(conditions))
	return {
		"value": total,
		"fieldtype": "Currency",
		"options": converter.company_currency,
		"route": ["query-report", "PDC Register"],
		"route_options": route_options,
	}

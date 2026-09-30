"""Number Card source "Banks Below Minimum (7 days)" of the Treasury workspace (read only, spec 7e §3.7).

The positions are evaluated with the user's own permissions (their User Permissions on Company and Bank
Account apply), like the Bank Balance Outlook report the card opens.
"""

import erpnext
import frappe
from frappe.utils import cint

from treasury.balance_alerts.positions import CRITICAL, evaluate
from treasury.pdc.report_utils import validate_company

DEFAULT_HORIZON_DAYS = 7
MAX_HORIZON_DAYS = 31
REPORT = "Bank Balance Outlook"


@frappe.whitelist()
def get_low_balance_card(filters: str | dict | None = None) -> dict:
	"""filters: company (default: the user's default company) and horizon_days (default 7). The number of
	bank positions projected below their minimum within the horizon."""
	filters = frappe._dict(frappe.parse_json(filters) or {})
	company = filters.company or erpnext.get_default_company()
	if not company:
		return {"value": 0, "fieldtype": "Int"}
	company = validate_company(company)
	days = min(max(cint(filters.horizon_days) or DEFAULT_HORIZON_DAYS, 1), MAX_HORIZON_DAYS)
	positions = evaluate(company, horizon_days=days)
	return {
		"value": sum(1 for p in positions if p.level == CRITICAL),
		"fieldtype": "Int",
		"route": ["query-report", REPORT],
		"route_options": {"company": company, "horizon_days": days, "level": CRITICAL},
	}

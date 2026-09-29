"""Phase 7b: Treasury Company Settings rows saved before the Bank Transfer settings get their defaults.

Idempotent: only empty values are set; the fee rules and limits start empty (no data migration: there are no
transfers yet).
"""

import frappe
from frappe.query_builder.functions import IfNull

DEFAULTS = {
	"default_transfer_method": "Normal",
	"default_charge_bearer": "SHA",
	"transfer_completion": "On Bank Confirmation",
	"transfer_limit_action": "Warn",
}


def execute():
	settings = frappe.qb.DocType("Treasury Company Settings")
	for fieldname, value in DEFAULTS.items():
		field = getattr(settings, fieldname)
		frappe.qb.update(settings).set(field, value).where(IfNull(field, "") == "").run()

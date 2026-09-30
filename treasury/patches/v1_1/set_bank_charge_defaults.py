"""Phase 7d: Treasury Company Settings rows saved before the Bank Charge settings get their defaults.

Idempotent: only empty values are set (no data migration: there are no bank charges yet).
"""

import frappe
from frappe.query_builder.functions import IfNull

DEFAULTS = {
	"bank_charge_voucher": "Payment Entry",
	"bank_charge_clearance": "On Posting",
}


def execute():
	settings = frappe.qb.DocType("Treasury Company Settings")
	for fieldname, value in DEFAULTS.items():
		field = getattr(settings, fieldname)
		frappe.qb.update(settings).set(field, value).where(IfNull(field, "") == "").run()

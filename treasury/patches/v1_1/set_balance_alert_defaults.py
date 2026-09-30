"""Phase 7e: Treasury Company Settings rows saved before the bank balance alerts get On Low Projected Balance
"Warn" (a no-op until Balance Thresholds are set).

Idempotent: only empty values are set (no data migration: thresholds start empty).
"""

import frappe
from frappe.query_builder.functions import IfNull

DEFAULT_MODE = "Warn"


def execute():
	settings = frappe.qb.DocType("Treasury Company Settings")
	frappe.qb.update(settings).set(settings.on_low_balance, DEFAULT_MODE).where(
		IfNull(settings.on_low_balance, "") == ""
	).run()

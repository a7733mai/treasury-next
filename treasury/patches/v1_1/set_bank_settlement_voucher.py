"""Phase 7a: Treasury Company Settings rows saved before the new fields get their defaults.

`bank_settlement_voucher` = "Payment Entry" (the new default; a company keeps the 1.0 behaviour by choosing
"Journal Entry") and `mode_name_template` = "{method} - {bank}". Idempotent; no voucher is touched: cheques
already posted keep their Journal Entries, and later events follow each voucher's own doctype.
"""

import frappe
from frappe.query_builder.functions import IfNull

DEFAULTS = {"bank_settlement_voucher": "Payment Entry", "mode_name_template": "{method} - {bank}"}


def execute():
	settings = frappe.qb.DocType("Treasury Company Settings")
	for fieldname, value in DEFAULTS.items():
		field = getattr(settings, fieldname)
		frappe.qb.update(settings).set(field, value).where(IfNull(field, "") == "").run()

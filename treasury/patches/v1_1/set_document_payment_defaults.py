"""Phase 7c: Treasury Company Settings rows saved before the "Pay / Receive from Documents" section get its
defaults (installments: 12, Monthly; cheque book auto-selection on).

Idempotent: the installment values are set only where empty; the cheque book auto-selection is a new check
field (0 on the existing rows) that this patch, which runs once, turns on everywhere.
"""

import frappe
from frappe.query_builder.functions import IfNull


def execute():
	settings = frappe.qb.DocType("Treasury Company Settings")
	frappe.qb.update(settings).set(settings.default_installment_frequency, "Monthly").where(
		IfNull(settings.default_installment_frequency, "") == ""
	).run()
	frappe.qb.update(settings).set(settings.default_installment_count, 12).where(
		IfNull(settings.default_installment_count, 0) == 0
	).run()
	frappe.qb.update(settings).set(settings.auto_select_cheque_book, 1).where(
		IfNull(settings.auto_select_cheque_book, 0) == 0
	).run()

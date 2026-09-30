"""Phase 8: PDC Cheques saved before 1.2 get their Cheque Timing (treasury.pdc.timing), computed from the dates.

Idempotent: only rows without a timing (NULL or empty) are set, since every save computes it afterwards, and
`modified` is not touched. The rule is the one of `get_cheque_timing`: Current-dated when the original due date
is on (or before) the issue date, else Post-dated; a row without both dates keeps no timing.
"""

import frappe
from frappe.query_builder.functions import IfNull

from treasury.pdc.timing import CURRENT, POST_DATED


def execute():
	cheque = frappe.qb.DocType("PDC Cheque")
	pending = (
		(IfNull(cheque.cheque_timing, "") == "")
		& cheque.issue_date.isnotnull()
		& cheque.original_due_date.isnotnull()
	)
	frappe.qb.update(cheque).set(cheque.cheque_timing, CURRENT).where(
		pending & (cheque.original_due_date <= cheque.issue_date)
	).run()
	frappe.qb.update(cheque).set(cheque.cheque_timing, POST_DATED).where(
		pending & (cheque.original_due_date > cheque.issue_date)
	).run()

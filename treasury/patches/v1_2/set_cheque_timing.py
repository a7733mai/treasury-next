"""Phase 8: PDC Cheques saved before 1.2 get their Cheque Timing (treasury.pdc.timing), recomputed from the dates.

A full recompute: every row with both an issue and an original due date gets the value of the rule of
`get_cheque_timing` (Current-dated when the original due date is on or before the issue date, else Post-dated),
so a missing or wrong stored value is corrected. A row without both dates is left as it is. Safe to run twice
(the result depends only on the dates) and `modified` is not touched.
"""

import frappe

from treasury.pdc.timing import CURRENT, POST_DATED


def execute():
	cheque = frappe.qb.DocType("PDC Cheque")
	dated = cheque.issue_date.isnotnull() & cheque.original_due_date.isnotnull()
	frappe.qb.update(cheque).set(cheque.cheque_timing, CURRENT).where(
		dated & (cheque.original_due_date <= cheque.issue_date)
	).run()
	frappe.qb.update(cheque).set(cheque.cheque_timing, POST_DATED).where(
		dated & (cheque.original_due_date > cheque.issue_date)
	).run()

"""Status of a Cheque Batch, derived from the PDC Cheques it created.

Kept apart from `treasury.pdc.batch` (which drives the lifecycle) so that `treasury.pdc.lifecycle` can refresh
the status of a cheque's batch after every action without a circular import.
"""

import frappe

BATCH_DOCTYPE = "Cheque Batch"
CHEQUE_DOCTYPE = "PDC Cheque"
# Cheques that no longer represent an installment of the batch: a Replaced cheque's replacement carries the
# batch link on, and a Cancelled one was voided on purpose.
RELEASED_STATUSES = ("Cancelled", "Replaced")


def compute_batch_status(docstatus: int, direction: str, statuses: list[str]) -> str:
	"""Draft / Cancelled from the docstatus; otherwise from the operational statuses of the batch's cheques.

	Every cheque not Created any more (Delivered / Received, Deposited, Bounced, Returned, ...) counts as handed
	over; only Cleared counts as cleared.
	"""
	if docstatus == 0:
		return "Draft"
	if docstatus == 2:
		return "Cancelled"
	active = [s for s in statuses if s not in RELEASED_STATUSES]
	if not active:
		return "Submitted"
	cleared = sum(1 for s in active if s == "Cleared")
	if cleared:
		return "Cleared" if cleared == len(active) else "Partially Cleared"
	handed_over = sum(1 for s in active if s != "Created")
	delivered, partially = (
		("Received", "Partially Received") if direction == "Incoming" else ("Delivered", "Partially Delivered")
	)
	if not handed_over:
		return "Submitted"
	return delivered if handed_over == len(active) else partially


def update_batch_status(batch: str) -> str | None:
	"""Recompute and store the status of `batch` (no permission check: it is derived data)."""
	values = frappe.db.get_value(BATCH_DOCTYPE, batch, ["docstatus", "direction", "status"], as_dict=True)
	if not values:
		return None
	statuses = frappe.get_all(CHEQUE_DOCTYPE, filters={"cheque_batch": batch}, pluck="operational_status")
	status = compute_batch_status(values.docstatus, values.direction, statuses)
	if status != values.status:
		frappe.db.set_value(BATCH_DOCTYPE, batch, "status", status, update_modified=False)
	return status

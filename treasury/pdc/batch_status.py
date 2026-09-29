"""Status of a Cheque Batch, derived from the PDC Cheques it created.

Kept apart from `treasury.pdc.batch` (which drives the lifecycle) so that `treasury.pdc.lifecycle` can refresh
the status of a cheque's batch after every action without a circular import.

Concurrency: every lifecycle action on a cheque of a batch locks the batch row *before* the cheque row
(`lock_batch`, called from `lifecycle._begin`), and so do the batch's own submit / cancel / bulk actions. Actions
on cheques of the same batch are therefore serialised, and `update_batch_status` always sees (with a locking
read) the latest statuses of all the batch's cheques: two concurrent actions can never store a stale status.
"""

import frappe

BATCH_DOCTYPE = "Cheque Batch"
CHEQUE_DOCTYPE = "PDC Cheque"
# Cheques that no longer represent an installment of the batch: a Replaced cheque's replacement carries the
# batch link on, and a Cancelled one was voided on purpose.
RELEASED_STATUSES = ("Cancelled", "Replaced")
# Cheques whose installment is settled: cleared by the bank, or passed on (endorsed to a third party /
# discounted at the bank) so that the batch has nothing left to collect or pay on them.
SETTLED_STATUSES = ("Cleared", "Endorsed", "Discounted")


def compute_batch_status(docstatus: int, direction: str, statuses: list[str]) -> str:
	"""Draft / Cancelled from the docstatus; otherwise from the operational statuses of the batch's cheques.

	Every cheque not Created any more (Delivered / Received, Deposited, Bounced, Returned, ...) counts as handed
	over; Cleared, Endorsed and Discounted count as settled ("Cleared").
	"""
	if docstatus == 0:
		return "Draft"
	if docstatus == 2:
		return "Cancelled"
	active = [s for s in statuses if s not in RELEASED_STATUSES]
	if not active:
		return "Submitted"
	settled = sum(1 for s in active if s in SETTLED_STATUSES)
	if settled:
		return "Cleared" if settled == len(active) else "Partially Cleared"
	handed_over = sum(1 for s in active if s != "Created")
	delivered, partially = (
		("Received", "Partially Received") if direction == "Incoming" else ("Delivered", "Partially Delivered")
	)
	if not handed_over:
		return "Submitted"
	return delivered if handed_over == len(active) else partially


def lock_batch(batch: str) -> frappe._dict | None:
	"""Lock the batch row until the end of the transaction; returns its docstatus, direction and status."""
	return frappe.db.get_value(
		BATCH_DOCTYPE, batch, ["docstatus", "direction", "status"], as_dict=True, for_update=True
	)


def get_cheque_statuses(batch: str) -> list[str]:
	"""Operational statuses of the batch's cheques, read with a locking read (the latest committed values,
	not the transaction's snapshot)."""
	cheque = frappe.qb.DocType(CHEQUE_DOCTYPE)
	return (
		frappe.qb.from_(cheque)
		.select(cheque.operational_status)
		.where(cheque.cheque_batch == batch)
		.for_update()
		.run(pluck=True)
	)


def update_batch_status(batch: str) -> str | None:
	"""Recompute and store the status of `batch` (no permission check: it is derived data)."""
	values = lock_batch(batch)
	if not values:
		return None
	status = compute_batch_status(values.docstatus, values.direction, get_cheque_statuses(batch))
	if status != values.status:
		frappe.db.set_value(BATCH_DOCTYPE, batch, "status", status, update_modified=False)
	return status


def in_batch_operation(batch: str | None) -> bool:
	"""True while `treasury.pdc.batch` acts on all the cheques of `batch` (submit, cancel, Deliver / Receive
	All): the per-cheque refresh of the batch status is skipped then, the operation refreshes it once."""
	return bool(batch) and frappe.flags.get("treasury_batch_operation") == batch

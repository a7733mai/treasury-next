"""Daily Bank Transfer job (hooks.py `scheduler_events`): send the Scheduled transfers whose execution date
has come.

Each transfer is sent through `treasury.bank_transfers.lifecycle.send(system=True)` — the rules of "Send Now",
without the user permission check (a scheduler job has no acting user; the approval was checked when the
transfer was submitted), the transfer limits of the day enforced under the Bank Account lock — in its own
savepoint: a failing transfer is rolled back, stays Scheduled and is
logged (once per transfer and day), the others go on; each is committed on its own. Days on which the job did
not run are caught up: every transfer due up to `as_of` is posted on its own execution date.
"""

import frappe
from frappe import _
from frappe.utils import getdate, today

from treasury.bank_transfers import lifecycle

DOCTYPE = "Bank Transfer"
SAVEPOINT = "treasury_bank_transfer_scheduler"


def post_scheduled_transfers(as_of=None) -> dict:
	"""Send every due Scheduled transfer of all companies. Returns {"posted": [], "failed": []}."""
	as_of = getdate(as_of or today())
	result = {"posted": [], "failed": []}
	for name in _due_transfers(as_of):
		if _send_one(name):
			result["posted"].append(name)
		else:
			result["failed"].append(name)
		_commit()
	return result


def _due_transfers(as_of) -> list[str]:
	return frappe.get_all(
		DOCTYPE,
		filters={"docstatus": 1, "status": lifecycle.SCHEDULED, "execution_date": ["<=", as_of]},
		order_by="execution_date asc, creation asc",
		pluck="name",
	)


def _commit():
	"""Keep each transfer's result even if the job dies later (tests run in one rolled back transaction)."""
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: allowed in scheduler jobs


def _send_one(name: str) -> bool:
	frappe.db.savepoint(SAVEPOINT)
	try:
		lifecycle.send(name, system=True)
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.clear_messages()
		_log_failure(name)
		return False
	frappe.db.release_savepoint(SAVEPOINT)
	return True


def _log_failure(name: str):
	"""Error Log of a failing transfer, at most once per day (it may fail every day, e.g. a closed period)."""
	title = _("Scheduled bank transfer failed: {0}").format(name)
	if frappe.db.exists(
		"Error Log",
		{"reference_doctype": DOCTYPE, "reference_name": name, "method": title, "creation": [">=", today()]},
	):
		return
	frappe.log_error(title=title, reference_doctype=DOCTYPE, reference_name=name)

"""Service execution context of Treasury code that builds ERPNext vouchers."""

from contextlib import contextmanager

import frappe

SYSTEM_USER = "Administrator"
REAL_USER_FLAG = "treasury_real_user"


@contextmanager
def service_context(actor: str | None = None):
	"""Mark a block of Treasury service code that runs *after* the caller checked the user's permission (and
	role) on the Treasury source document; yields the acting user.

	The session user is NOT changed: vouchers, their GL / Payment Ledger rows and their Version history are
	created under the real user, and ERPNext's role-based accounting guards (frozen accounts, accounting
	periods, credit limits, ...) apply to that user. What the block does change, narrowly:

	- `frappe.flags.ignore_account_permission` (ERPNext's own flag, read by `get_balance_on` and
	  `get_account_details`): the user's Account read permission is not required to compute a balance. Treasury
	  roles hold no accounting permission; the app grants none.
	- the acting user (`real_user()`, used for the Treasury status log): the session user that entered the
	  outermost block, or `actor` for a system action (the scheduler passes Administrator).

	Document-level permission bypasses are never set here: each document built by the service sets
	`doc.flags.ignore_permissions` itself with a `# SECURITY:` comment naming the preceding Treasury check.
	Never wrap user-controlled code in it. Nested blocks keep the outer acting user.
	"""
	outer = frappe.flags.get(REAL_USER_FLAG)
	outer_account_flag = frappe.flags.get("ignore_account_permission")
	acting = outer or actor or frappe.session.user
	frappe.flags[REAL_USER_FLAG] = acting
	frappe.flags.ignore_account_permission = True
	try:
		yield acting
	finally:
		frappe.flags[REAL_USER_FLAG] = outer
		frappe.flags.ignore_account_permission = outer_account_flag


def real_user() -> str:
	"""The user acting in the current Treasury action: the session user, outside `service_context` too."""
	return frappe.flags.get(REAL_USER_FLAG) or frappe.session.user

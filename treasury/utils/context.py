"""Elevated execution context for Treasury service code."""

from contextlib import contextmanager

import frappe

SYSTEM_USER = "Administrator"
REAL_USER_FLAG = "treasury_real_user"


@contextmanager
def elevated(actor: str | None = None):
	"""Run the block as Administrator and restore the session afterwards; yields the acting user.

	Only for service code that runs *after* the caller checked the user's permission (and role) on the
	Treasury source document: ERPNext checks the session user's own permissions while it builds vouchers
	(e.g. Account in `get_party_account`, the reference documents of a Payment Entry), and Treasury users
	are not expected to hold accounting permissions. Never wrap user-controlled code in it.

	The acting user (`real_user()`, used for the Treasury status log and the voucher owner) is the session
	user that entered the outermost block, or `actor` for a system action (e.g. the scheduler passes
	Administrator). Nested blocks keep the outer acting user.
	"""
	session = frappe.local.session
	user = session.user
	outer = frappe.flags.get(REAL_USER_FLAG)
	acting = outer or actor or user
	frappe.flags[REAL_USER_FLAG] = acting
	if user == SYSTEM_USER:
		try:
			yield acting
		finally:
			frappe.flags[REAL_USER_FLAG] = outer
		return

	sid, data, form_dict = session.sid, session.data, frappe.local.form_dict
	frappe.set_user(SYSTEM_USER)
	try:
		yield acting
	finally:
		# frappe.set_user also resets the sid, session data and form_dict of the request
		frappe.set_user(user)
		session.sid, session.data = sid, data
		frappe.local.form_dict = form_dict
		frappe.flags[REAL_USER_FLAG] = outer


def real_user() -> str:
	"""The user acting in the current Treasury action: the session user, outside `elevated` too."""
	return frappe.flags.get(REAL_USER_FLAG) or frappe.session.user

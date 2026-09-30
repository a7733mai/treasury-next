# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Cheque timing (spec 8 §4.1): Current-dated or Post-dated, informational only (no GL effect).

The stored values are not core msgids on purpose ("Current" is translated by Frappe as "تيار"); en.po shows
"Current-dated" as "Current"."""

import frappe
from frappe import _
from frappe.utils import escape_html, getdate

CURRENT, POST_DATED = "Current-dated", "Post-dated"
TIMINGS = (CURRENT, POST_DATED)


def get_cheque_timing(issue_date, original_due_date) -> str | None:
	"""Current-dated when due on (or before) its issue date, else Post-dated; None without both dates.

	The original due date, not the effective one: a postponement does not change the timing. Both dates are
	locked once the cheque leaves Created, so the timing never changes after that."""
	if not issue_date or not original_due_date:
		return None
	return CURRENT if getdate(original_due_date) <= getdate(issue_date) else POST_DATED


def validate_timing(value: str | None) -> str | None:
	"""A report / dashboard filter value: '' / None → None; a value outside TIMINGS is rejected."""
	if not value:
		return None
	if value not in TIMINGS:
		frappe.throw(_("Invalid Cheque Timing: {0}").format(escape_html(str(value))))
	return value

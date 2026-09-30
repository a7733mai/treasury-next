# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Server side of the Treasury Dashboard page (/app/treasury-dashboard): one read-only endpoint (GET allowed,
like the number-card sources); the data comes from treasury.pdc.overview."""

import frappe
from frappe import _
from frappe.utils import cint

from treasury.pdc.overview import DEFAULT_HORIZON, get_overview
from treasury.pdc.report_utils import as_list
from treasury.pdc.timing import validate_timing


@frappe.whitelist()
def get_dashboard_data(
	company: str | None = None,
	bank_accounts: str | list | None = None,
	horizon: int | str | None = None,
	cheque_timing: str | None = None,
) -> dict:
	"""The dashboard of `company`: `bank_accounts` a list or its JSON (none: every bank), `horizon` 7, 14 or 30
	days, `cheque_timing` Current-dated / Post-dated (none: every cheque; narrows the open-cheque KPIs only).
	Permissions: see treasury.pdc.overview."""
	horizon = DEFAULT_HORIZON if horizon in (None, "") else cint(horizon)
	return get_overview(
		company, parse_bank_accounts(bank_accounts), horizon, cheque_timing=validate_timing(cheque_timing)
	)


def parse_bank_accounts(value) -> list[str]:
	"""The bank account names of the request: a list of strings (or its JSON / a comma list), else a clean
	validation error rather than a failing query."""
	try:
		names = as_list(value)
	except (ValueError, TypeError):
		names = None
	if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
		frappe.throw(_("Invalid Bank Account."))
	return names

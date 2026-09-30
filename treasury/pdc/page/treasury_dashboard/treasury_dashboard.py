# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Server side of the Treasury Dashboard page (/app/treasury-dashboard): one read-only endpoint (GET allowed,
like the number-card sources); the data comes from treasury.pdc.overview."""

import frappe
from frappe.utils import cint

from treasury.pdc.overview import DEFAULT_HORIZON, get_overview
from treasury.pdc.report_utils import as_list


@frappe.whitelist()
def get_dashboard_data(
	company: str | None = None, bank_accounts: str | list | None = None, horizon: int | str | None = None
) -> dict:
	"""The dashboard of `company`: `bank_accounts` a list or its JSON (none: every bank), `horizon` 7, 14 or 30
	days. Permissions: see treasury.pdc.overview."""
	horizon = DEFAULT_HORIZON if horizon in (None, "") else cint(horizon)
	return get_overview(company, as_list(bank_accounts), horizon)

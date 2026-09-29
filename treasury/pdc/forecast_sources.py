"""Extension point of the Bank Position Forecast report.

Other modules (e.g. Bank Facilities, Phase 5) add expected cash flows without touching the report: list a
function in the `treasury_forecast_sources` hook of any app's hooks.py:

	treasury_forecast_sources = ["treasury.bank_facilities.forecast.get_flows"]

Each function receives the report filters (frappe._dict: company, bank_accounts — the names of the bank
accounts in scope —, from_date, to_date, currency_view) and returns a list of dicts:

	date                        expected date of the flow (overdue when before today)
	direction                   "in" or "out"
	bank_account                Bank Account (None: unassigned bank)
	amount                      amount in the company currency
	amount_in_account_currency  amount in the bank account currency (bank currency view)
	reference_doctype, reference_name, party, description   optional, shown in the details view

Flows are read-only forecasts: a source never posts anything.
"""

import frappe

HOOK = "treasury_forecast_sources"


def get_source_methods() -> list[str]:
	return frappe.get_hooks(HOOK) or []


def get_additional_flows(filters) -> list[dict]:
	"""Expected flows of all registered sources, in the order the hooks list them."""
	flows = []
	for method in get_source_methods():
		flows.extend(frappe.get_attr(method)(filters) or [])
	return flows

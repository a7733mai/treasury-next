"""The ONLY module that imports the custody app, and only its public API (`custody.api.v1`, EC-21).

Every import is inside a function, so this module loads without custody; callers check
`treasury.integrations.custody.is_installed()` first. The bridge needs custody API major version 1
(`custody.api.require_api`): on a custody without the public API, or with another major version, every call
raises a clear error instead of reaching into custody internals. No private custody module is imported here.
"""

import frappe
from frappe import _

API_MAJOR = 1


def api():
	"""The custody public API module (v1). Throws when custody is not installed or offers another API major."""
	if "custody" not in frappe.get_installed_apps():
		frappe.throw(_("The Custody app is not installed."))
	try:
		from custody.api import CustodyApiVersionError, require_api
		from custody.api import v1

		require_api(API_MAJOR)
	except (ImportError, CustodyApiVersionError):
		frappe.throw(
			_("This Treasury version needs the Custody app with public API version {0}. Update the Custody app.").format(
				API_MAJOR
			)
		)
	return v1


def api_version() -> str | None:
	"""The installed custody API version ("1.0"), or None without custody / without a public API."""
	if "custody" not in frappe.get_installed_apps():
		return None
	try:
		from custody.api import API_VERSION
	except ImportError:
		return None
	return API_VERSION


def request_totals(name: str, for_update: bool = False):
	"""Every balance of a Custody Request, recomputed from its submitted documents (F5)."""
	return api().get_request_totals(name, for_update=for_update)


def lock_custodian(custodian: str | None):
	"""Row lock on the Custodian: custody locks it before its requests, so Treasury does too (no deadlock)."""
	api().lock_custodians(custodian)


def payment_allowance(req, payment_type: str, totals) -> float:
	"""Most a custodian Payment Entry (Pay / Receive) may move on the request now, from custody's own books
	(F5). Gross: Treasury's own reservations are deducted by `documents.available`."""
	return api().get_request_allowance(req, payment_type, totals)


def resolve_purpose(req, payment_type: str, totals) -> str:
	"""The purpose custody derives for a Pay / Receive Payment Entry of the request (F1)."""
	return api().resolve_purpose(req, payment_type, totals)


def assert_request_payable(req):
	"""Throws unless the request accepts payments (submitted)."""
	api().assert_request_payable(req)


def draft_replenishments(request: str) -> list[str]:
	"""Draft Initial Funding / Replenishment Payment Entries of a float, oldest first (F13). Plain read."""
	return api().list_draft_replenishments(request)


def assert_custodian_usable(custodian: str, winddown: bool):
	"""Money to the custodian (inflow) needs a usable custodian; a return or a reimbursement is a wind-down."""
	return api().assert_custodian_usable(custodian, winddown=winddown)


def replenish_purposes() -> tuple:
	"""The purposes that top a float up (Initial Funding, Replenishment)."""
	return api().replenish_purposes()


def check_invariants(company: str) -> list:
	"""Every mismatch between stored, recomputed and ledger custody balances ([] = clean). Tests only."""
	return api().check_invariants(company)

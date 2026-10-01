"""The ONLY module that imports the custody app (read-only use of its public helpers).

Every import is inside its function, so this module loads without custody; callers check
`treasury.integrations.custody.is_installed()` first. Custody 2.0.0 (spec 9 §0).
"""


def request_totals(name: str, for_update: bool = False):
	"""Every balance of a Custody Request, recomputed from its submitted documents (F5)."""
	from custody.custody.balances import get_request_totals

	return get_request_totals(name, for_update=for_update)


def lock_custodian(custodian: str | None):
	"""Row lock on the Custodian: custody locks it before its requests, so Treasury does too (no deadlock)."""
	from custody.custody.balances import lock_custodians

	lock_custodians(custodian)


def payment_allowance(req, payment_type: str, totals) -> float:
	"""Most a custodian Payment Entry (Pay / Receive) may move on the request now (F5)."""
	from custody.custody.balances import get_payment_allowance

	return get_payment_allowance(req, payment_type, totals)


def resolve_purpose(req, payment_type: str, totals) -> str:
	"""The purpose custody derives for a Pay / Receive Payment Entry of the request (F1)."""
	from custody.custody.payments import resolve_purpose as _resolve_purpose

	return _resolve_purpose(req, payment_type, totals)


def assert_request_payable(req):
	"""Throws unless the request accepts payments (submitted)."""
	from custody.custody.payments import assert_request_payable as _assert_request_payable

	_assert_request_payable(req)


def draft_replenishments(request: str) -> list[str]:
	"""Draft Initial Funding / Replenishment Payment Entries of a float, oldest first (F13). Plain read."""
	from custody.custody.payments import find_draft_replenishment

	return find_draft_replenishment(request, for_update=False)


def assert_custodian_usable(custodian: str, winddown: bool):
	"""Money to the custodian (inflow) needs a usable custodian; a return or a reimbursement is a wind-down."""
	from custody.custody.constants import MODE_INFLOW, MODE_WINDDOWN
	from custody.custody.sync import assert_custodian_usable as _assert_custodian_usable

	return _assert_custodian_usable(custodian, MODE_WINDDOWN if winddown else MODE_INFLOW)


def replenish_purposes() -> tuple:
	"""The purposes that top a float up (Initial Funding, Replenishment)."""
	from custody.custody.constants import REPLENISH_PURPOSES

	return tuple(REPLENISH_PURPOSES)


def check_invariants(company: str) -> list:
	"""Every mismatch between stored, recomputed and ledger custody balances ([] = clean). Tests only."""
	from custody.custody.balances import check_invariants as _check_invariants

	return _check_invariants(company)

"""Custody fixtures for the Treasury ↔ Custody bridge tests (spec 9 §8).

Own helpers on the Treasury test company: custody's test helpers are bound to its own `_Test Custody Co`. Nothing
here imports custody at load time (the test classes skip when custody is not installed); every helper is
idempotent or creates uniquely named records inside the IntegrationTestCase transaction.
"""

import contextlib

import frappe
from frappe.utils import flt, today

from treasury.integrations.custody import REQUEST, api, compat
from treasury.tests.utils import (
	COMPANY,
	GENDER,
	_ensure,
	acc,
	bank_account_name,
	ensure_account,
	ensure_fiscal_year,
	ensure_user,
	unique_cheque_no,
)

APPROVER = "approver@treasury.test"
ACCOUNTANT = "trs-acc@treasury.test"
CUSTODY_ACCOUNT = "Treasury Employee Custody"
EXPENSE_ACCOUNT = "Treasury Custody Expense"


def custody_account() -> str:
	return acc(CUSTODY_ACCOUNT)


@contextlib.contextmanager
def as_user(user: str):
	"""Run a block as another user (custody caches its access per user), then back to Administrator."""
	from custody.custody.access import clear_user_cache

	frappe.set_user(user)
	clear_user_cache()
	try:
		yield
	finally:
		frappe.set_user("Administrator")
		clear_user_cache()


def ensure_custody_setup() -> frappe._dict:
	"""The custody account, the Custody Settings row of the test company and the approver / accountant users."""
	account = ensure_account(CUSTODY_ACCOUNT, "Accounts Receivable", "Receivable")
	expense = ensure_account(EXPENSE_ACCOUNT, "Indirect Expenses")
	cash = frappe.get_cached_value("Company", COMPANY, "default_cash_account") or acc("Cash")
	settings = frappe.get_single("Custody Settings")
	if not any(row.company == COMPANY for row in settings.get("companies")):
		settings.append(
			"companies", {"company": COMPANY, "custody_account": account, "default_cash_account": cash}
		)
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Custody Settings", "Custody Settings")
	ensure_user(APPROVER, ["Custody Approver"])
	ensure_user(ACCOUNTANT, ["Accounts User", "Treasury User"])
	ensure_fiscal_year(COMPANY, today())
	return frappe._dict(custody_account=account, expense_account=expense, cash_account=cash)


def use_custody_settings(testcase, **values):
	"""Change Custody Settings for the current test only (restored by a cleanup)."""
	previous = {key: frappe.db.get_single_value("Custody Settings", key) for key in values}

	def apply(changes):
		frappe.db.set_single_value("Custody Settings", changes)
		frappe.clear_document_cache("Custody Settings", "Custody Settings")

	apply(values)
	testcase.addCleanup(apply, previous)


def make_custodian(user: str | None = None) -> tuple[str, str]:
	"""A ticked Employee of the test company -> (employee, custodian)."""
	_ensure("Gender", GENDER, {"gender": GENDER})
	employee = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": f"_Test Treasury Custodian {frappe.generate_hash(length=6)}",
			"company": COMPANY,
			"gender": GENDER,
			"date_of_birth": "1990-01-01",
			"date_of_joining": "2020-01-01",
			"status": "Active",
			"user_id": user,
			"create_user_permission": 0,
		}
	).insert(ignore_permissions=True)
	employee.is_custodian = 1
	employee.save(ignore_permissions=True)
	custodian = frappe.db.get_value("Custodian", {"employee": employee.name})
	return employee.name, custodian


def make_request(custodian: str, amount: float, custody_type: str = "Temporary", float_amount=None, **values):
	"""An approved Custody Request (temporary: `amount` approved; permanent: a float of `float_amount`)."""
	from frappe.model.workflow import apply_workflow

	data = {
		"doctype": REQUEST,
		"custody_type": custody_type,
		"custodian": custodian,
		"company": COMPANY,
		"posting_date": today(),
		"purpose": "Treasury test custody",
		**values,
	}
	if custody_type == "Temporary":
		data["requested_amount"] = amount
	else:
		data["float_amount"] = float_amount or amount
	doc = frappe.get_doc(data).insert(ignore_permissions=True)
	apply_workflow(doc, "Send for Approval")
	doc.reload()
	with as_user(APPROVER):
		apply_workflow(doc, "Approve")
	doc.reload()
	return doc


def make_settlement(request: str, amount: float):
	"""An approved settlement of one expense row (reviewed by the accountant)."""
	from custody.custody.doctype.custody_request.custody_request import make_settlement as mapper
	from frappe.model.workflow import apply_workflow

	doc = mapper(request)
	doc.append(
		"expenses",
		{"expense_account": acc(EXPENSE_ACCOUNT), "amount": amount, "description": "Treasury test expense"},
	)
	doc.insert(ignore_permissions=True)
	apply_workflow(doc, "Send for Review")
	doc.reload()
	with as_user(ACCOUNTANT):
		apply_workflow(doc, "Approve")
	doc.reload()
	return doc


def mapped_cheque(request: str, direction: str = "Outgoing") -> dict:
	"""The unsaved cheque of the Treasury button, as the client receives it (without the client-only keys)."""
	data = api.make_pdc_cheque(request, direction)
	for key in ("name", "__islocal", "__unsaved", "owner", "creation", "modified", "modified_by"):
		data.pop(key, None)
	return data


def cheque_from_request(
	request: str, direction: str = "Outgoing", amount=None, insert: bool = True, **values
):
	"""A PDC Cheque of the request through the mapper (`amount`: else what the request still accepts)."""
	data = mapped_cheque(request, direction)
	if not data.get("bank_account"):
		data["bank_account"] = bank_account_name("EGP")
	data["cheque_no"] = data.get("cheque_no") or (None if data.get("cheque_book") else unique_cheque_no())
	if amount is not None:
		data["amount"] = amount
	data.update(values)
	doc = frappe.get_doc(data)
	if insert:
		doc.insert()
	return doc


def build_cheque(request, direction: str = "Outgoing", amount: float = 0, **values):
	"""A PDC Cheque of the request built by hand (no mapper), not inserted."""
	req = frappe.get_doc(REQUEST, request) if isinstance(request, str) else request
	return frappe.get_doc(
		{
			"doctype": "PDC Cheque",
			"direction": direction,
			"company": COMPANY,
			"party_type": "Custodian",
			"party": req.custodian,
			"bank_account": bank_account_name("EGP"),
			"cheque_no": unique_cheque_no(),
			"amount": amount,
			"issue_date": today(),
			"original_due_date": today(),
			"source_doctype": REQUEST,
			"source_name": req.name,
			**values,
		}
	)


def custody_payment_entries(request: str, docstatus: int = 1) -> list[str]:
	return frappe.get_all(
		"Payment Entry",
		filters={"party_type": "Custodian", "custody_request": request, "docstatus": docstatus},
		pluck="name",
	)


def assert_request(testcase, name: str, **expected):
	values = frappe.db.get_value(REQUEST, name, list(expected), as_dict=True)
	for field, value in expected.items():
		actual = values.get(field)
		if isinstance(value, float | int) and not isinstance(value, bool):
			testcase.assertAlmostEqual(flt(actual), flt(value), places=2, msg=field)
		else:
			testcase.assertEqual(actual, value, field)


def invariants() -> list:
	return compat.check_invariants(COMPANY)

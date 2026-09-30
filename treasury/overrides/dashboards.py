"""Connections ("Treasury" group) added to the dashboards of ERPNext / hrms doctypes (spec 7c §5.2).

Frappe calls each `override_doctype_dashboards` hook as `fn(data=data)` with the dashboard built from the
doctype's `_dashboard.py` and JSON links, and uses what it returns. These functions only add: an existing key
of `non_standard_fieldnames` / `dynamic_links` or an existing `fieldname` is never overwritten (`setdefault`),
and existing groups and items keep their order.

Counting (`frappe.desk.notifications.get_open_count`): a filter field that is not on the counted doctype is
looked up in its first child table having it, so `reference_name` counts PDC Cheque Reference / Cheque Batch
Item rows and `voucher_no` the status log rows. Internal links are never used through a Dynamic Link (they
would show the value on every doctype listed).
"""

import frappe
from frappe import _

TREASURY = "Treasury"
BANK_FACILITIES = "Bank Facilities"

# invoice / order -> the Treasury documents allocating to it (child reference rows, or the Security Cheque's
# own Dynamic Link)
INVOICE_ITEMS = {
	"PDC Cheque": "reference_name",
	"Cheque Batch": "reference_name",
	"Bank Transfer": "reference_name",
}
ORDER_ITEMS = {**INVOICE_ITEMS, "Security Cheque": "reference_name"}
QUOTATION_ITEMS = {"Security Cheque": "reference_name"}
# voucher -> the Treasury documents whose status log names it
VOUCHER_ITEMS = {
	"PDC Cheque": "voucher_no",
	"Bank Transfer": "voucher_no",
	"Letter of Credit": "voucher_no",
	"Letter of Guarantee": "voucher_no",
	"Cheque Discounting": "voucher_no",
}
JOURNAL_ITEMS = {**VOUCHER_ITEMS, "Facility Drawdown": "journal_entry", "Facility Repayment": "journal_entry"}
PARTY_ITEMS = {
	"PDC Cheque": "party",
	"Cheque Batch": "party",
	"Bank Transfer": "party",
	"Security Cheque": "party",
}
HRMS_ITEMS = INVOICE_ITEMS
BANK_ACCOUNT_ITEMS = {
	"PDC Cheque": "bank_account",
	"Cheque Batch": "bank_account",
	"Cheque Book": "bank_account",
	"Bank Transfer": "bank_account",
	"Security Cheque": "bank_account",
}
BANK_ACCOUNT_FACILITY_ITEMS = {
	"Bank Facility": "default_bank_account",
	"Facility Drawdown": "bank_account",
	"Facility Repayment": "bank_account",
	"Letter of Credit": "bank_account",
	"Letter of Guarantee": "bank_account",
	"Cheque Discounting": "bank_account",
}


def _extend(
	data,
	items: dict[str, str],
	label: str = TREASURY,
	dynamic_links: dict | None = None,
	fieldname: str | None = None,
) -> frappe._dict:
	"""Add `items` ({doctype: link fieldname}) to the group `label` of a dashboard, without changing anything
	already there."""
	data = frappe._dict(data or {})
	for key in ("transactions",):
		if data.get(key) is None:
			data[key] = []
	for key in ("non_standard_fieldnames", "dynamic_links"):
		if data.get(key) is None:
			data[key] = {}
	if fieldname and not data.get("fieldname"):
		data.fieldname = fieldname
	for doctype, link_fieldname in items.items():
		data.non_standard_fieldnames.setdefault(doctype, link_fieldname)
	for key, value in (dynamic_links or {}).items():
		data.dynamic_links.setdefault(key, value)

	group = next((g for g in data.transactions if _(g.get("label") or "") == _(label)), None)
	if group is None:
		group = {"label": _(label), "items": []}
		data.transactions.append(group)
	group_items = group.setdefault("items", [])
	for doctype in items:
		if doctype not in group_items:
			group_items.append(doctype)
	return data


def _party(doctype: str, data, **kwargs) -> frappe._dict:
	items = {**PARTY_ITEMS, **kwargs.pop("extra", {})}
	return _extend(data, items, dynamic_links={"party": [doctype, "party_type"]}, **kwargs)


# ------------------------------------------------------------------ hooks (hooks.py override_doctype_dashboards)


def sales_invoice(data):
	return _extend(data, INVOICE_ITEMS)


def purchase_invoice(data):
	return _extend(data, INVOICE_ITEMS)


def sales_order(data):
	return _extend(data, ORDER_ITEMS)


def purchase_order(data):
	return _extend(data, ORDER_ITEMS)


def quotation(data):
	return _extend(
		data, QUOTATION_ITEMS, dynamic_links={"reference_name": ["Quotation", "reference_doctype"]}
	)


def supplier_quotation(data):
	return _extend(
		data, QUOTATION_ITEMS, dynamic_links={"reference_name": ["Supplier Quotation", "reference_doctype"]}
	)


def payment_entry(data):
	return _extend(data, VOUCHER_ITEMS)


def journal_entry(data):
	return _extend(data, JOURNAL_ITEMS)


def customer(data):
	return _party("Customer", data)


def supplier(data):
	return _party("Supplier", data, extra={"Letter of Credit": "supplier"})


def employee(data):
	return _party("Employee", data, fieldname="employee")


def bank_account(data):
	data = _extend(data, BANK_ACCOUNT_ITEMS)
	return _extend(data, BANK_ACCOUNT_FACILITY_ITEMS, label=BANK_FACILITIES)


def expense_claim(data):
	return _extend(data, HRMS_ITEMS)


def employee_advance(data):
	return _extend(data, HRMS_ITEMS)

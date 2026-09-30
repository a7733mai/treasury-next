"""Invoice / order / Journal Entry references of a Treasury document that pays or receives (PDC Cheque, Bank
Transfer): which reference types a direction and party allow, and the validation of the reference rows
(child DocType PDC Cheque Reference)."""

import erpnext
import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt

from treasury.utils import hrms


def reference_rules(direction: str | None, party_type: str | None) -> dict:
	"""Reference types a document may allocate, and the field holding their party (None: no party check).

	Invoices and orders only for an outgoing payment to a Supplier / incoming payment from a Customer; any
	payment may settle a Journal Entry of its party (checked in the Payment Ledger when it is posted).
	"""
	if (direction, party_type) == ("Outgoing", "Supplier"):
		return {"Purchase Invoice": "supplier", "Purchase Order": "supplier", "Journal Entry": None}
	if (direction, party_type) == ("Incoming", "Customer"):
		return {"Sales Invoice": "customer", "Sales Order": "customer", "Journal Entry": None}
	if (direction, party_type) == ("Outgoing", "Employee"):
		# Expense Claims / Employee Advances only where the Payment Entry class accepts them (hrms)
		return {"Journal Entry": None, **{d: "employee" for d in hrms.supported_reference_doctypes()}}
	return {"Journal Entry": None}


def scheduled_transfer_amounts(reference_doctype: str, names: list[str]) -> dict:
	"""{reference name: amount} allocated by submitted Bank Transfers still Scheduled (not posted yet)."""
	if not names:
		return {}
	ref = frappe.qb.DocType("PDC Cheque Reference")
	transfer = frappe.qb.DocType("Bank Transfer")
	rows = (
		frappe.qb.from_(ref)
		.join(transfer)
		.on(ref.parent == transfer.name)
		.select(ref.reference_name, Sum(ref.allocated_amount))
		.where(
			(ref.parenttype == "Bank Transfer")
			& (ref.reference_doctype == reference_doctype)
			& (ref.reference_name.isin(names))
			& (transfer.docstatus == 1)
			& (transfer.status == "Scheduled")
		)
		.groupby(ref.reference_name)
	).run()
	return {name: flt(amount) for name, amount in rows}


def allocation_precision() -> int:
	return frappe.get_precision("PDC Cheque Reference", "allocated_amount") or 2


def validate_references(doc, direction: str, total_message: str, amount_field: str = "amount"):
	"""Each row: an allowed type, a positive allocation, a submitted document of the company and of the party.

	The total allocation (party account currency) may not exceed `doc.<amount_field>` when the document is in
	the company currency (`total_message` otherwise)."""
	allowed = reference_rules(direction, doc.party_type)
	precision = allocation_precision()
	total = 0
	for row in doc.get("references"):
		if row.reference_doctype not in allowed:
			frappe.throw(
				_("Row #{0}: Reference Type must be one of: {1}.").format(
					row.idx, ", ".join(_(d) for d in allowed)
				)
			)
		if flt(row.allocated_amount) <= 0:
			frappe.throw(_("Row #{0}: Allocated Amount must be greater than zero.").format(row.idx))
		reference = frappe.db.get_value(
			row.reference_doctype, row.reference_name, ["company", "docstatus"], as_dict=True
		)
		if not reference or reference.docstatus != 1 or reference.company != doc.company:
			frappe.throw(
				_("Row #{0}: {1} {2} must be a submitted document of company {3}.").format(
					row.idx, _(row.reference_doctype), frappe.bold(row.reference_name), doc.company
				)
			)
		party_field = allowed[row.reference_doctype]
		reference_party = party_field and frappe.db.get_value(
			row.reference_doctype, row.reference_name, party_field
		)
		if party_field and reference_party != doc.party:
			frappe.throw(
				_("Row #{0}: {1} {2} does not belong to {3}.").format(
					row.idx,
					_(row.reference_doctype),
					frappe.bold(row.reference_name),
					frappe.bold(doc.party),
				)
			)
		total += flt(row.allocated_amount, precision)

	# Allocations are in the party account currency; compare only when that is the document currency.
	company_currency = erpnext.get_company_currency(doc.company)
	if doc.currency == company_currency and flt(total, precision) > flt(doc.get(amount_field), precision):
		frappe.throw(total_message)

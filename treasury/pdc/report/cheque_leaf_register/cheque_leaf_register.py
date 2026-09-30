# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Cheque Leaf Register (spec 7f §3.6): every leaf of the outgoing cheque books with its status.

One header row per book (its totals over all of its leaves), then its leaves (`view` Leaves). Leaf statuses
and attribution: treasury.pdc.cheque_leaves. A leaf used by a cheque the user may not read keeps its status and
usage; its details are blank (`restricted`) and a party filter never matches it.

A number typed by hand on a cheque without a book belongs to the book of the bank account whose range covers
it: a book that is not Closed first, then the newest.
"""

import frappe
from frappe import _

from treasury.pdc import cheque_leaves
from treasury.pdc.cheque_leaves import LEAF_STATUSES, MAX_LEAVES, UNUSED, USAGE_PAYMENT, USAGE_SECURITY
from treasury.pdc.report_utils import (
	CTX,
	as_list,
	column,
	payment_entry_column,
	validate_company,
	validate_date_range,
	validate_party,
)

BOOK = "Cheque Book"
BOOK_STATUSES = ("Active", "Exhausted", "Closed")
VIEW_LEAVES, VIEW_BOOKS = "Leaves", "Books"
BOOK_FIELDS = [
	"name",
	"bank_account",
	"book_no",
	"start_no",
	"end_no",
	"leaf_digits",
	"status",
	"creation",
]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, BOOK)
	validate_date_range(filters.from_date, filters.to_date)
	statuses = as_list(filters.status)
	if invalid := [s for s in statuses if s not in LEAF_STATUSES]:
		frappe.throw(_("Invalid Status: {0}").format(", ".join(invalid)))
	if filters.book_status and filters.book_status not in BOOK_STATUSES:
		frappe.throw(_("Invalid Status: {0}").format(filters.book_status))
	if filters.usage and filters.usage not in (USAGE_PAYMENT, USAGE_SECURITY):
		frappe.throw(_("Invalid Usage: {0}").format(filters.usage))
	if filters.party:
		validate_party(filters.party_type, filters.party)
	view = filters.view or VIEW_LEAVES
	if view not in (VIEW_LEAVES, VIEW_BOOKS):
		frappe.throw(_("Invalid View: {0}").format(view))

	books = get_books(filters, company)
	row_filtered = bool(statuses or filters.usage or filters.party_type or filters.from_date or filters.to_date)
	if view == VIEW_BOOKS and not row_filtered:
		summaries = cheque_leaves.book_summaries(books)
		data = [header_row(book, summaries[book.name]) for book in books]
	else:
		check_size(books)
		data = []
		leaves_by_book = cheque_leaves.get_leaves(books)
		for book in books:
			leaves = leaves_by_book[book.name]
			matching = [row for row in leaves if matches(row, filters, statuses)]
			if row_filtered and not matching:
				continue
			data.append(header_row(book, cheque_leaves.summarize(leaves, book.status)))
			if view == VIEW_LEAVES:
				data += matching
	return get_columns(), data, None, None, get_summary(data)


def get_books(filters: frappe._dict, company: str) -> list[frappe._dict]:
	conditions = {"company": company}
	if filters.bank_account:
		conditions["bank_account"] = filters.bank_account
	if filters.cheque_book:
		conditions["name"] = filters.cheque_book
	if filters.book_status:
		conditions["status"] = filters.book_status
	return frappe.get_list(
		BOOK, filters=conditions, fields=BOOK_FIELDS, order_by="bank_account asc, start_no asc"
	)


def check_size(books: list):
	leaves = sum(cheque_leaves.total_leaves(book) for book in books)
	if leaves > MAX_LEAVES:
		frappe.throw(
			_("The selection has {0} leaves (maximum {1}). Select a Cheque Book or a Bank Account.").format(
				leaves, MAX_LEAVES
			)
		)


def matches(row: frappe._dict, filters: frappe._dict, statuses: list[str]) -> bool:
	if statuses and row.status not in statuses:
		return False
	if filters.usage and row.usage != filters.usage:
		return False
	if filters.party_type or filters.party:
		if row.restricted or row.party_type != filters.party_type:
			return False
		if filters.party and row.party != filters.party:
			return False
	if filters.from_date or filters.to_date:
		if row.status == UNUSED:
			return UNUSED in statuses
		return cheque_leaves.activity_in_period(row, filters.from_date, filters.to_date)
	return True


def header_row(book: frappe._dict, summary: dict) -> frappe._dict:
	return frappe._dict(
		is_group=1,
		indent=0,
		cheque_book=book.name,
		bank_account=book.bank_account,
		book_no=book.book_no,
		leaf_no=f"{cheque_leaves.format_leaf(book.start_no, book.leaf_digits)} - "
		f"{cheque_leaves.format_leaf(book.end_no, book.leaf_digits)}",
		status=book.status,
		total_leaves=summary["total"],
		used_leaves=summary["used"],
		cancelled_leaves=summary["cancelled"],
		void_leaves=summary["void"],
		remaining_leaves=summary["remaining"],
	)


def get_summary(data: list) -> list[dict]:
	headers = [row for row in data if row.get("is_group")]

	def total(fieldname: str) -> int:
		return sum(row[fieldname] for row in headers)

	return [
		{"value": total("total_leaves"), "label": _("Total Leaves"), "datatype": "Int"},
		{"value": total("used_leaves"), "label": _("Used", context=CTX), "datatype": "Int", "indicator": "Blue"},
		{
			"value": total("cancelled_leaves"),
			"label": _("Cancelled Leaves"),
			"datatype": "Int",
			"indicator": "Grey",
		},
		{"value": total("void_leaves"), "label": _("Void Leaves"), "datatype": "Int", "indicator": "Grey"},
		{
			"value": total("remaining_leaves"),
			"label": _("Remaining", context=CTX),
			"datatype": "Int",
			"indicator": "Green",
		},
	]


def get_columns() -> list[dict]:
	return [
		column("cheque_book", _("Cheque Book"), "Link", 150, options=BOOK),
		column("bank_account", _("Bank Account", context="Treasury"), "Link", 170, options="Bank Account"),
		column("book_no", _("Book No"), width=80),
		column("leaf_no", _("Leaf No"), width=150),
		column("status", _("Status", context="Treasury"), width=130),
		column("usage", _("Usage", context="Cheque Leaf"), width=90),
		column(
			"document_type", _("Document Type", context="Cheque Leaf"), "Link", 100, options="DocType", hidden=1
		),
		column("document", _("Document", context="Cheque Leaf"), "Dynamic Link", 160, options="document_type"),
		column("party_type", _("Party Type"), width=90, hidden=1),
		column("party", _("Party", context=CTX), "Dynamic Link", 150, options="party_type"),
		column("party_name", _("Party Name"), width=160),
		column("amount", _("Amount", context="Treasury"), "Currency", 120, options="currency"),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		column("issue_date", _("Issue Date", context="Treasury"), "Date", 100),
		column("handover_date", _("Handover Date"), "Date", 100),
		column("due_date", _("Due Date", context="Treasury"), "Date", 100),
		column("clearance_date", _("Clearance Date", context="Cheque Leaf"), "Date", 100),
		column("expiry_date", _("Expiry Date", context="Treasury"), "Date", 100),
		column("return_date", _("Return Date"), "Date", 100),
		column("pdc_cheque", _("PDC Cheque"), "Link", 150, options="PDC Cheque"),
		column("pdc_status", _("PDC Status"), width=100),
		payment_entry_column(),
		column("print_count", _("Times Printed"), "Int", 80),
		column("cancel_reason_type", _("Reason Type", context="Cheque Leaf"), width=110),
		column("cancel_reason", _("Cancel / Void Reason"), width=180),
		column("cancelled_on", _("Cancelled / Voided On"), "Date", 100),
		column("cancelled_by", _("Cancelled / Voided By"), "Link", 140, options="User"),
		column("total_leaves", _("Total Leaves"), "Int", 80),
		column("used_leaves", _("Used", context=CTX), "Int", 70),
		column("cancelled_leaves", _("Cancelled Leaves"), "Int", 90),
		column("void_leaves", _("Void Leaves"), "Int", 80),
		column("remaining_leaves", _("Remaining", context=CTX), "Int", 90),
	]

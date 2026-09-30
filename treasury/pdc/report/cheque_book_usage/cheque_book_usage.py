# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Cheque Book Usage (PLAN.md §5.6): leaves used, cancelled and remaining per cheque book.

The counts come from the leaf engine of the Cheque Leaf Register (treasury.pdc.cheque_leaves), so both
reports always agree: a leaf is used by an outgoing PDC Cheque in circulation or any outgoing Security
Cheque (cancelling or returning it frees nothing); a cancelled cheque's leaf, a Cancel Leaf row or a cancelled
PDC Cheque without a book, is a cancelled leaf; Void leaves (Void Leaf) are counted apart. A leaf is counted
once. The leaf-by-leaf view is Cheque Leaf Register.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from treasury.pdc.cheque_leaves import book_summaries, format_leaf
from treasury.pdc.report_utils import (
	CTX,
	column,
	payment_entries_by_cheque,
	payment_entry_column,
	validate_company,
)

BOOK_DOCTYPE = "Cheque Book"
STATUSES = ("Active", "Exhausted", "Closed")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, BOOK_DOCTYPE)
	if filters.status and filters.status not in STATUSES:
		frappe.throw(_("Invalid Status: {0}").format(filters.status))

	conditions = {"company": company}
	if filters.bank_account:
		conditions["bank_account"] = filters.bank_account
	if filters.status:
		conditions["status"] = filters.status
	books = frappe.get_list(
		BOOK_DOCTYPE,
		filters=conditions,
		fields=[
			"name",
			"bank_account",
			"book_no",
			"start_no",
			"end_no",
			"next_no",
			"leaf_digits",
			"status",
			"creation",
		],
		order_by="bank_account asc, start_no asc",
	)

	# the counts of the Cheque Leaf Register (one leaf engine): e.g. the leaf of a cancelled cheque with a
	# hand-typed number and no book is a cancelled leaf in both
	summaries = book_summaries(books)
	payment_entries = get_book_payment_entries([b.name for b in books])
	data = [make_row(book, summaries[book.name]) for book in books]
	for row in data:
		row.payment_entry = payment_entries.get(row.cheque_book, "")
	return get_columns(), data


def get_book_payment_entries(books: list[str]) -> dict[str, str]:
	"""{book: comma separated submitted Payment Entries (current cycle) of its cheques, in leaf order}; only the
	cheques the user can read (User Permissions apply)."""
	if not books or not frappe.has_permission("PDC Cheque", "read"):
		return {}
	cheques = frappe.get_list(
		"PDC Cheque",
		filters={"cheque_book": ["in", books]},
		fields=["name", "cheque_book"],
		order_by="cheque_no asc, creation asc",
	)
	by_cheque = payment_entries_by_cheque([c.name for c in cheques])
	result: dict[str, list[str]] = {}
	for cheque in cheques:
		if by_cheque.get(cheque.name):
			result.setdefault(cheque.cheque_book, []).append(by_cheque[cheque.name])
	return {book: ", ".join(entries) for book, entries in result.items()}


def make_row(book, summary: dict) -> frappe._dict:
	"""One book row from its leaf summary (cheque_leaves.book_summaries)."""
	start, end, digits = cint(book.start_no), cint(book.end_no), cint(book.leaf_digits)
	total = summary["total"]
	consumed = summary["used"] + summary["cancelled"] + summary["void"]
	return frappe._dict(
		cheque_book=book.name,
		bank_account=book.bank_account,
		book_no=book.book_no,
		leaf_range=f"{format_leaf(start, digits)} - {format_leaf(end, digits)}",
		total_leaves=total,
		used_leaves=summary["used"],
		cancelled_leaves=summary["cancelled"],
		void_leaves=summary["void"],
		remaining_leaves=summary["remaining"],
		next_no=format_leaf(cint(book.next_no), digits) if book.status == "Active" else None,
		status=book.status,
		used_percent=flt(consumed * 100.0 / total, 2) if total else 0,
	)


def get_columns() -> list[dict]:
	return [
		column("cheque_book", _("Cheque Book"), "Link", 150, options=BOOK_DOCTYPE),
		column("bank_account", _("Bank Account", context="Treasury"), "Link", 180, options="Bank Account"),
		column("book_no", _("Book No"), width=90),
		column("leaf_range", _("Range", context=CTX), width=150),
		column("total_leaves", _("Total Leaves"), "Int", 90),
		column("used_leaves", _("Used", context=CTX), "Int", 80),
		column("cancelled_leaves", _("Cancelled Leaves"), "Int", 110),
		column("void_leaves", _("Void Leaves"), "Int", 90),
		column("remaining_leaves", _("Remaining", context=CTX), "Int", 90),
		column("next_no", _("Next No"), width=100),
		column("status", _("Status", context="Treasury"), width=90),
		column("used_percent", _("% Used"), "Percent", 90),
		payment_entry_column(),
	]

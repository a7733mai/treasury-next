# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Cheque Book Usage (PLAN.md §5.6): leaves used, cancelled and remaining per cheque book.

A leaf is used when an outgoing PDC Cheque of the book's bank account carries its number (the same rule the
cheque book applies when it allocates numbers) and that cheque is not cancelled; a cancelled cheque's leaf
is recorded in the book's cancelled leaves. An outgoing Security Cheque uses its leaf whatever its status
(cancelling or returning it frees nothing). A leaf is counted once: cancelled / void wins over used. Void
leaves (Void Leaf) are counted apart from the cancelled ones. The leaf-by-leaf view is Cheque Leaf Register.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from treasury.pdc.cheque_leaves import get_cancelled_rows
from treasury.pdc.report_utils import (
	CTX,
	OUTGOING,
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
		fields=["name", "bank_account", "book_no", "start_no", "end_no", "next_no", "leaf_digits", "status"],
		order_by="bank_account asc, start_no asc",
	)

	used_by_bank = {bank: get_used_leaves(bank) for bank in {b.bank_account for b in books}}
	cancelled_by_book = get_cancelled_leaves([b.name for b in books])
	void_by_book = get_cancelled_leaves([b.name for b in books], kind="Void")
	payment_entries = get_book_payment_entries([b.name for b in books])
	data = [
		make_row(
			book,
			used_by_bank[book.bank_account],
			cancelled_by_book.get(book.name, set()),
			void_by_book.get(book.name, set()),
		)
		for book in books
	]
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


def get_used_leaves(bank_account: str) -> set[int]:
	"""Leaf numbers written on non-cancelled outgoing PDC cheques or any outgoing security cheque of the bank
	account."""
	numbers = frappe.get_all(
		"PDC Cheque",
		filters={
			"bank_account": bank_account,
			"direction": OUTGOING,
			"operational_status": ["!=", "Cancelled"],
		},
		pluck="cheque_no",
	)
	numbers += frappe.get_all(
		"Security Cheque",
		filters={"bank_account": bank_account, "direction": OUTGOING},
		pluck="cheque_no",
	)
	return {int(n) for n in numbers if str(n or "").strip().isdigit()}


def get_cancelled_leaves(books: list[str], kind: str = "Cancelled") -> dict[str, set[int]]:
	"""{book: leaf numbers of its cancelled leaves rows of `kind` (Cancelled / Void)}."""
	return {
		book: {number for number, row in rows.items() if row.kind == kind}
		for book, rows in get_cancelled_rows(books).items()
	}


def format_leaf(number: int, digits: int) -> str:
	return str(number).zfill(digits) if digits else str(number)


def make_row(
	book, used_leaves: set[int], cancelled_leaves: set[int], void_leaves: set[int] | None = None
) -> frappe._dict:
	start, end, digits = cint(book.start_no), cint(book.end_no), cint(book.leaf_digits)
	total = max(end - start + 1, 0)
	cancelled = {n for n in cancelled_leaves if start <= n <= end}
	void = {n for n in void_leaves or () if start <= n <= end}
	used = {n for n in used_leaves if start <= n <= end} - cancelled - void
	remaining = 0 if book.status == "Closed" else total - len(used) - len(cancelled) - len(void)
	return frappe._dict(
		cheque_book=book.name,
		bank_account=book.bank_account,
		book_no=book.book_no,
		leaf_range=f"{format_leaf(start, digits)} - {format_leaf(end, digits)}",
		total_leaves=total,
		used_leaves=len(used),
		cancelled_leaves=len(cancelled),
		void_leaves=len(void),
		remaining_leaves=remaining,
		next_no=format_leaf(cint(book.next_no), digits) if book.status == "Active" else None,
		status=book.status,
		used_percent=flt((len(used) + len(cancelled) + len(void)) * 100.0 / total, 2) if total else 0,
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

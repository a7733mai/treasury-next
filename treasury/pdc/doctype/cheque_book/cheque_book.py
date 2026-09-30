# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html, getdate, now_datetime, today

from treasury.utils.context import real_user
from treasury.utils.protected_fields import changed_tables

# Leaves are scanned in chunks so a (mis)configured huge range never loads everything at once.
SCAN_CHUNK = 500
# `kind` of a cancelled leaves row: a cheque (or a spoilt leaf) cancelled, or a leaf voided (Void Leaf).
KIND_CANCELLED = "Cancelled"
KIND_VOID = "Void"
VOID_REASON_TYPES = ("Damaged", "Misprinted", "Lost", "Stolen", "Other")
# Reason type of the rows recorded by a cheque's cancellation (record_cancelled_leaf) only.
CHEQUE_CANCELLED = "Cheque Cancelled"
# Leaves voided in one action at most (one row each).
MAX_VOID_RANGE = 100


class ChequeBook(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.pdc.doctype.cheque_book_cancelled_leaf.cheque_book_cancelled_leaf import (
			ChequeBookCancelledLeaf,
		)

		bank_account: DF.Link
		book_no: DF.Data | None
		cancelled_leaves: DF.Table[ChequeBookCancelledLeaf]
		company: DF.Link
		end_no: DF.Int
		leaf_digits: DF.Int
		naming_series: DF.Literal["CHB-.YYYY.-.####"]
		next_no: DF.Int
		start_no: DF.Int
		status: DF.Literal["Active", "Exhausted", "Closed"]
	# end: auto-generated types

	def onload(self):
		if not self.is_new():
			from treasury.pdc import cheque_leaves

			self.set_onload("leaf_summary", cheque_leaves.book_summary(self))

	def validate(self):
		# first: the cancelled / void leaves are an audit trail, written only by the leaf actions
		self.validate_leaf_log()
		self.validate_bank_account()
		self.validate_range()
		self.validate_overlap()
		self.validate_cancelled_leaves()
		if self.is_new() or not cint(self.next_no):
			self.next_no = self.start_no
		self.update_status()

	def validate_bank_account(self):
		bank_account = frappe.db.get_value(
			"Bank Account", self.bank_account, ["company", "is_company_account"], as_dict=True
		)
		if not bank_account or not bank_account.is_company_account or bank_account.company != self.company:
			frappe.throw(
				_("Bank Account {0} must be a company bank account of {1}.").format(
					frappe.bold(self.bank_account), frappe.bold(self.company)
				)
			)

	def validate_range(self):
		if cint(self.start_no) <= 0:
			frappe.throw(_("Start No must be greater than zero."))
		if cint(self.start_no) > cint(self.end_no):
			frappe.throw(_("Start No cannot be greater than End No."))
		if not self.is_new():
			before = self.get_doc_before_save()
			range_changed = before and (before.start_no, before.end_no) != (self.start_no, self.end_no)
			if range_changed and self.has_used_leaves():
				frappe.throw(_("The range of a cheque book cannot be changed after leaves have been used."))

	def validate_overlap(self):
		if self.status == "Closed":
			return
		overlapping = frappe.get_all(
			"Cheque Book",
			filters={
				"bank_account": self.bank_account,
				"status": ["!=", "Closed"],
				"name": ["!=", self.name],
				"start_no": ["<=", self.end_no],
				"end_no": [">=", self.start_no],
			},
			pluck="name",
		)
		if overlapping:
			frappe.throw(
				_("The range {0} - {1} overlaps with Cheque Book {2} of the same bank account.").format(
					self.start_no, self.end_no, frappe.bold(overlapping[0])
				)
			)

	def validate_leaf_log(self):
		"""`read_only` only protects the form: a REST save could edit or delete a row and free its leaf.
		Only the leaf actions (Cancel Leaf, Void Leaf, a cancelled cheque) change the table; a new book may be
		inserted with rows."""
		if self.is_new() or self.flags.treasury_leaf_action:
			return
		before = self.get_doc_before_save()
		if before and changed_tables(self, before, ("cancelled_leaves",), lifecycle_save=False):
			frappe.throw(
				_("Cancelled and void leaves can only be recorded by the Cancel Leaf and Void Leaf actions."),
				title=_("Not Allowed"),
			)

	def validate_cancelled_leaves(self):
		before = None if self.is_new() else self.get_doc_before_save()
		saved = {row.name for row in before.cancelled_leaves} if before else set()
		seen = set()
		for row in self.cancelled_leaves:
			number = self.parse_leaf(row.cheque_no)
			if number in seen:
				frappe.throw(
					_("Row #{0}: Cheque No {1} is cancelled more than once.").format(
						row.idx, escape_html(str(row.cheque_no))
					)
				)
			seen.add(number)
			if row.name and row.name in saved:
				continue  # recorded before: kept exactly as it is
			if not self.flags.treasury_leaf_action:
				self.set_manual_row_provenance(row)
			row.cheque_no = self.format_leaf(number)
			row.date = row.date or today()
			row.kind = row.kind or KIND_CANCELLED
			row.user = real_user()
			row.datetime = now_datetime()

	def set_manual_row_provenance(self, row):
		"""A row typed into a new book (not recorded by a leaf action) is a plain cancelled leaf: its kind and
		source are server-set, so no one can forge a void leaf or a "Cheque Cancelled" row pointing at a cheque
		(e.g. of another company)."""
		if row.reason_type == CHEQUE_CANCELLED:
			frappe.throw(
				_("Row #{0}: the reason type {1} is recorded only when a cheque is cancelled.").format(
					row.idx, _(CHEQUE_CANCELLED, context="Cheque Book Cancelled Leaf")
				),
				title=_("Not Allowed"),
			)
		row.kind = KIND_CANCELLED
		row.source_doctype = None
		row.source_name = None

	def update_status(self):
		if self.status == "Closed":
			return
		self.status = "Active" if self.find_available_leaf(cint(self.next_no)) is not None else "Exhausted"

	# ------------------------------------------------------------------ leaves

	def format_leaf(self, number: int) -> str:
		return str(number).zfill(cint(self.leaf_digits)) if cint(self.leaf_digits) else str(number)

	def parse_leaf(self, cheque_no) -> int:
		"""Leaf number of `cheque_no`, which must be a whole number inside the book range."""
		value = str(cheque_no or "").strip()
		if not value.isdigit():
			# the value may be anything the client sent: escaped in the message
			frappe.throw(
				_("Cheque No {0} is not a valid leaf number.").format(frappe.bold(escape_html(str(cheque_no))))
			)
		number = int(value)
		if not cint(self.start_no) <= number <= cint(self.end_no):
			frappe.throw(
				_("Cheque No {0} is outside the range {1} - {2} of Cheque Book {3}.").format(
					frappe.bold(value), self.start_no, self.end_no, frappe.bold(self.name)
				)
			)
		return number

	def cancelled_numbers(self) -> set[int]:
		return {cint(row.cheque_no) for row in self.cancelled_leaves}

	def used_numbers(self, numbers: list[int], exclude_cancelled_cheques: bool = False) -> set[int]:
		"""Leaves among `numbers` already written on an outgoing PDC Cheque or Security Cheque of this bank
		account. A security cheque keeps its leaf whatever its status (cancelling or returning it frees
		nothing), so `exclude_cancelled_cheques` applies to PDC Cheques only."""
		if not numbers:
			return set()
		candidates = {str(n) for n in numbers} | {self.format_leaf(n) for n in numbers}
		filters = {
			"bank_account": self.bank_account,
			"direction": "Outgoing",
			"cheque_no": ["in", list(candidates)],
		}
		used = frappe.get_all("Security Cheque", filters=filters, pluck="cheque_no")
		if exclude_cancelled_cheques:
			filters["operational_status"] = ["!=", "Cancelled"]
		used += frappe.get_all("PDC Cheque", filters=filters, pluck="cheque_no")
		return {cint(n) for n in used if str(n).strip().isdigit()}

	def has_used_leaves(self) -> bool:
		return bool(
			frappe.db.exists("PDC Cheque", {"cheque_book": self.name})
			or frappe.db.exists("Security Cheque", {"cheque_book": self.name})
		)

	def find_available_leaf(self, start_from: int) -> int | None:
		"""First leaf from `start_from` on that is neither cancelled nor already used."""
		cancelled = self.cancelled_numbers()
		number = max(cint(start_from), cint(self.start_no))
		end = cint(self.end_no)
		while number <= end:
			chunk = list(range(number, min(number + SCAN_CHUNK, end + 1)))
			used = self.used_numbers([n for n in chunk if n not in cancelled])
			for candidate in chunk:
				if candidate not in cancelled and candidate not in used:
					return candidate
			number = chunk[-1] + 1
		return None

	def add_cancelled_leaf(
		self,
		number: int,
		reason: str | None = None,
		date=None,
		kind: str = KIND_CANCELLED,
		reason_type: str | None = None,
		source: tuple[str, str] | None = None,
	):
		"""Append a cancelled / void leaf row; `source`: (doctype, name) of the cheque whose cancellation
		recorded it. The row's user and time are set on save (`validate_cancelled_leaves`)."""
		if number in self.cancelled_numbers():
			return
		self.append(
			"cancelled_leaves",
			{
				"cheque_no": self.format_leaf(number),
				"kind": kind,
				"reason_type": reason_type,
				"reason": reason,
				"date": getdate(date or today()),
				"source_doctype": source[0] if source else None,
				"source_name": source[1] if source else None,
			},
		)

	@frappe.whitelist(methods=["POST"])
	def close(self):
		# the latest saved book, locked: since this one was loaded, a cheque may have taken a leaf (the allocation
		# writes next_no / status), which would make saving this copy fail with a timestamp mismatch
		book = _lock_book(self.name)
		book.check_permission("write")
		if book.status == "Closed":
			frappe.throw(_("Cheque Book {0} is already closed.").format(frappe.bold(book.name)))
		book.status = "Closed"
		book.save()
		self.reload()


def _lock_book(cheque_book: str):
	if not frappe.db.get_value("Cheque Book", cheque_book, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_("Cheque Book"), cheque_book))
	return frappe.get_doc("Cheque Book", cheque_book)


def allocate_next_cheque_no(cheque_book: str) -> str:
	"""Reserve the next free leaf of `cheque_book` and return its (zero-padded) cheque number.

	The book row is locked, so two concurrent allocations never get the same leaf. Cancelled and
	already used leaves are skipped; the book becomes Exhausted once no free leaf is left.
	"""
	book = _lock_book(cheque_book)
	if book.status != "Active":
		frappe.throw(
			_("Cheque Book {0} is {1}; no cheque number can be allocated from it.").format(
				frappe.bold(book.name), _(book.status)
			)
		)

	number = book.find_available_leaf(cint(book.next_no) or cint(book.start_no))
	if number is None:
		frappe.throw(_("Cheque Book {0} has no free leaves left.").format(frappe.bold(book.name)))

	following = book.find_available_leaf(number + 1)
	book.db_set({"next_no": number + 1, "status": "Active" if following is not None else "Exhausted"})
	return book.format_leaf(number)


def record_cancelled_leaf(
	cheque_book: str,
	cheque_no: str,
	reason: str | None = None,
	date=None,
	source: tuple[str, str] | None = None,
):
	"""Record a leaf as cancelled (used when its PDC Cheque is cancelled; permission checked by the caller).
	`source`: (doctype, name) of that cheque."""
	book = _lock_book(cheque_book)
	value = str(cheque_no or "").strip()
	if not value.isdigit() or not cint(book.start_no) <= int(value) <= cint(book.end_no):
		return
	book.add_cancelled_leaf(int(value), reason, date, reason_type=CHEQUE_CANCELLED, source=source)
	book.flags.ignore_permissions = True  # the caller checked permission on the PDC Cheque
	book.flags.treasury_leaf_action = True
	book.save()


@frappe.whitelist(methods=["POST"])
def cancel_leaf(cheque_book: str, cheque_no: str, reason: str | None = None):
	"""Mark an unused leaf as cancelled (spoilt / lost) so it is never allocated."""
	book = _lock_book(cheque_book)
	book.check_permission("write")
	if not reason:
		frappe.throw(_("A reason is required to cancel a leaf."))
	number = book.parse_leaf(cheque_no)
	if number in book.cancelled_numbers():
		frappe.throw(_("Cheque No {0} is already cancelled.").format(frappe.bold(cheque_no)))
	if book.used_numbers([number], exclude_cancelled_cheques=True):
		frappe.throw(
			_(
				"Cheque No {0} is used by a PDC Cheque or a Security Cheque. Cancel that cheque instead."
			).format(frappe.bold(cheque_no))
		)
	book.add_cancelled_leaf(number, reason, reason_type="Spoilt")
	book.flags.treasury_leaf_action = True
	book.save()
	return book.format_leaf(number)


@frappe.whitelist(methods=["POST"])
def void_leaf(
	cheque_book: str,
	from_cheque_no: str,
	to_cheque_no: str | None = None,
	reason_type: str | None = None,
	reason: str | None = None,
) -> list[str]:
	"""Void one unused leaf or a range of them (damaged, misprinted, lost, stolen ...): each gets a `Void` row
	with the reason and the user, so it is never allocated. The book may be in any status (record keeping)."""
	book = _lock_book(cheque_book)
	book.check_permission("write")
	if reason_type not in VOID_REASON_TYPES:
		frappe.throw(_("Select the reason type of the void leaf."))
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required to void a leaf."))
	first = book.parse_leaf(from_cheque_no)
	last = book.parse_leaf(to_cheque_no or from_cheque_no)
	if last < first:
		frappe.throw(_("To Cheque No cannot be before From Cheque No."))
	if last - first + 1 > MAX_VOID_RANGE:
		frappe.throw(_("At most {0} leaves can be voided at once.").format(MAX_VOID_RANGE))

	numbers = list(range(first, last + 1))
	cancelled = book.cancelled_numbers()
	if already := next((n for n in numbers if n in cancelled), None):
		frappe.throw(
			_("Cheque No {0} is already cancelled or void.").format(frappe.bold(book.format_leaf(already)))
		)
	if used := book.used_numbers(numbers, exclude_cancelled_cheques=True):
		frappe.throw(
			_(
				"Cheque No {0} is used by a PDC Cheque or a Security Cheque. Cancel or replace that cheque instead."
			).format(frappe.bold(book.format_leaf(min(used))))
		)

	for number in numbers:
		book.add_cancelled_leaf(number, reason, today(), kind=KIND_VOID, reason_type=reason_type)
	book.flags.treasury_leaf_action = True
	book.save()  # Exhausted if no free leaf remains

	leaves = [book.format_leaf(n) for n in numbers]
	range_text = leaves[0] if len(leaves) == 1 else f"{leaves[0]} - {leaves[-1]}"
	reason_label = _(reason_type, context="Cheque Book Cancelled Leaf")
	book.add_comment("Info", _("Leaves {0} voided ({1}): {2}").format(range_text, reason_label, reason))
	return leaves


def lock_covering_books(bank_account: str | None, cheque_no: str | None) -> list[str]:
	"""Lock the cheque books of `bank_account` whose range covers `cheque_no` (a hand-typed number), in name
	order, and return them: a concurrent Void Leaf / Cancel Leaf (which lock the book) and this cheque's save
	are then serialised, so a leaf never ends up both void and used. Callers lock books before the bank
	account row, as the number allocation does (no deadlock)."""
	value = str(cheque_no or "").strip()
	if not bank_account or not value.isdigit():
		return []
	book = frappe.qb.DocType("Cheque Book")
	return (
		frappe.qb.from_(book)
		.select(book.name)
		.where(
			(book.bank_account == bank_account) & (book.start_no <= int(value)) & (book.end_no >= int(value))
		)
		.orderby(book.name)
		.for_update()
		.run(pluck=True)
	)

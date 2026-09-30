"""Cheque leaf register (spec 7f §3.1): the status of every leaf of outgoing cheque books. Read only.

A leaf is consumed the way the cheque book allocates numbers (`ChequeBook.used_numbers`): by an outgoing PDC
Cheque or Security Cheque of the book's bank account carrying its number, or by a cancelled / void leaves row
of the book. The status of a leaf, first match wins:

1. a cancelled leaves row: `Void` (Void Leaf) or `Cancelled Leaf` (Cancel Leaf, a cancelled cheque);
2. an outgoing Security Cheque: its status (`Security Issued` ...); an encashed one shows the PDC Cheque of
   its encashment (same number, no book);
3. outgoing PDC Cheques: the newest one still in circulation (not Cancelled / Replaced), else the newest;
4. otherwise `Unused`.

Attribution: a document with a `cheque_book` belongs to that book only; without one (a number typed by hand) it
belongs to the book of the same bank account whose range covers the number: a book that is not Closed first,
then the newest (overlapping ranges exist only between a closed book and a newer one).

Statuses are read with `frappe.get_all`: leaf consumption must be truthful for whoever manages the books (the
Cheque Book Usage report counts the same way). Party, amount, dates, document and Payment Entry are shown only
for the documents the user may read (`frappe.get_list`); the other used leaves keep their status and usage
with blank details and `restricted = 1`.

A fixed number of queries per call, whatever the number of leaves or cheques.
"""

import frappe
from frappe.utils import cint, get_datetime, getdate

from treasury.pdc.report_utils import payment_entries_by_cheque

UNUSED, WRITTEN, ISSUED, CLEARED, BOUNCED, REPLACED = (
	"Unused",
	"Written",
	"Issued (PDC)",
	"Cleared",
	"Bounced",
	"Replaced",
)
CANCELLED_LEAF, VOID = "Cancelled Leaf", "Void"
SEC_CREATED, SEC_ISSUED, SEC_RETURNED, SEC_ENCASHED, SEC_LOST, SEC_CANCELLED = (
	"Security Created",
	"Security Issued",
	"Security Returned",
	"Security Encashed",
	"Security Lost",
	"Security Cancelled",
)
LEAF_STATUSES = (
	UNUSED,
	WRITTEN,
	ISSUED,
	CLEARED,
	BOUNCED,
	REPLACED,
	CANCELLED_LEAF,
	VOID,
	SEC_CREATED,
	SEC_ISSUED,
	SEC_RETURNED,
	SEC_ENCASHED,
	SEC_LOST,
	SEC_CANCELLED,
)
PDC_LEAF_STATUS = {
	"Created": WRITTEN,
	"Delivered": ISSUED,
	"Cleared": CLEARED,
	"Bounced": BOUNCED,
	"Replaced": REPLACED,
	"Cancelled": CANCELLED_LEAF,
}
SEC_LEAF_STATUS = {
	"Created": SEC_CREATED,
	"Issued": SEC_ISSUED,
	"Returned": SEC_RETURNED,
	"Encashed": SEC_ENCASHED,
	"Lost": SEC_LOST,
	"Cancelled": SEC_CANCELLED,
}
USAGE_PAYMENT, USAGE_SECURITY = "Payment", "Security"
# Leaves listed at most by one report run / print (the book summary is computed for any size).
MAX_LEAVES = 10000
# Not consumed by a payment or a guarantee.
NOT_USED_STATUSES = (UNUSED, CANCELLED_LEAF, VOID)
# PDC Cheques that released their leaf's circulation (a newer cheque on the leaf wins).
RELEASED_PDC_STATUSES = ("Cancelled", "Replaced")

BOOK = "Cheque Book"
PDC = "PDC Cheque"
SECURITY = "Security Cheque"
OUTGOING = "Outgoing"
KIND_VOID = "Void"

PDC_FIELDS = [
	"name",
	"bank_account",
	"cheque_no",
	"cheque_book",
	"security_cheque",
	"operational_status",
	"party_type",
	"party",
	"party_name",
	"amount",
	"currency",
	"issue_date",
	"handover_date",
	"effective_due_date",
	"actual_clearance_date",
	"print_count",
	"creation",
]
SECURITY_FIELDS = [
	"name",
	"bank_account",
	"cheque_no",
	"cheque_book",
	"status",
	"party_type",
	"party",
	"party_name",
	"amount_type",
	"amount",
	"currency",
	"issue_date",
	"handover_date",
	"expiry_date",
	"return_date",
	"encashment_date",
	"converted_pdc_cheque",
	"creation",
]
CANCELLED_FIELDS = [
	"parent",
	"cheque_no",
	"kind",
	"date",
	"reason_type",
	"reason",
	"user",
	"datetime",
	"source_doctype",
	"source_name",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_leaves(books: list) -> dict[str, list[frappe._dict]]:
	"""{book: [leaf rows in number order]} of `books` (rows with name, bank_account, book_no, start_no,
	end_no, leaf_digits, status, creation). The caller keeps the ranges within MAX_LEAVES."""
	books = [frappe._dict(b) for b in books or ()]
	if not books:
		return {}
	context = _load(books, details=True)
	result = {}
	for book in books:
		result[book.name] = [
			_leaf_row(context, book, number) for number in range(cint(book.start_no), cint(book.end_no) + 1)
		]
	return result


def book_summary(book) -> dict:
	"""{total, used, cancelled, void, remaining} of one book (same data as `get_leaves`, no leaf rows)."""
	book = frappe._dict(
		name=book.name,
		bank_account=book.bank_account,
		start_no=book.start_no,
		end_no=book.end_no,
		leaf_digits=book.leaf_digits,
		status=book.status,
		creation=book.get("creation"),
	)
	return book_summaries([book])[book.name]


def book_summaries(books: list) -> dict[str, dict]:
	"""{book: summary} of `books`, for any number of leaves: only the leaves in use are looked at."""
	books = [frappe._dict(b) for b in books or ()]
	if not books:
		return {}
	context = _load(books, details=False)
	result = {}
	for book in books:
		statuses = [_pick(context, book, number)[0] for number in _touched_numbers(context, book)]
		unused = total_leaves(book) - len([s for s in statuses if s != UNUSED])
		result[book.name] = summarize(
			[{"status": s} for s in statuses], book.status, total=total_leaves(book), unused=unused
		)
	return result


def summarize(rows, book_status: str, total: int | None = None, unused: int | None = None) -> dict:
	"""Counts of leaf rows: `used` = consumed by a payment or a guarantee; `remaining` = the Unused leaves, 0
	once the book is Closed. `total` / `unused`: when `rows` holds only the leaves in use (book_summary)."""
	statuses = [frappe._dict(r).status for r in rows]
	unused = statuses.count(UNUSED) if unused is None else unused
	return {
		"total": len(statuses) if total is None else total,
		"used": len([s for s in statuses if s not in NOT_USED_STATUSES]),
		"cancelled": statuses.count(CANCELLED_LEAF),
		"void": statuses.count(VOID),
		"remaining": 0 if book_status == "Closed" else unused,
	}


def total_leaves(book) -> int:
	return max(cint(book.end_no) - cint(book.start_no) + 1, 0)


def format_leaf(number: int, digits) -> str:
	"""Like `ChequeBook.format_leaf`."""
	return str(number).zfill(cint(digits)) if cint(digits) else str(number)


def get_cancelled_rows(books: list[str]) -> dict[str, dict[int, frappe._dict]]:
	"""{book: {leaf number: cancelled / void leaves row}} (`kind` "Cancelled" when empty)."""
	if not books:
		return {}
	result: dict[str, dict[int, frappe._dict]] = {}
	for row in frappe.get_all(
		"Cheque Book Cancelled Leaf",
		filters={"parenttype": BOOK, "parent": ["in", list(books)]},
		fields=CANCELLED_FIELDS,
		order_by="idx asc",
	):
		value = str(row.cheque_no or "").strip()
		if value.isdigit():
			row.kind = row.kind or "Cancelled"
			result.setdefault(row.parent, {}).setdefault(int(value), row)
	return result


def treasury_cheque_book_leaves(doc) -> dict:
	"""Jinja helper of the "Cheque Book Leaves" print format: {"summary", "leaves", "too_many"}. Nothing for a
	user without print permission on the book (any template may call it)."""
	empty = {"leaves": [], "summary": {}, "too_many": 0}
	if getattr(doc, "doctype", None) != BOOK or not doc.get("name"):
		return empty
	if not frappe.has_permission(BOOK, "print", doc=doc.name):
		return empty
	summary = book_summary(doc)
	if total_leaves(doc) > MAX_LEAVES:
		return {"leaves": [], "summary": summary, "too_many": 1}
	book = frappe._dict(
		name=doc.name,
		bank_account=doc.bank_account,
		book_no=doc.book_no,
		start_no=doc.start_no,
		end_no=doc.end_no,
		leaf_digits=doc.leaf_digits,
		status=doc.status,
		creation=doc.get("creation"),
	)
	return {"leaves": get_leaves([book])[doc.name], "summary": summary, "too_many": 0}


# ---------------------------------------------------------------------------
# Loading and attribution
# ---------------------------------------------------------------------------


def _load(books: list[frappe._dict], details: bool) -> frappe._dict:
	"""Every document of the books' bank accounts that may sit on a leaf, attributed to its book."""
	banks = sorted({b.bank_account for b in books})
	book_names = [b.name for b in books]
	# every book of these banks (not only the listed ones): a number typed by hand belongs to the right book
	all_books = frappe.get_all(
		BOOK,
		filters={"bank_account": ["in", banks]},
		fields=["name", "bank_account", "start_no", "end_no", "status", "creation"],
	)
	filters = {"bank_account": ["in", banks], "direction": OUTGOING}
	pdc_rows = frappe.get_all(PDC, filters=filters, fields=PDC_FIELDS, order_by="creation asc, name asc")
	security_rows = frappe.get_all(
		SECURITY, filters=filters, fields=SECURITY_FIELDS, order_by="creation asc, name asc"
	)
	cancelled = get_cancelled_rows(book_names)

	books_by_bank: dict[str, list] = {}
	for book in all_books:
		books_by_bank.setdefault(book.bank_account, []).append(book)
	listed = set(book_names)
	pdc_by_leaf: dict[str, dict[int, list]] = {}
	security_by_leaf: dict[str, dict[int, list]] = {}
	for rows, target in ((pdc_rows, pdc_by_leaf), (security_rows, security_by_leaf)):
		for row in rows:
			value = str(row.cheque_no or "").strip()
			if not value.isdigit():
				continue
			number = int(value)
			book = row.cheque_book or _covering_book(books_by_bank.get(row.bank_account, ()), number)
			if book in listed:
				target.setdefault(book, {}).setdefault(number, []).append(row)

	context = frappe._dict(
		pdc=pdc_by_leaf,
		security=security_by_leaf,
		cancelled=cancelled,
		pdc_by_name={row.name: row for row in pdc_rows},
		readable_pdc=set(),
		readable_security=set(),
		payment_entries={},
	)
	if details:
		_load_details(context, filters)
	return context


def _covering_book(books, number: int) -> str | None:
	"""The book whose range covers `number`: not Closed first, then the newest."""
	candidates = [b for b in books if cint(b.start_no) <= number <= cint(b.end_no)]
	if not candidates:
		return None
	candidates.sort(key=lambda b: (b.status != "Closed", get_datetime(b.creation)), reverse=True)
	return candidates[0].name


def _load_details(context: frappe._dict, filters: dict):
	"""The documents the user may read, and the Payment Entries of the readable PDC Cheques on the leaves."""
	if frappe.has_permission(PDC, "read"):
		context.readable_pdc = set(frappe.get_list(PDC, filters=filters, pluck="name"))
	if frappe.has_permission(SECURITY, "read"):
		context.readable_security = set(frappe.get_list(SECURITY, filters=filters, pluck="name"))
	on_leaves = {row.name for leaves in context.pdc.values() for rows in leaves.values() for row in rows}
	on_leaves |= {
		row.converted_pdc_cheque
		for leaves in context.security.values()
		for rows in leaves.values()
		for row in rows
		if row.converted_pdc_cheque
	}
	context.payment_entries = payment_entries_by_cheque(sorted(on_leaves & context.readable_pdc))


def _touched_numbers(context: frappe._dict, book) -> list[int]:
	"""The leaf numbers of `book` with a document or a cancelled row (all the others are Unused)."""
	numbers = set(context.cancelled.get(book.name, {}))
	numbers |= set(context.security.get(book.name, {}))
	numbers |= set(context.pdc.get(book.name, {}))
	start, end = cint(book.start_no), cint(book.end_no)
	return sorted(n for n in numbers if start <= n <= end)


# ---------------------------------------------------------------------------
# Status and rows
# ---------------------------------------------------------------------------


def _newest(rows: list) -> frappe._dict | None:
	return max(rows, key=lambda r: (get_datetime(r.creation), r.name)) if rows else None


def _leaf_pdc(rows: list) -> frappe._dict | None:
	"""The PDC Cheque that speaks for a leaf: the newest in circulation, else the newest."""
	live = [r for r in rows if r.operational_status not in RELEASED_PDC_STATUSES]
	return _newest(live or rows)


def _pick(context: frappe._dict, book, number: int) -> tuple:
	"""(status, usage, cancelled row, security cheque, PDC cheque) of a leaf (see the module docstring)."""
	cancelled = context.cancelled.get(book.name, {}).get(number)
	pdc_rows = context.pdc.get(book.name, {}).get(number, [])
	if cancelled:
		status = VOID if cancelled.kind == KIND_VOID else CANCELLED_LEAF
		return status, None, cancelled, None, _leaf_pdc(pdc_rows)
	security = _newest(context.security.get(book.name, {}).get(number, []))
	if security:
		pdc = context.pdc_by_name.get(security.converted_pdc_cheque) if security.converted_pdc_cheque else None
		status = SEC_LEAF_STATUS.get(security.status, security.status)
		return status, USAGE_SECURITY, None, security, pdc
	pdc = _leaf_pdc(pdc_rows)
	if pdc:
		return PDC_LEAF_STATUS.get(pdc.operational_status, pdc.operational_status), USAGE_PAYMENT, None, None, pdc
	return UNUSED, None, None, None, None


def _leaf_row(context: frappe._dict, book, number: int) -> frappe._dict:
	status, usage, cancelled, security, pdc = _pick(context, book, number)
	row = frappe._dict(
		cheque_book=book.name,
		bank_account=book.bank_account,
		book_no=book.get("book_no"),
		leaf_number=number,
		leaf_no=format_leaf(number, book.leaf_digits),
		status=status,
		usage=usage,
		restricted=0,
		indent=1,
	)
	for fieldname in (
		"document_type",
		"document",
		"party_type",
		"party",
		"party_name",
		"amount",
		"currency",
		"issue_date",
		"handover_date",
		"due_date",
		"expiry_date",
		"return_date",
		"clearance_date",
		"payment_entry",
		"pdc_cheque",
		"pdc_status",
		"print_count",
		"cancel_reason_type",
		"cancel_reason",
		"cancelled_on",
		"cancelled_by",
		"activity_date",
	):
		row[fieldname] = None

	if cancelled:
		row.update(
			cancel_reason_type=cancelled.reason_type,
			cancel_reason=cancelled.reason,
			cancelled_on=cancelled.date,
			cancelled_by=cancelled.user,
		)
		if cancelled.source_doctype and cancelled.source_name:
			source = (
				context.pdc_by_name.get(cancelled.source_name) if cancelled.source_doctype == PDC else None
			)
			_set_document(context, row, cancelled.source_doctype, cancelled.source_name, source)
		elif pdc:
			_set_document(context, row, PDC, pdc.name, pdc)
		row.activity_date = row.issue_date or cancelled.date
	elif security:
		_set_document(context, row, SECURITY, security.name, security)
		if pdc and pdc.name in context.readable_pdc:
			row.update(
				pdc_cheque=pdc.name,
				pdc_status=pdc.operational_status,
				clearance_date=pdc.actual_clearance_date,
				payment_entry=context.payment_entries.get(pdc.name, ""),
			)
		row.activity_date = row.issue_date
	elif pdc:
		_set_document(context, row, PDC, pdc.name, pdc)
		row.activity_date = row.issue_date
	return row


def _set_document(context: frappe._dict, row: frappe._dict, doctype: str, name: str, doc):
	"""The document of a leaf with its details, or `restricted` when the user may not read it."""
	readable = context.readable_pdc if doctype == PDC else context.readable_security
	if doctype not in (PDC, SECURITY) or name not in readable or not doc:
		row.restricted = 1
		return
	row.update(
		document_type=doctype,
		document=name,
		party_type=doc.party_type,
		party=doc.party,
		party_name=doc.party_name,
		amount=doc.amount,
		currency=doc.currency,
		issue_date=doc.issue_date,
		handover_date=doc.handover_date,
	)
	if doctype == PDC:
		row.update(
			due_date=doc.effective_due_date,
			clearance_date=doc.actual_clearance_date,
			payment_entry=context.payment_entries.get(name, ""),
			print_count=cint(doc.print_count),
		)
	else:
		row.update(expiry_date=doc.expiry_date, return_date=doc.return_date)


def activity_in_period(row, from_date=None, to_date=None) -> bool:
	"""`from_date` ≤ the leaf's activity date ≤ `to_date` (a leaf without one is outside any period)."""
	if not (from_date or to_date):
		return True
	if not row.activity_date:
		return False
	date = getdate(row.activity_date)
	return (not from_date or date >= getdate(from_date)) and (not to_date or date <= getdate(to_date))

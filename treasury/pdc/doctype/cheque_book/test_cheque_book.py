# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from treasury.pdc.doctype.cheque_book.cheque_book import (
	MAX_VOID_RANGE,
	allocate_next_cheque_no,
	cancel_leaf,
	void_leaf,
)
from treasury.tests.utils import (
	DEFAULT_PDC_SETTINGS,
	PHASE_7A_TEST_RECORD_IGNORES,
	PHASE_7F_TEST_RECORD_IGNORES,
	TreasuryTestMixin,
	ensure_user,
	get_voucher_count,
	make_cheque_book,
	make_pdc_cheque,
	make_security_cheque,
	unique_leaf_start,
)

IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Company",
	"Account",
	"Bank Account",
	"Print Format",
	*PHASE_7A_TEST_RECORD_IGNORES,
	*PHASE_7F_TEST_RECORD_IGNORES,
]
BOOK_READER = "treasury-cheque-book-reader@example.com"


class TestChequeBook(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	def book(self, size: int = 5, **values):
		# books accumulate until the class rolls back: every test gets a random range no book overlaps
		start = unique_leaf_start(size)
		return make_cheque_book(start, start + size - 1, leaf_digits=8, **values)

	def leaf(self, book, offset: int) -> str:
		return str(book.start_no + offset).zfill(8)

	def test_sequential_allocation(self):
		book = self.book()
		self.assertEqual((book.status, book.next_no), ("Active", book.start_no))
		numbers = [make_pdc_cheque(cheque_book=book.name).cheque_no for _ in range(3)]
		self.assertEqual(numbers, [self.leaf(book, i) for i in range(3)])
		book.reload()
		self.assertEqual(book.next_no, book.start_no + 3)
		self.assertEqual(allocate_next_cheque_no(book.name), self.leaf(book, 3))

	def test_cancelled_and_used_leaves_are_skipped(self):
		book = self.book()
		cancel_leaf(book.name, self.leaf(book, 0), "Spoilt")
		make_pdc_cheque(cheque_no=self.leaf(book, 1))  # written by hand, without the book
		self.assertEqual(make_pdc_cheque(cheque_book=book.name).cheque_no, self.leaf(book, 2))

		book.reload()
		self.assertEqual([row.cheque_no for row in book.cancelled_leaves], [self.leaf(book, 0)])
		self.assertRaises(frappe.ValidationError, cancel_leaf, book.name, self.leaf(book, 0), "Again")
		self.assertRaises(frappe.ValidationError, cancel_leaf, book.name, self.leaf(book, 2), "Used")
		self.assertRaises(frappe.ValidationError, cancel_leaf, book.name, str(book.end_no + 1), "Outside")

		# a cancelled leaf cannot be written on a cheque
		self.assertRaises(
			frappe.ValidationError, make_pdc_cheque, cheque_book=book.name, cheque_no=self.leaf(book, 0)
		)

	def test_exhaustion(self):
		book = self.book(size=2)
		make_pdc_cheque(cheque_book=book.name)
		book.reload()
		self.assertEqual(book.status, "Active")
		make_pdc_cheque(cheque_book=book.name)
		book.reload()
		self.assertEqual(book.status, "Exhausted")
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, cheque_book=book.name)
		self.assertRaises(frappe.ValidationError, allocate_next_cheque_no, book.name)

	def test_cancelling_remaining_leaves_exhausts_the_book(self):
		book = self.book(size=2)
		make_pdc_cheque(cheque_book=book.name)
		cancel_leaf(book.name, self.leaf(book, 1), "Lost")
		book.reload()
		self.assertEqual(book.status, "Exhausted")

	def test_cancelled_cheque_records_its_leaf(self):
		book = self.book()
		cheque = make_pdc_cheque(cheque_book=book.name)
		cheque.cancel_cheque(reason="Wrong amount")
		book.reload()
		self.assertIn(cheque.cheque_no, [row.cheque_no for row in book.cancelled_leaves])
		self.assertEqual(make_pdc_cheque(cheque_book=book.name).cheque_no, self.leaf(book, 1))

	def test_range_validation(self):
		start = unique_leaf_start(11)
		self.assertRaises(frappe.ValidationError, make_cheque_book, start + 10, start)
		self.assertRaises(frappe.ValidationError, make_cheque_book, 0, 10)

	def test_overlapping_books_are_rejected(self):
		book = self.book(size=10)
		self.assertRaises(frappe.ValidationError, make_cheque_book, book.start_no + 5, book.end_no + 5)
		self.assertRaises(frappe.ValidationError, make_cheque_book, book.start_no - 5, book.start_no)

		# another bank account may use the same range
		make_cheque_book(book.start_no, book.end_no, currency="USD")

		# a closed book no longer blocks its range
		book.close()
		book.reload()
		self.assertEqual(book.status, "Closed")
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, cheque_book=book.name)
		make_cheque_book(book.start_no, book.end_no)

	def test_book_must_match_cheque_bank_account(self):
		book = self.book()
		self.assertRaises(
			frappe.ValidationError, make_pdc_cheque, cheque_book=book.name, currency="USD", amount=10
		)
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, "Incoming", cheque_book=book.name)

	# ------------------------------------------------------------------ Phase 7f: void leaves, audit trail

	def assert_error(self, message: str, fn, *args, **kwargs):
		with self.assertRaises(frappe.ValidationError) as ctx:
			fn(*args, **kwargs)
		self.assertIn(message, str(ctx.exception))

	def test_void_single_leaf(self):
		book = self.book(size=10)
		result = void_leaf(book.name, self.leaf(book, 0), reason_type="Damaged", reason="Torn")
		self.assertEqual(result, [self.leaf(book, 0)])

		book.reload()
		self.assertEqual(len(book.cancelled_leaves), 1)
		row = book.cancelled_leaves[0]
		self.assertEqual(
			(row.cheque_no, row.kind, row.reason_type, row.reason, row.user),
			(self.leaf(book, 0), "Void", "Damaged", "Torn", "Administrator"),
		)
		self.assertTrue(row.datetime)
		self.assertEqual(make_pdc_cheque(cheque_book=book.name).cheque_no, self.leaf(book, 1))
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Cheque Book", "reference_name": book.name, "comment_type": "Info"},
			pluck="content",
		)
		self.assertEqual(len(comments), 1)
		self.assertIn("Torn", comments[0])

	def test_void_range(self):
		book = self.book(size=10)
		result = void_leaf(
			book.name, self.leaf(book, 2), self.leaf(book, 4), reason_type="Misprinted", reason="Printer jam"
		)
		self.assertEqual(result, [self.leaf(book, i) for i in (2, 3, 4)])
		book.reload()
		self.assertEqual(
			[(r.cheque_no, r.kind) for r in book.cancelled_leaves],
			[(self.leaf(book, i), "Void") for i in (2, 3, 4)],
		)
		numbers = [make_pdc_cheque(cheque_book=book.name).cheque_no for _i in range(3)]
		self.assertEqual(numbers, [self.leaf(book, i) for i in (0, 1, 5)])

	def test_void_errors(self):
		book = self.book(size=200)
		make_pdc_cheque(cheque_book=book.name)  # leaf 0 is used
		cancel_leaf(book.name, self.leaf(book, 1), "Spoilt")
		args = {"reason_type": "Damaged", "reason": "Torn"}
		self.assert_error("is used by a PDC Cheque", void_leaf, book.name, self.leaf(book, 0), **args)
		self.assert_error("already cancelled or void", void_leaf, book.name, self.leaf(book, 1), **args)
		self.assert_error(
			"cannot be before", void_leaf, book.name, self.leaf(book, 5), self.leaf(book, 3), **args
		)
		self.assert_error(
			f"At most {MAX_VOID_RANGE} leaves",
			void_leaf,
			book.name,
			self.leaf(book, 2),
			self.leaf(book, 2 + MAX_VOID_RANGE),
			**args,
		)
		self.assert_error(
			"A reason is required", void_leaf, book.name, self.leaf(book, 2), reason_type="Damaged", reason=" "
		)
		self.assert_error(
			"Select the reason type", void_leaf, book.name, self.leaf(book, 2), reason_type="Spoilt", reason="x"
		)
		self.assert_error("is outside the range", void_leaf, book.name, str(book.end_no + 1), **args)
		# a range reaching a used leaf (a security cheque's leaf included) is refused as a whole
		make_security_cheque("Outgoing", cheque_no=self.leaf(book, 10))
		self.assert_error(
			"is used by a PDC Cheque", void_leaf, book.name, self.leaf(book, 8), self.leaf(book, 12), **args
		)
		book.reload()
		self.assertEqual([r.cheque_no for r in book.cancelled_leaves], [self.leaf(book, 1)])

	def test_void_exhausts_book(self):
		book = self.book(size=2)
		make_pdc_cheque(cheque_book=book.name)
		void_leaf(book.name, self.leaf(book, 1), reason_type="Lost", reason="Lost in transit")
		book.reload()
		self.assertEqual(book.status, "Exhausted")

	def test_void_on_closed_book(self):
		book = self.book(size=3)
		book.close()
		void_leaf(book.name, self.leaf(book, 0), reason_type="Stolen", reason="Police report 12")
		book.reload()
		self.assertEqual((book.status, book.cancelled_leaves[0].kind), ("Closed", "Void"))

	def test_void_requires_write(self):
		book = self.book()
		ensure_user(BOOK_READER, ["Treasury User"])
		with self.set_user(BOOK_READER):
			self.assertRaises(
				frappe.PermissionError,
				void_leaf,
				book.name,
				self.leaf(book, 0),
				reason_type="Damaged",
				reason="Torn",
			)
		book.reload()
		self.assertEqual(book.cancelled_leaves, [])

	def test_cancelled_leaves_protected(self):
		book = self.book()
		cancel_leaf(book.name, self.leaf(book, 0), "Spoilt")
		message = "can only be recorded by the Cancel Leaf and Void Leaf actions"

		doc = frappe.get_doc("Cheque Book", book.name)
		doc.cancelled_leaves[0].reason = "Changed"
		self.assert_error(message, doc.save)

		doc = frappe.get_doc("Cheque Book", book.name)
		doc.set("cancelled_leaves", [])
		self.assert_error(message, doc.save)

		doc = frappe.get_doc("Cheque Book", book.name)
		doc.append("cancelled_leaves", {"cheque_no": self.leaf(book, 1), "reason": "Sneaky"})
		self.assert_error(message, doc.save)

		# other changes still save; the row is kept exactly as recorded
		doc = frappe.get_doc("Cheque Book", book.name)
		doc.book_no = "B-7F"
		doc.save()
		row = frappe.get_doc("Cheque Book", book.name).cancelled_leaves[0]
		self.assertEqual((row.reason, row.kind, row.reason_type), ("Spoilt", "Cancelled", "Spoilt"))

	def test_cancelled_pdc_records_source(self):
		book = self.book()
		cheque = make_pdc_cheque(cheque_book=book.name)
		cheque.cancel_cheque(reason="Wrong amount")
		book.reload()
		row = book.cancelled_leaves[0]
		self.assertEqual(
			(row.cheque_no, row.kind, row.reason_type, row.source_doctype, row.source_name),
			(cheque.cheque_no, "Cancelled", "Cheque Cancelled", "PDC Cheque", cheque.name),
		)
		self.assertEqual(row.user, "Administrator")

	def test_new_book_rows_provenance(self):
		"""Security Minor 9: rows typed into a new book are plain cancelled leaves; their kind, source and user
		are server-set (no forged void leaf or "Cheque Cancelled" row pointing at a cheque)."""
		other = make_pdc_cheque()
		start = unique_leaf_start(5)
		book = make_cheque_book(
			start,
			start + 4,
			leaf_digits=8,
			cancelled_leaves=[
				{
					"cheque_no": str(start),
					"kind": "Void",
					"reason_type": "Damaged",
					"reason": "Torn",
					"source_doctype": "PDC Cheque",
					"source_name": other.name,
					"user": "Guest",
				}
			],
		)
		row = book.cancelled_leaves[0]
		self.assertEqual(
			(row.kind, row.reason_type, row.source_doctype, row.source_name, row.user),
			("Cancelled", "Damaged", None, None, "Administrator"),
		)
		start = unique_leaf_start(5)
		self.assert_error(
			"is recorded only when a cheque is cancelled",
			make_cheque_book,
			start,
			start + 4,
			leaf_digits=8,
			cancelled_leaves=[{"cheque_no": str(start), "reason_type": "Cheque Cancelled", "reason": "x"}],
		)

	def test_hand_typed_number_of_void_leaf(self):
		"""Security Minor 11: a hand-typed number (no book) locks the covering book and may not take a void
		leaf; a leaf taken that way cannot be voided."""
		book = self.book(size=5)
		void_leaf(book.name, self.leaf(book, 1), reason_type="Lost", reason="Lost")
		self.assert_error(
			"is a cancelled leaf of Cheque Book", make_pdc_cheque, cheque_no=self.leaf(book, 1)
		)
		make_pdc_cheque(cheque_no=self.leaf(book, 2))
		self.assert_error(
			"is used by a PDC Cheque",
			void_leaf,
			book.name,
			self.leaf(book, 2),
			reason_type="Lost",
			reason="Lost",
		)

	def test_void_comment_translated(self):
		"""Review Minor 4: the reason type in the book's comment is translated with its context."""
		book = self.book()
		previous = frappe.local.lang
		self.addCleanup(setattr, frappe.local, "lang", previous)
		frappe.local.lang = "ar"
		void_leaf(book.name, self.leaf(book, 0), reason_type="Misprinted", reason="Test")
		expected = frappe._("Misprinted", lang="ar", context="Cheque Book Cancelled Leaf")
		self.assertNotEqual(expected, "Misprinted")
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Cheque Book", "reference_name": book.name, "comment_type": "Info"},
			pluck="content",
		)
		self.assertTrue(any(expected in (c or "") for c in comments), comments)

	def test_no_vouchers(self):
		book = self.book(size=10)
		cheque = make_pdc_cheque(cheque_book=book.name)
		before = get_voucher_count("PDC Cheque", cheque.name)
		void_leaf(book.name, self.leaf(book, 3), reason_type="Other", reason="Test")
		cancel_leaf(book.name, self.leaf(book, 4), "Spoilt")
		for doctype in ("Payment Entry", "Journal Entry"):
			self.assertEqual(frappe.db.count(doctype, {"treasury_source_doctype": "Cheque Book"}), 0)
		self.assertEqual(get_voucher_count("PDC Cheque", cheque.name), before)
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Created", "Not Posted"))

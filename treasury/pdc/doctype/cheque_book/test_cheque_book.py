# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import random

import frappe
from frappe.tests import IntegrationTestCase

from treasury.pdc.doctype.cheque_book.cheque_book import allocate_next_cheque_no, cancel_leaf
from treasury.tests.utils import (
	DEFAULT_PDC_SETTINGS,
	TreasuryTestMixin,
	make_cheque_book,
	make_pdc_cheque,
)

IGNORE_TEST_RECORD_DEPENDENCIES = ["Company", "Account", "Bank Account", "Print Format"]


class TestChequeBook(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# books accumulate until the class rolls back: give every test its own, non-overlapping range
		cls.next_start = random.randint(1_000, 8_000) * 1_000

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	def book(self, size: int = 5, **values):
		start = type(self).next_start
		type(self).next_start += 1_000
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
		start = type(self).next_start
		type(self).next_start += 1_000
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

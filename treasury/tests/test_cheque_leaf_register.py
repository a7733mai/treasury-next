"""Phase 7f: the leaf engine (treasury.pdc.cheque_leaves), Cheque Leaf Register and the Cheque Book Leaves print
(spec 7f §7.2)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate, today

from treasury.pdc.cheque_leaves import MAX_LEAVES
from treasury.pdc.doctype.cheque_book.cheque_book import cancel_leaf, void_leaf
from treasury.pdc.report.cheque_book_usage.cheque_book_usage import execute as cheque_book_usage
from treasury.pdc.report.cheque_leaf_register.cheque_leaf_register import execute as leaf_register
from treasury.pdc.report_utils import payment_entries_by_cheque
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	add_user_permission,
	day,
	ensure_alert_bank,
	ensure_company,
	ensure_supplier,
	ensure_user,
	get_voucher_count,
	leaf_rows,
	make_cheque_book,
	make_pdc_cheque,
	make_security_cheque,
	unique_leaf_start,
)

OTHER_SUPPLIER = "_Test Treasury Leaf Supplier"
RESTRICTED_USER = "treasury-leaf-restricted@example.com"
NO_ACCESS_USER = "treasury-leaf-noaccess@example.com"
OTHER_COMPANY_USER = "treasury-leaf-othercompany@example.com"
BOOK = "Cheque Book"


class TestChequeLeafRegister(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_supplier(OTHER_SUPPLIER)

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)
		previous = frappe.local.lang
		self.addCleanup(setattr, frappe.local, "lang", previous)
		frappe.local.lang = "en"

	# ------------------------------------------------------------------ helpers

	def book(self, size: int = 20, **values):
		# books accumulate until the class rolls back (test_max_leaves has 10 001 leaves): every test gets a
		# random range that no book of the site overlaps
		start = unique_leaf_start(size)
		return make_cheque_book(start, start + size - 1, leaf_digits=8, **values)

	@staticmethod
	def leaf(book, offset: int) -> str:
		return str(book.start_no + offset).zfill(8)

	@staticmethod
	def rows(book, **filters) -> dict:
		return leaf_rows({"cheque_book": book.name, **filters})

	def row(self, book, offset: int):
		return self.rows(book)[book.start_no + offset]

	def header(self, book, **filters):
		_columns, data, _message, _chart, summary = leaf_register(
			{"company": COMPANY, "cheque_book": book.name, **filters}
		)
		return next(r for r in data if r.get("is_group")), summary

	def security(self, book, **values):
		values.setdefault("issue_date", day(-30))
		values.setdefault("expiry_date", day(200))
		return make_security_cheque("Outgoing", amount=500, cheque_book=book.name, **values)

	# ------------------------------------------------------------------ statuses

	def test_unused(self):
		book = self.book()
		row = self.row(book, 0)
		self.assertEqual(
			(row.status, row.usage, row.document, row.leaf_no), ("Unused", None, None, self.leaf(book, 0))
		)
		self.assertEqual(len(self.rows(book)), 20)

	def test_pdc_lifecycle(self):
		book = self.book()
		cheque = make_pdc_cheque(amount=1000, cheque_book=book.name, issue_date=day(-20), due_date=day(-5))
		row = self.row(book, 0)
		self.assertEqual(
			(row.status, row.usage, row.document_type, row.document),
			("Written", "Payment", "PDC Cheque", cheque.name),
		)
		self.assertEqual(
			(row.party_type, row.party, row.amount, row.currency), ("Supplier", SUPPLIER, 1000, "EGP")
		)

		cheque.deliver(handover_date=day(-19))
		row = self.row(book, 0)
		self.assertEqual(row.status, "Issued (PDC)")
		cheque.reload()
		self.assertEqual(row.payment_entry, cheque.party_payment_entry)

		cheque.clear(clearance_date=day(-4))
		cheque.reload()
		row = self.row(book, 0)
		self.assertEqual(row.status, "Cleared")
		dates = (row.issue_date, row.handover_date, row.due_date, row.clearance_date)
		self.assertEqual(tuple(getdate(d) for d in dates), (day(-20), day(-19), day(-5), day(-4)))
		self.assertEqual(row.payment_entry, payment_entries_by_cheque([cheque.name])[cheque.name])
		self.assertIn(cheque.bank_payment_entry, row.payment_entry)

	def test_pdc_bounced(self):
		book = self.book()
		cheque = make_pdc_cheque(cheque_book=book.name, issue_date=day(-20), due_date=day(-5))
		cheque.deliver(handover_date=day(-19))
		cheque.bounce(bounce_date=day(-4), reason="Insufficient funds")
		self.assertEqual(self.row(book, 0).status, "Bounced")

	def test_pdc_replaced(self):
		book = self.book()
		cheque = make_pdc_cheque(cheque_book=book.name, issue_date=day(-20), due_date=day(-5))
		cheque.deliver(handover_date=day(-19))
		result = cheque.replace({"replacement_date": day(-10), "reason": "Torn by the supplier"})
		rows = self.rows(book)
		self.assertEqual(rows[book.start_no].status, "Replaced")
		self.assertEqual(rows[book.start_no + 1].status, "Issued (PDC)")
		self.assertEqual(rows[book.start_no + 1].document, result["replaced_by"])

	def test_pdc_cancelled(self):
		book = self.book()
		cheque = make_pdc_cheque(cheque_book=book.name)
		cheque.cancel_cheque(reason="Wrong payee")
		row = self.row(book, 0)
		self.assertEqual(
			(row.status, row.document, row.cancel_reason_type, row.cancel_reason),
			("Cancelled Leaf", cheque.name, "Cheque Cancelled", "Wrong payee"),
		)
		self.assertEqual(row.cancelled_by, "Administrator")

	def test_cancel_leaf_and_void(self):
		book = self.book()
		cancel_leaf(book.name, self.leaf(book, 5), "Spoilt by ink")
		void_leaf(book.name, self.leaf(book, 6), reason_type="Damaged", reason="Torn")
		rows = self.rows(book)
		cancelled, void = rows[book.start_no + 5], rows[book.start_no + 6]
		self.assertEqual((cancelled.status, cancelled.cancel_reason_type), ("Cancelled Leaf", "Spoilt"))
		self.assertEqual(
			(void.status, void.cancel_reason_type, void.cancel_reason), ("Void", "Damaged", "Torn")
		)
		self.assertEqual(getdate(void.cancelled_on), getdate(today()))
		self.assertIsNone(void.usage)

	def test_manual_number_attributed(self):
		book = self.book()
		cheque = make_pdc_cheque(cheque_no=self.leaf(book, 3))  # typed by hand, no cheque book
		row = self.row(book, 3)
		self.assertEqual((row.status, row.document, row.usage), ("Written", cheque.name, "Payment"))
		# an overlapping newer book of the same bank takes a hand-typed number once the old one is closed
		book.close()
		newer = make_cheque_book(book.start_no, book.end_no, leaf_digits=8)
		self.assertEqual(self.row(newer, 3).document, cheque.name)
		self.assertEqual(self.row(book, 3).status, "Unused")

	def test_security_issue_return(self):
		book = self.book()
		security = self.security(book)
		row = self.row(book, 0)
		self.assertEqual(
			(row.status, row.usage, row.document), ("Security Created", "Security", security.name)
		)
		self.assertEqual(getdate(row.expiry_date), day(200))

		security.issue(handover_date=day(-25))
		self.assertEqual(self.row(book, 0).status, "Security Issued")
		security.return_cheque(return_date=day(-3), person="Mr Y", reason="Contract closed")
		row = self.row(book, 0)
		self.assertEqual(row.status, "Security Returned")
		self.assertEqual(getdate(row.return_date), day(-3))
		self.assertIsNone(row.due_date)

	def test_security_cancelled_lost(self):
		book = self.book()
		cancelled = self.security(book)
		cancelled.cancel_cheque(reason="Not needed")
		lost = self.security(book)
		lost.issue(handover_date=day(-25))
		lost.mark_lost(lost_date=day(-2), reason="Lost by the supplier")
		rows = self.rows(book)
		self.assertEqual(rows[book.start_no].status, "Security Cancelled")
		self.assertEqual(rows[book.start_no + 1].status, "Security Lost")

	def test_security_encashed(self):
		book = self.book()
		security = self.security(book)
		security.issue(handover_date=day(-25))
		result = security.encash(encashment_date=day(-10))
		pdc = frappe.get_doc("PDC Cheque", result["pdc_cheque"])
		pdc.clear(clearance_date=day(-9))
		pdc.reload()

		row = self.row(book, 0)
		self.assertEqual(
			(row.status, row.usage, row.document), ("Security Encashed", "Security", security.name)
		)
		self.assertEqual((row.pdc_cheque, row.pdc_status), (pdc.name, "Cleared"))
		self.assertEqual(getdate(row.clearance_date), day(-9))
		self.assertEqual(row.payment_entry, payment_entries_by_cheque([pdc.name])[pdc.name])
		# the encashment PDC Cheque carries the same number without a book: the leaf is counted once
		header, _summary = self.header(book)
		self.assertEqual(header.used_leaves, 1)

	# ------------------------------------------------------------------ counts

	def test_summary_counts(self):
		book = self.book()
		for _i in range(3):
			make_pdc_cheque(cheque_book=book.name)
		self.security(book)
		cancel_leaf(book.name, self.leaf(book, 4), "Spoilt")
		void_leaf(book.name, self.leaf(book, 5), self.leaf(book, 6), reason_type="Lost", reason="Lost")

		header, summary = self.header(book)
		expected = (20, 4, 1, 2, 13)
		fields = ("total_leaves", "used_leaves", "cancelled_leaves", "void_leaves", "remaining_leaves")
		self.assertEqual(tuple(header[f] for f in fields), expected)
		self.assertEqual(tuple(card["value"] for card in summary), expected)

		_columns, data = cheque_book_usage({"company": COMPANY, "bank_account": book.bank_account})
		usage = next(r for r in data if r.cheque_book == book.name)
		self.assertEqual(
			(usage.used_leaves, usage.cancelled_leaves, usage.void_leaves, usage.remaining_leaves),
			(4, 1, 2, 13),
		)
		self.assertEqual(usage.used_percent, 35.0)

		book.reload()
		book.run_method("onload")
		self.assertEqual(
			book.get_onload().leaf_summary,
			{"total": 20, "used": 4, "cancelled": 1, "void": 2, "remaining": 13},
		)

	def test_closed_book_remaining_zero(self):
		book = self.book(size=5)
		make_pdc_cheque(cheque_book=book.name)
		book.close()
		header, _summary = self.header(book)
		self.assertEqual((header.status, header.remaining_leaves, header.used_leaves), ("Closed", 0, 1))
		self.assertEqual([r.status for r in self.rows(book).values()].count("Unused"), 4)

	def test_filters(self):
		book = self.book(size=10)
		pdc = make_pdc_cheque(cheque_book=book.name, issue_date=day(-30), due_date=day(-1))
		security = self.security(book, party=OTHER_SUPPLIER, issue_date=day(-10))
		void_leaf(book.name, self.leaf(book, 2), reason_type="Other", reason="Test")
		unused = {book.start_no + i for i in range(3, 10)}

		self.assertEqual(set(self.rows(book, status=["Void"])), {book.start_no + 2})
		self.assertEqual([r.document for r in self.rows(book, usage="Security").values()], [security.name])
		rows = self.rows(book, party_type="Supplier", party=SUPPLIER)
		self.assertEqual([r.document for r in rows.values()], [pdc.name])
		rows = self.rows(book, from_date=day(-10), to_date=day(-10))
		self.assertEqual([r.document for r in rows.values()], [security.name])
		rows = self.rows(book, from_date=day(-10), to_date=day(-10), status=["Unused"])
		self.assertEqual(set(rows), unused)
		rows = self.rows(book, from_date=day(-40), to_date=day(-5), status=["Unused", "Written"])
		self.assertEqual(set(rows), unused | {book.start_no})
		# a book without a matching leaf is left out
		filters = {"company": COMPANY, "cheque_book": book.name, "status": ["Bounced"]}
		_columns, data, *_rest = leaf_register(filters)
		self.assertEqual(data, [])
		self.assertRaises(
			frappe.ValidationError, leaf_register, {"company": COMPANY, "status": ["Lost Somewhere"]}
		)

	def test_books_view(self):
		book = self.book(size=10)
		make_pdc_cheque(cheque_book=book.name)
		_columns, data, *_rest = leaf_register({"company": COMPANY, "view": "Books"})
		self.assertTrue(data)
		self.assertTrue(all(row.get("is_group") for row in data))
		header = next(row for row in data if row.cheque_book == book.name)
		self.assertEqual((header.total_leaves, header.used_leaves, header.remaining_leaves), (10, 1, 9))
		self.assertEqual(header.leaf_no, f"{self.leaf(book, 0)} - {self.leaf(book, 9)}")

	def test_one_book_columns_and_chart(self):
		"""Design 7f P4 / P5 / P6: the leaf number first, the book columns only when several books may be
		listed, and the usage chart over all the leaves of the listed books (Leaves view only)."""
		book = self.book(size=10)
		make_pdc_cheque(cheque_book=book.name)
		security = self.security(book)
		void_leaf(book.name, self.leaf(book, 5), reason_type="Other", reason="Test")

		columns, _data, _message, chart, cards = leaf_register({"company": COMPANY, "cheque_book": book.name})
		fieldnames = [c["fieldname"] for c in columns]
		self.assertEqual(fieldnames[0], "leaf_no")
		self.assertFalse({"cheque_book", "bank_account", "book_no"} & set(fieldnames))
		self.assertEqual(chart["type"], "percentage")
		# unused, used – payment, used – security, cancelled, void
		self.assertEqual(chart["data"]["datasets"][0]["values"], [7, 1, 1, 0, 1])
		self.assertEqual(cards[-1]["indicator"], "Green")  # 7 left
		self.assertEqual(
			self.row(book, 1).amount_type,
			frappe.db.get_value("Security Cheque", security.name, "amount_type"),
		)

		# a status filter changes the rows, not the chart of the book
		_columns, _data, _message, filtered, _cards = leaf_register(
			{"company": COMPANY, "cheque_book": book.name, "status": ["Void"]}
		)
		self.assertEqual(filtered["data"]["datasets"][0]["values"], [7, 1, 1, 0, 1])

		columns, *_rest = leaf_register({"company": COMPANY, "bank_account": book.bank_account})
		self.assertEqual(
			{"cheque_book", "bank_account", "book_no"} - {c["fieldname"] for c in columns}, set()
		)
		_columns, _data, _message, chart, _cards = leaf_register(
			{"company": COMPANY, "cheque_book": book.name, "view": "Books"}
		)
		self.assertIsNone(chart)

	def test_remaining_card_indicator(self):
		book = self.book(size=3)
		make_pdc_cheque(cheque_book=book.name)
		_header, cards = self.header(book)
		self.assertEqual((cards[-1]["value"], cards[-1]["indicator"]), (2, "Orange"))
		book.reload()  # the allocation wrote next_no
		book.close()
		_header, cards = self.header(book)
		self.assertEqual((cards[-1]["value"], cards[-1]["indicator"]), (0, "Red"))

	def test_max_leaves(self):
		# a bank account of its own: the book stays until the class rolls back, and a later test listing the
		# shared EGP bank account would otherwise be over the limit
		book = self.book(size=MAX_LEAVES + 1, bank_account=ensure_alert_bank().bank_account)
		with self.assertRaises(frappe.ValidationError) as ctx:
			leaf_register({"company": COMPANY})
		self.assertIn(str(MAX_LEAVES), str(ctx.exception))
		_columns, data, *_rest = leaf_register({"company": COMPANY, "view": "Books"})
		header = next(row for row in data if row.cheque_book == book.name)
		self.assertEqual((header.total_leaves, header.remaining_leaves), (MAX_LEAVES + 1, MAX_LEAVES + 1))

	# ------------------------------------------------------------------ permissions

	def test_restricted_details(self):
		book = self.book(size=5)
		allowed = make_pdc_cheque(cheque_book=book.name)
		hidden = make_pdc_cheque(cheque_book=book.name, party=OTHER_SUPPLIER)
		ensure_user(RESTRICTED_USER, ["Treasury User", "Accounts User"])
		add_user_permission(RESTRICTED_USER, "PDC Cheque", allowed.name)

		with self.set_user(RESTRICTED_USER):
			rows = self.rows(book)
			shown, restricted = rows[book.start_no], rows[book.start_no + 1]
			self.assertEqual((shown.document, shown.restricted), (allowed.name, 0))
			self.assertEqual(
				(restricted.status, restricted.usage, restricted.restricted), ("Written", "Payment", 1)
			)
			self.assertEqual(
				(restricted.party, restricted.amount, restricted.document, restricted.payment_entry),
				(None, None, None, None),
			)
			self.assertEqual(self.rows(book, party_type="Supplier", party=OTHER_SUPPLIER), {})
		self.assertEqual(self.row(book, 1).document, hidden.name)

	def test_restricted_cancelled_and_period(self):
		"""Security Minor 10: an unreadable cancelled cheque's reason and user are hidden. Review Minor 3: a
		restricted leaf keeps its activity date, so a period filter treats it like any other leaf."""
		book = self.book(size=5)
		dates = {"issue_date": day(-10), "due_date": day(-1)}
		hidden = make_pdc_cheque(cheque_book=book.name, party=OTHER_SUPPLIER, **dates)
		hidden.cancel_cheque(reason=f"{OTHER_SUPPLIER} refused it")
		make_pdc_cheque(cheque_book=book.name, party=OTHER_SUPPLIER, **dates)
		allowed = make_pdc_cheque(cheque_book=book.name)
		ensure_user(RESTRICTED_USER, ["Treasury User", "Accounts User"])
		add_user_permission(RESTRICTED_USER, "PDC Cheque", allowed.name)

		with self.set_user(RESTRICTED_USER):
			cancelled = self.rows(book)[book.start_no]
			self.assertEqual(
				(cancelled.status, cancelled.restricted, cancelled.cancel_reason, cancelled.cancelled_by),
				("Cancelled Leaf", 1, None, None),
			)
			self.assertEqual(cancelled.cancel_reason_type, "Cheque Cancelled")
			rows = self.rows(book, from_date=day(-10), to_date=day(-10))
			self.assertEqual(set(rows), {book.start_no, book.start_no + 1})
			self.assertEqual(rows[book.start_no + 1].restricted, 1)
		cancelled = self.row(book, 0)
		self.assertEqual(
			(cancelled.cancel_reason, cancelled.cancelled_by),
			(f"{OTHER_SUPPLIER} refused it", "Administrator"),
		)

	def test_usage_agrees_on_cancelled_cheque_without_book(self):
		"""Review Minor 2: a cancelled cheque with a hand-typed number and no book is a cancelled leaf in the
		register and in Cheque Book Usage alike."""
		book = self.book(size=10)
		cheque = make_pdc_cheque(cheque_no=self.leaf(book, 3))
		cheque.cancel_cheque(reason="Wrong payee")
		self.assertEqual(self.row(book, 3).status, "Cancelled Leaf")
		header, _summary = self.header(book)
		_columns, data = cheque_book_usage({"company": COMPANY, "bank_account": book.bank_account})
		usage = next(r for r in data if r.cheque_book == book.name)
		fields = ("used_leaves", "cancelled_leaves", "void_leaves", "remaining_leaves")
		self.assertEqual(tuple(usage[f] for f in fields), (0, 1, 0, 9))
		self.assertEqual(tuple(usage[f] for f in fields), tuple(header[f] for f in fields))

	def test_company_permission(self):
		ensure_user(NO_ACCESS_USER, [])
		with self.set_user(NO_ACCESS_USER):
			self.assertRaises(frappe.PermissionError, leaf_register, {"company": COMPANY})
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(OTHER_COMPANY_USER, ["Treasury User"])
		add_user_permission(OTHER_COMPANY_USER, "Company", OTHER_COMPANY)
		with self.set_user(OTHER_COMPANY_USER):
			self.assertRaises(frappe.PermissionError, leaf_register, {"company": COMPANY})

	def test_query_count(self):
		bank = ensure_alert_bank()
		for _i in range(3):
			book = self.book(size=50, bank_account=bank.bank_account)
			for _j in range(4):
				make_pdc_cheque(bank_account=bank.bank_account, cheque_book=book.name)
			make_security_cheque("Outgoing", bank_account=bank.bank_account, cheque_book=book.name)
			cancel_leaf(book.name, self.leaf(book, 10), "Spoilt")
			void_leaf(book.name, self.leaf(book, 11), self.leaf(book, 13), reason_type="Other", reason="Test")
		filters = {"company": COMPANY, "bank_account": bank.bank_account}
		leaf_register(filters)  # warm the caches (meta, permissions)
		with self.assertQueryCount(12):
			_columns, data, *_rest = leaf_register(filters)
		self.assertEqual(len(data), 3 + 3 * 50)

	# ------------------------------------------------------------------ print

	def test_book_print_format(self):
		book = self.book(size=10)
		cheque = make_pdc_cheque(cheque_book=book.name)
		void_leaf(book.name, self.leaf(book, 1), reason_type="Damaged", reason="Torn")
		for lang in ("en", "ar"):
			frappe.local.lang = lang
			html = frappe.get_print(BOOK, book.name, print_format="Cheque Book Leaves")
			for offset in range(10):
				self.assertIn(self.leaf(book, offset), html)
			self.assertIn(cheque.name, html)
			self.assertIn('class="cbl-total">10<', html)
			self.assertIn('class="cbl-used">1<', html)
			self.assertIn('class="cbl-void">1<', html)
			self.assertIn('class="cbl-remaining">8<', html)
			self.assertIn('dir="rtl"' if lang == "ar" else 'dir="ltr"', html)
		self.assertEqual(get_voucher_count(BOOK, book.name), 0)

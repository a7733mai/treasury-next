# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Cheque Batch: one voucher for many PDC Cheques (PLAN.md §6, §12)."""

import re
from unittest.mock import patch

import frappe
from frappe.cache_manager import clear_user_cache
from frappe.permissions import update_permission_property
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, formatdate, getdate, nowdate

from treasury.pdc import lifecycle, scheduler
from treasury.pdc.batch import create_cheques
from treasury.pdc.batch_status import compute_batch_status
from treasury.pdc.report.pdc_register import pdc_register
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	assert_gl_entries,
	assert_party_balance,
	bank_account_name,
	day,
	ensure_supplier,
	ensure_user,
	get_ledger_open_amount,
	get_party_outstanding,
	get_voucher_count,
	make_cheque_book,
	make_pdc_cheque,
	make_purchase_invoice,
	unique_cheque_no,
)

# Fixtures are built by treasury.tests.utils; don't let Frappe pull ERPNext's global test records.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Company",
	"Account",
	"Bank Account",
	"Cheque Batch",
	"Cheque Book",
	"Currency",
	"DocType",
	"Payment Entry",
	"Journal Entry",
	"PDC Cheque",
	"User",
	"Print Format",
]

DOCTYPE = "Cheque Batch"
CHEQUE = "PDC Cheque"
TREASURY_USER = "treasury-batch-user@example.com"
NO_CHEQUE_USER = "treasury-batch-nocheque@example.com"
FIFO_SUPPLIER = "_Test Treasury Supplier FIFO"
PERMISSION_SAVEPOINT = "treasury_batch_permission_test"
ARABIC = re.compile(r"[؀-ۿ]")

ISSUE = day(-181)  # 2025-01-01
FIRST_DUE = day(-151)  # 2025-01-31
MONTH_ENDS = [
	getdate(d)
	for d in (
		"2025-01-31",
		"2025-02-28",
		"2025-03-31",
		"2025-04-30",
		"2025-05-31",
		"2025-06-30",
		"2025-07-31",
		"2025-08-31",
		"2025-09-30",
		"2025-10-31",
		"2025-11-30",
		"2025-12-31",
	)
]


class TestChequeBatch(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# books accumulate until the class rolls back: every test gets its own range (distinct from the
		# ranges of the other test classes)
		cls.next_start = 73_000_000
		ensure_supplier(FIFO_SUPPLIER)
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(NO_CHEQUE_USER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	# ------------------------------------------------------------------ helpers

	def book(self, size: int = 20):
		start = type(self).next_start
		type(self).next_start += 1_000
		return make_cheque_book(start, start + size - 1, leaf_digits=8)

	@staticmethod
	def make_batch(direction: str = "Outgoing", rows=None, **values):
		outgoing = direction == "Outgoing"
		doc = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"direction": direction,
				"company": COMPANY,
				"party_type": "Supplier" if outgoing else "Customer",
				"party": SUPPLIER if outgoing else CUSTOMER,
				"bank_account": bank_account_name("EGP"),
				"issue_date": ISSUE,
				"purpose": "Rent" if outgoing else "Installments",
				**values,
			}
		)
		for row in rows or []:
			doc.append("cheques", row)
		return doc

	@staticmethod
	def numbered_rows(count: int, amount=1000, first_due=FIRST_DUE, **values) -> list[dict]:
		return [
			{"due_date": add_days(first_due, 30 * i), "amount": amount, "cheque_no": unique_cheque_no(), **values}
			for i in range(count)
		]

	def rent_batch(self, handover=True):
		"""12 monthly rent cheques from a cheque book: 100,000 split, due on every month end of 2025."""
		book = self.book()
		doc = self.make_batch(
			cheque_book=book.name,
			handover_date=ISSUE if handover else None,
			gen_count=12,
			gen_frequency="Monthly",
			gen_first_due_date=FIRST_DUE,
			gen_month_end=1,
			gen_amount_mode="Split Total",
			gen_total=100000,
		)
		preview = doc.generate_rows()
		doc.insert()
		return doc, book, preview

	@staticmethod
	def cheques(doc) -> list:
		return [frappe.get_doc(CHEQUE, row.pdc_cheque) for row in doc.cheques]

	@staticmethod
	def status(name: str) -> tuple[str, str]:
		return tuple(frappe.db.get_value(CHEQUE, name, ["operational_status", "accounting_status"]))

	@staticmethod
	def batch_status(doc) -> str:
		return frappe.db.get_value(DOCTYPE, doc.name, "status")

	@staticmethod
	def leaves(book, first: int, count: int) -> list[str]:
		return [str(book.start_no + first + i).zfill(8) for i in range(count)]

	# ------------------------------------------------------------------ 12 rent cheques

	def test_rent_batch_on_handover(self):
		egp = self.data.egp
		doc, book, preview = self.rent_batch()

		self.assertTrue(doc.name.startswith("CHB-OUT-"))
		self.assertEqual(doc.posting_mode, "On Handover")
		self.assertEqual(doc.status, "Draft")
		self.assertEqual([getdate(r.due_date) for r in doc.cheques], MONTH_ENDS)
		amounts = [r.amount for r in doc.cheques]
		self.assertEqual(amounts, [8333.33] * 11 + [8333.37])
		self.assertEqual(flt(sum(amounts), 2), 100000)
		self.assertEqual((doc.total_amount, doc.cheque_count), (100000, 12))
		# leaves are only previewed while generating: nothing is allocated before the submit
		self.assertEqual(preview["cheque_numbers"], self.leaves(book, 0, 12))
		self.assertFalse(any(r.cheque_no for r in doc.cheques))
		book.reload()
		self.assertEqual(book.next_no, book.start_no)

		doc.submit()
		doc.reload()

		self.assertEqual(doc.status, "Delivered")
		self.assertEqual([r.cheque_no for r in doc.cheques], self.leaves(book, 0, 12))
		for row, cheque in zip(doc.cheques, self.cheques(doc), strict=True):
			self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Delivered", "Party Posted"))
			self.assertEqual((cheque.cheque_batch, cheque.cheque_batch_row), (doc.name, row.name))
			self.assertEqual((cheque.cheque_no, cheque.cheque_book), (row.cheque_no, book.name))
			self.assertEqual(getdate(cheque.original_due_date), getdate(row.due_date))
			self.assertEqual((getdate(cheque.issue_date), getdate(cheque.handover_date)), (ISSUE, ISSUE))
			assert_gl_entries(
				self,
				"Payment Entry",
				cheque.party_payment_entry,
				[
					(self.data.payable, row.amount, 0, SUPPLIER, ISSUE),
					(egp.pdc_payable, 0, row.amount, None, ISSUE),
				],
			)
		self.assertEqual(sum(get_voucher_count(CHEQUE, r.pdc_cheque) for r in doc.cheques), 12)
		book.reload()
		self.assertEqual(book.next_no, book.start_no + 12)

		# idempotent: running the submit step again creates and posts nothing new
		create_cheques(doc)
		self.assertEqual(frappe.db.count(CHEQUE, {"cheque_batch": doc.name}), 12)
		self.assertEqual(sum(get_voucher_count(CHEQUE, r.pdc_cheque) for r in doc.cheques), 12)

	def test_rent_batch_on_cheque_date(self):
		self.use_company_settings(outgoing_party_posting="On Cheque Date")
		doc, _book, _preview = self.rent_batch()
		self.assertEqual(doc.posting_mode, "On Cheque Date")

		doc.submit()
		doc.reload()

		names = [r.pdc_cheque for r in doc.cheques]
		self.assertEqual(doc.status, "Delivered")
		for name in names:
			self.assertEqual(self.status(name), ("Delivered", "Not Posted"))
			self.assertEqual(get_voucher_count(CHEQUE, name), 0)

		# the scheduler run on the 3rd due date posts exactly the first three cheques, each on its due date
		result = scheduler.post_due_party_postings(as_of=MONTH_ENDS[2])

		self.assertEqual([n for n in result["posted"] if n in names], names[:3])
		for row, cheque in zip(doc.cheques[:3], self.cheques(doc)[:3], strict=True):
			self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Delivered", "Party Posted"))
			assert_gl_entries(
				self,
				"Payment Entry",
				cheque.party_payment_entry,
				[
					(self.data.payable, row.amount, 0, SUPPLIER, row.due_date),
					(self.data.egp.pdc_payable, 0, row.amount, None, row.due_date),
				],
			)
		for name in names[3:]:
			self.assertEqual(self.status(name), ("Delivered", "Not Posted"))
		self.assertEqual(sum(get_voucher_count(CHEQUE, n) for n in names), 3)

	# ------------------------------------------------------------------ generator

	def test_generator_frequencies_and_numbering(self):
		doc = self.make_batch(
			"Incoming",
			gen_count=4,
			gen_frequency="Weekly",
			gen_first_due_date=getdate("2025-02-03"),
			gen_amount_mode="Per Cheque",
			gen_amount=500,
			gen_first_cheque_no="000098",
			gen_drawer_bank="Drawer Bank Y",
		)

		def generate() -> list:
			doc.generate_rows()
			return [str(getdate(r.due_date)) for r in doc.cheques]

		self.assertEqual(generate(), ["2025-02-03", "2025-02-10", "2025-02-17", "2025-02-24"])
		self.assertEqual([r.cheque_no for r in doc.cheques], ["000098", "000099", "000100", "000101"])
		self.assertEqual([r.amount for r in doc.cheques], [500] * 4)
		self.assertEqual({r.drawer_bank for r in doc.cheques}, {"Drawer Bank Y"})
		self.assertEqual(doc.total_amount, 2000)

		doc.gen_frequency, doc.gen_interval_days = "Custom Days", 10
		self.assertEqual(generate(), ["2025-02-03", "2025-02-13", "2025-02-23", "2025-03-05"])
		doc.gen_interval_days = 0
		self.assertRaises(frappe.ValidationError, doc.generate_rows)

		# month-end alignment
		doc.gen_count, doc.gen_frequency = 3, "Monthly"
		doc.gen_first_due_date, doc.gen_month_end = getdate("2025-01-31"), 0
		self.assertEqual(generate(), ["2025-01-31", "2025-02-28", "2025-03-31"])  # never drifts to the 28th
		doc.gen_first_due_date = getdate("2025-04-30")
		self.assertEqual(generate(), ["2025-04-30", "2025-05-30", "2025-06-30"])
		doc.gen_month_end = 1
		self.assertEqual(generate(), ["2025-04-30", "2025-05-31", "2025-06-30"])
		doc.gen_frequency, doc.gen_first_due_date = "Quarterly", getdate("2025-01-31")
		self.assertEqual(generate(), ["2025-01-31", "2025-04-30", "2025-07-31"])
		doc.gen_frequency = "Semi-Annually"
		self.assertEqual(generate(), ["2025-01-31", "2025-07-31", "2026-01-31"])

		# numbering keeps the prefix; the zero padding grows only when needed
		doc.gen_first_cheque_no = "CHQ-0999"
		generate()
		self.assertEqual([r.cheque_no for r in doc.cheques], ["CHQ-0999", "CHQ-1000", "CHQ-1001"])
		doc.gen_first_cheque_no = "ABC"
		self.assertRaises(frappe.ValidationError, doc.generate_rows)
		doc.gen_first_cheque_no = ""
		self.assertRaises(frappe.ValidationError, doc.generate_rows)
		doc.gen_first_cheque_no = "1"

		# split total: the last cheque absorbs the rounding, the sum is exact
		doc.gen_amount_mode, doc.gen_total = "Split Total", 100
		generate()
		self.assertEqual([r.amount for r in doc.cheques], [33.33, 33.33, 33.34])
		self.assertEqual(doc.total_amount, 100)
		doc.gen_total = 0.02
		self.assertRaises(frappe.ValidationError, doc.generate_rows)
		doc.gen_count = 0
		self.assertRaises(frappe.ValidationError, doc.generate_rows)

	# ------------------------------------------------------------------ allocation

	def test_fifo_allocation_across_invoices(self):
		first = make_purchase_invoice(1000, posting_date=day(-170), supplier=FIFO_SUPPLIER)
		second = make_purchase_invoice(1500, posting_date=day(-165), supplier=FIFO_SUPPLIER)
		third = make_purchase_invoice(700, posting_date=day(-160), supplier=FIFO_SUPPLIER)
		# rows entered latest due first: FIFO follows the due dates, not the row order
		rows = list(reversed(self.numbered_rows(4, amount=1000, first_due=day(-140))))
		doc = self.make_batch(
			party=FIFO_SUPPLIER,
			issue_date=day(-150),
			handover_date=day(-150),
			allocation_method="FIFO Invoices",
			rows=rows,
		)

		summary = doc.allocate_fifo()

		self.assertEqual((summary["allocated"], summary["unallocated"]), (3200, 800))
		self.assertEqual(
			[(d["reference_name"], d["allocated"]) for d in summary["invoices"]],
			[(first.name, 1000), (second.name, 1500), (third.name, 700)],
		)
		by_due = sorted(doc.cheques, key=lambda r: getdate(r.due_date))
		self.assertEqual(
			[(r.reference_doctype, r.reference_name, r.allocated_amount) for r in by_due],
			[
				("Purchase Invoice", first.name, 1000),
				("Purchase Invoice", second.name, 1000),
				("Purchase Invoice", second.name, 500),
				("Purchase Invoice", third.name, 700),
			],
		)

		doc.insert()
		doc.submit()
		doc.reload()

		for invoice in (first, second, third):
			self.assertEqual(get_party_outstanding("Purchase Invoice", invoice.name), 0)
		# 4,000 paid for 3,200 invoiced: 800 is an advance (balance = debit - credit)
		assert_party_balance(self, "Supplier", FIFO_SUPPLIER, day(0), 800)
		by_due = sorted(doc.cheques, key=lambda r: getdate(r.due_date))
		partial = frappe.db.get_value(CHEQUE, by_due[2].pdc_cheque, "party_payment_entry")
		self.assertEqual(abs(get_ledger_open_amount("Payment Entry", partial)), 500)
		full = frappe.db.get_value(CHEQUE, by_due[0].pdc_cheque, "party_payment_entry")
		self.assertEqual(get_ledger_open_amount("Payment Entry", full), 0)

		# nothing left to allocate for a new batch of the same supplier
		other = self.make_batch(party=FIFO_SUPPLIER, rows=self.numbered_rows(1))
		self.assertEqual(other.allocate_fifo()["allocated"], 0)

	def test_over_allocation_is_rejected(self):
		pi = make_purchase_invoice(1000, posting_date=day(-170))

		def allocated_batch(*allocations, method="Manual"):
			rows = self.numbered_rows(len(allocations), amount=600)
			for row, allocated in zip(rows, allocations, strict=True):
				row.update(reference_doctype="Purchase Invoice", reference_name=pi.name, allocated_amount=allocated)
			return self.make_batch(allocation_method=method, rows=rows)

		# two rows together allocating more than the invoice outstanding
		self.assertRaises(frappe.ValidationError, allocated_batch(600, 600).insert)
		# a row allocating more than its own amount
		self.assertRaises(frappe.ValidationError, allocated_batch(700).insert)
		# Advance means no allocation
		self.assertRaises(frappe.ValidationError, allocated_batch(100, method="Advance (no allocation)").insert)
		# within the outstanding
		allocated_batch(600, 400).insert()

		# a not yet posted cheque already allocating 300 leaves 700 for the batch
		make_pdc_cheque(amount=300, invoice=pi, issue_date=day(-170), due_date=day(-100))
		self.assertRaises(frappe.ValidationError, allocated_batch(600, 200).insert)
		allocated_batch(600, 100).insert()

	# ------------------------------------------------------------------ validation

	def test_row_validations(self):
		self.assertRaises(frappe.ValidationError, self.make_batch().insert)  # no rows

		rows = self.numbered_rows(2)
		rows[1]["cheque_no"] = rows[0]["cheque_no"]
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.make_batch(rows=rows).insert()
		self.assertIn("Row #2", str(ctx.exception))

		rows = self.numbered_rows(1, first_due=add_days(ISSUE, -1))
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.make_batch(rows=rows).insert()
		self.assertIn("Row #1", str(ctx.exception))

		self.assertRaises(frappe.ValidationError, self.make_batch(rows=self.numbered_rows(1, amount=0)).insert)
		self.assertRaises(
			frappe.ValidationError,
			self.make_batch(rows=self.numbered_rows(1), handover_date=add_days(ISSUE, -1)).insert,
		)
		self.assertRaises(
			frappe.ValidationError, self.make_batch(rows=self.numbered_rows(1), bank_account=None).insert
		)
		self.assertRaises(
			frappe.ValidationError,
			self.make_batch(rows=self.numbered_rows(1), party_type="Employee").insert,
		)
		# not enough free leaves in the book for the rows without a number
		book = self.book(size=2)
		rows = [{"due_date": FIRST_DUE, "amount": 100} for _ in range(3)]
		self.assertRaises(frappe.ValidationError, self.make_batch(rows=rows, cheque_book=book.name).insert)

	# ------------------------------------------------------------------ incoming

	def test_incoming_batch_receive_deposit_and_status(self):
		egp = self.data.egp
		doc = self.make_batch(
			"Incoming",
			issue_date=day(-100),
			handover_date=day(-100),
			gen_count=3,
			gen_frequency="Monthly",
			gen_first_due_date=day(-60),
			gen_amount_mode="Per Cheque",
			gen_amount=2000,
			gen_first_cheque_no="IN-0001",
			gen_drawer_bank="Drawer Bank Y",
		)
		doc.generate_rows()
		doc.insert()
		self.assertTrue(doc.name.startswith("CHB-IN-"))
		self.assertEqual(doc.posting_mode, "On Receipt")

		doc.submit()
		doc.reload()

		self.assertEqual(doc.status, "Received")
		cheques = self.cheques(doc)
		self.assertEqual([c.cheque_no for c in cheques], ["IN-0001", "IN-0002", "IN-0003"])
		for cheque in cheques:
			self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Received", "Party Posted"))
			self.assertEqual(cheque.drawer_bank, "Drawer Bank Y")
			assert_gl_entries(
				self,
				"Payment Entry",
				cheque.party_payment_entry,
				[
					(egp.pdc_receivable, 2000, 0, None, day(-100)),
					(self.data.receivable, 0, 2000, CUSTOMER, day(-100)),
				],
			)

		first = cheques[0]
		first.deposit(deposit_date=day(-61))
		first.reload()
		self.assertEqual((first.operational_status, first.accounting_status), ("Deposited", "Under Collection Posted"))
		assert_gl_entries(
			self,
			"Journal Entry",
			first.under_collection_journal,
			[(egp.under_collection, 2000, 0, None, day(-61)), (egp.pdc_receivable, 0, 2000, None, day(-61))],
		)
		self.assertEqual(self.batch_status(doc), "Received")

		first.clear(clearance_date=day(-59))
		self.assertEqual(self.batch_status(doc), "Partially Cleared")
		for cheque in cheques[1:]:
			cheque.clear(clearance_date=day(-30))
		self.assertEqual(self.batch_status(doc), "Cleared")

	# ------------------------------------------------------------------ atomic submit

	def test_submit_failure_rolls_back_everything(self):
		book = self.book()
		rows = [{"due_date": add_days(FIRST_DUE, 30 * i), "amount": 1000} for i in range(8)]
		doc = self.make_batch(cheque_book=book.name, handover_date=ISSUE, rows=rows)
		doc.insert()
		vouchers_before = frappe.db.count("Payment Entry", {"treasury_source_doctype": CHEQUE})
		cheques_before = frappe.db.count(CHEQUE)
		real_deliver = lifecycle.deliver
		calls = []

		def deliver_but_fail_on_row_7(name, *args, **kwargs):
			calls.append(name)
			if len(calls) == 7:
				frappe.throw("Simulated failure")
			return real_deliver(name, *args, **kwargs)

		with patch.object(lifecycle, "deliver", side_effect=deliver_but_fail_on_row_7):
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.submit()

		self.assertEqual(len(calls), 7)
		self.assertIn("Row #7", str(ctx.exception))
		self.assertIn("Simulated failure", str(ctx.exception))
		# nothing is left: no cheque, no voucher, no leaf, the batch is still a draft
		self.assertEqual(tuple(frappe.db.get_value(DOCTYPE, doc.name, ["docstatus", "status"])), (0, "Draft"))
		self.assertEqual(frappe.db.count(CHEQUE, {"cheque_batch": doc.name}), 0)
		self.assertEqual(frappe.db.count(CHEQUE), cheques_before)
		self.assertEqual(frappe.db.count("Payment Entry", {"treasury_source_doctype": CHEQUE}), vouchers_before)
		self.assertFalse(
			any(frappe.get_all("Cheque Batch Item", filters={"parent": doc.name}, pluck="pdc_cheque"))
		)
		book.reload()
		self.assertEqual((book.next_no, len(book.cancelled_leaves)), (book.start_no, 0))

		# once the problem is gone the batch submits, with the same leaves
		doc = frappe.get_doc(DOCTYPE, doc.name)
		doc.submit()
		doc.reload()
		self.assertEqual([r.cheque_no for r in doc.cheques], self.leaves(book, 0, 8))
		self.assertEqual(doc.status, "Delivered")

	# ------------------------------------------------------------------ cancel

	def test_cancel_when_no_cheque_is_in_use(self):
		book = self.book()
		rows = [{"due_date": add_days(FIRST_DUE, 30 * i), "amount": 1000} for i in range(3)]
		doc = self.make_batch(cheque_book=book.name, rows=rows)
		doc.insert()
		doc.submit()
		doc.reload()
		self.assertEqual(doc.status, "Submitted")
		names = [r.pdc_cheque for r in doc.cheques]
		for name in names:
			self.assertEqual(self.status(name), ("Created", "Not Posted"))

		doc.cancel()

		for name in names:
			self.assertEqual(self.status(name), ("Cancelled", "Not Posted"))
			self.assertEqual(get_voucher_count(CHEQUE, name), 0)
		self.assertEqual(self.batch_status(doc), "Cancelled")
		book.reload()
		self.assertEqual([r.cheque_no for r in book.cancelled_leaves], self.leaves(book, 0, 3))

		# the amendment does not reuse the cancelled leaves (copied as the form's Amend does it)
		amended = frappe.copy_doc(doc, ignore_no_copy=False)
		amended.docstatus = 0
		amended.amended_from = doc.name
		amended.insert()
		self.assertFalse(any(r.cheque_no for r in amended.cheques))
		amended.submit()
		amended.reload()
		self.assertEqual([r.cheque_no for r in amended.cheques], self.leaves(book, 3, 3))

	def test_cancel_is_blocked_while_cheques_are_in_use(self):
		doc = self.make_batch(handover_date=ISSUE, rows=self.numbered_rows(2))
		doc.insert()
		doc.submit()
		doc.reload()

		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.cancel()

		for row in doc.cheques:
			self.assertIn(row.pdc_cheque, str(ctx.exception))
			self.assertEqual(self.status(row.pdc_cheque), ("Delivered", "Party Posted"))
		self.assertEqual(frappe.db.get_value(DOCTYPE, doc.name, "docstatus"), 1)

	# ------------------------------------------------------------------ bulk actions

	def test_deliver_all_isolates_failures(self):
		doc = self.make_batch(rows=self.numbered_rows(3))
		doc.insert()
		doc.submit()
		doc.reload()
		names = [r.pdc_cheque for r in doc.cheques]
		real_post_party = lifecycle._post_party

		def post_then_fail(cheque, posting_date):
			real_post_party(cheque, posting_date)  # the Payment Entry exists when it fails
			if cheque.name == names[1]:
				frappe.throw("Simulated posting failure")

		with patch.object(lifecycle, "_post_party", side_effect=post_then_fail):
			result = doc.deliver_all(handover_date=ISSUE)

		self.assertEqual(result["succeeded"], [names[0], names[2]])
		self.assertEqual([f["cheque"] for f in result["failed"]], [names[1]])
		self.assertIn("Simulated posting failure", result["failed"][0]["error"])
		self.assertEqual(self.status(names[1]), ("Created", "Not Posted"))
		self.assertEqual(get_voucher_count(CHEQUE, names[1]), 0)
		for name in (names[0], names[2]):
			self.assertEqual(self.status(name), ("Delivered", "Party Posted"))
		self.assertEqual(self.batch_status(doc), "Partially Delivered")

		# a second run only acts on the cheque that is still Created
		result = doc.deliver_all(handover_date=ISSUE)
		self.assertEqual(result["succeeded"], [names[1]])
		self.assertEqual(result["skipped"], [names[0], names[2]])
		self.assertEqual(sum(get_voucher_count(CHEQUE, n) for n in names), 3)
		self.assertEqual(self.batch_status(doc), "Delivered")

		self.assertRaises(frappe.ValidationError, doc.receive_all, ISSUE)

	def test_print_all_lists_the_cheques(self):
		doc = self.make_batch(rows=self.numbered_rows(2))
		doc.insert()
		doc.submit()
		doc.reload()
		info = doc.print_all()
		self.assertEqual(info["names"], [r.pdc_cheque for r in doc.cheques])
		self.assertEqual((info["doctype"], info["print_format"]), (CHEQUE, "PDC Cheque Print"))

	# ------------------------------------------------------------------ protection, replacement, status

	def test_batch_link_is_protected_on_the_cheque(self):
		doc = self.make_batch(rows=self.numbered_rows(1))
		doc.insert()
		doc.submit()
		doc.reload()

		cheque = frappe.get_doc(CHEQUE, doc.cheques[0].pdc_cheque)
		cheque.cheque_batch = None
		self.assertRaises(frappe.ValidationError, cheque.save)
		cheque.reload()
		cheque.cheque_batch_row = "another-row"
		self.assertRaises(frappe.ValidationError, cheque.save)

		# a client cannot attach a cheque to a batch, neither on insert nor later
		other = make_pdc_cheque(cheque_batch=doc.name, cheque_batch_row=doc.cheques[0].name)
		self.assertIsNone(frappe.db.get_value(CHEQUE, other.name, "cheque_batch"))
		other.reload()
		other.cheque_batch = doc.name
		self.assertRaises(frappe.ValidationError, other.save)

	def test_replacement_stays_in_the_batch(self):
		doc = self.make_batch(handover_date=ISSUE, rows=self.numbered_rows(1))
		doc.insert()
		doc.submit()
		doc.reload()

		old = frappe.get_doc(CHEQUE, doc.cheques[0].pdc_cheque)
		result = old.replace(
			new_cheque_values={"cheque_no": unique_cheque_no(), "replacement_date": day(-150), "reason": "Torn"}
		)

		new = frappe.get_doc(CHEQUE, result["replaced_by"])
		self.assertEqual((new.cheque_batch, new.cheque_batch_row), (doc.name, doc.cheques[0].name))
		self.assertEqual(new.operational_status, "Delivered")
		self.assertEqual(self.batch_status(doc), "Delivered")

	def test_compute_batch_status(self):
		cases = [
			((0, "Outgoing", []), "Draft"),
			((2, "Outgoing", ["Cancelled"]), "Cancelled"),
			((1, "Outgoing", ["Created", "Created"]), "Submitted"),
			((1, "Outgoing", ["Delivered", "Created"]), "Partially Delivered"),
			((1, "Outgoing", ["Delivered", "Bounced"]), "Delivered"),
			((1, "Incoming", ["Received", "Created"]), "Partially Received"),
			((1, "Incoming", ["Deposited", "Received"]), "Received"),
			((1, "Outgoing", ["Cleared", "Delivered"]), "Partially Cleared"),
			((1, "Outgoing", ["Cleared", "Replaced", "Cleared"]), "Cleared"),
			((1, "Outgoing", ["Cancelled"]), "Submitted"),
		]
		for args, expected in cases:
			self.assertEqual(compute_batch_status(*args), expected, args)

	# ------------------------------------------------------------------ permissions

	def test_treasury_user_creates_and_submits(self):
		with self.set_user(TREASURY_USER):
			doc = self.make_batch(handover_date=ISSUE, rows=self.numbered_rows(2))
			doc.insert()
			doc.submit()
			doc.reload()
			self.assertEqual(doc.status, "Delivered")
			# cancelling a batch cancels cheques: a Treasury Manager action
			self.assertRaises(frappe.PermissionError, doc.cancel)

		for cheque in self.cheques(doc):
			self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Delivered", "Party Posted"))
			self.assertEqual(cheque.owner, TREASURY_USER)
			self.assertEqual(frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "owner"), TREASURY_USER)

	def test_submit_needs_pdc_cheque_create_permission(self):
		frappe.db.savepoint(PERMISSION_SAVEPOINT)
		self.addCleanup(self.restore_permissions)
		# a role allowed to submit batches but not to create cheques (a custom setup)
		for ptype in ("write", "create", "submit"):
			update_permission_property(DOCTYPE, "Accounts User", 0, ptype, 1)
		frappe.clear_cache(doctype=DOCTYPE)
		self.assertFalse(frappe.has_permission(CHEQUE, "create", user=NO_CHEQUE_USER))

		with self.set_user(NO_CHEQUE_USER):
			doc = self.make_batch(rows=self.numbered_rows(1))
			doc.insert()
			with self.assertRaises(frappe.PermissionError) as ctx:
				doc.submit()
		self.assertIn(CHEQUE, str(ctx.exception))
		self.assertEqual(frappe.db.get_value(DOCTYPE, doc.name, "docstatus"), 0)
		self.assertEqual(frappe.db.count(CHEQUE, {"cheque_batch": doc.name}), 0)

	@staticmethod
	def restore_permissions():
		frappe.db.rollback(save_point=PERMISSION_SAVEPOINT)
		frappe.clear_cache(doctype=DOCTYPE)
		clear_user_cache()

	# ------------------------------------------------------------------ print format and report

	def test_batch_receipt_print(self):
		doc = self.make_batch(handover_date=ISSUE, rows=self.numbered_rows(3, amount=1000))
		doc.insert()
		doc.submit()
		doc.reload()
		previous = frappe.local.lang
		self.addCleanup(setattr, frappe.local, "lang", previous)

		frappe.local.lang = "en"
		html = frappe.get_print(DOCTYPE, doc.name, print_format="Cheque Batch Receipt")
		for row in doc.cheques:
			self.assertIn(row.cheque_no, html)
			self.assertIn(formatdate(row.due_date), html)
		self.assertIn(doc.party_name, html)
		self.assertIn("Three Thousand", html)
		self.assertIn("Cheques Delivery Voucher", html)
		print_date = re.search(r'class="pdc-print-date-value">([^<]*)<', html).group(1).strip()
		self.assertEqual(print_date, formatdate(nowdate()))
		self.assertIn('dir="ltr"', html)

		frappe.local.lang = "ar"
		html = frappe.get_print(DOCTYPE, doc.name, print_format="Cheque Batch Receipt")
		self.assertIn('dir="rtl"', html)
		words = re.search(r'class="pdc-amount-words">([^<]*)<', html).group(1)
		self.assertTrue(ARABIC.search(words), words)

	def test_pdc_register_filters_by_batch(self):
		doc = self.make_batch(rows=self.numbered_rows(2))
		doc.insert()
		doc.submit()
		doc.reload()
		make_pdc_cheque(issue_date=ISSUE, due_date=FIRST_DUE)  # not in the batch

		_columns, data = pdc_register.execute(
			{"company": COMPANY, "cheque_batch": doc.name, "date_based_on": "Issue Date"}
		)

		self.assertEqual(
			sorted(row.cheque for row in data if row.get("cheque")), sorted(r.pdc_cheque for r in doc.cheques)
		)

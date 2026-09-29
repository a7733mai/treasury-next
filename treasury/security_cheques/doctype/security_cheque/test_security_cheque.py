# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Security Cheque (PLAN.md §7): lifecycle, custody, protections, encashment through a PDC Cheque, printing.

Rule 4 of CLAUDE.md: a security cheque never creates accounting entries. Every lifecycle test checks that no
GL Entry, Payment Ledger Entry, Payment Entry or Journal Entry appeared; only an encashment posts, through
the PDC Cheque it creates (and the PDC lifecycle's settings).
"""

import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, formatdate, getdate, nowdate

from treasury.pdc import lifecycle as pdc_lifecycle
from treasury.pdc.doctype.cheque_book.cheque_book import cancel_leaf
from treasury.pdc.report.cheque_book_usage.cheque_book_usage import execute as cheque_book_usage
from treasury.security_cheques.doctype.security_cheque.security_cheque import deposit_bank_accounts
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	assert_gl_entries,
	bank_account_name,
	day,
	ensure_custody_location,
	ensure_customer,
	ensure_employee,
	ensure_user,
	get_party_balance,
	get_party_outstanding,
	get_voucher_count,
	make_cheque_book,
	make_pdc_cheque,
	make_sales_invoice,
	make_sales_order,
	make_security_cheque,
	unique_cheque_no,
)

# Fixtures are built by treasury.tests.utils; don't let Frappe pull ERPNext's global test records.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Company",
	"Account",
	"Bank Account",
	"Cheque Book",
	"Currency",
	"Custody Location",
	"DocType",
	"Employee",
	"Journal Entry",
	"Payment Entry",
	"PDC Cheque",
	"Print Format",
	"Security Cheque",
	"User",
]

DOCTYPE = "Security Cheque"
SAFE = "_Test Treasury Safe"
VAULT = "_Test Treasury Vault"
CUSTODIAN = "treasury-sec-custodian@example.com"
TREASURY_USER = "treasury-sec-user@example.com"


def ledger_counts() -> tuple:
	"""Accounting rows of the test company: must not move on any security cheque action."""
	return (
		frappe.db.count("GL Entry", {"company": COMPANY}),
		frappe.db.count("Payment Ledger Entry", {"company": COMPANY}),
		frappe.db.count("Payment Entry", {"company": COMPANY}),
		frappe.db.count("Journal Entry", {"company": COMPANY}),
	)


class SecurityChequeTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_custody_location(SAFE)
		ensure_custody_location(VAULT)
		ensure_user(CUSTODIAN, ["Treasury User"])
		ensure_user(TREASURY_USER, ["Treasury User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	def assert_no_accounting(self, cheque, before: tuple):
		self.assertEqual(ledger_counts(), before, "a security cheque action created accounting entries")
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 0)
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": cheque.name}))
		self.assertFalse(frappe.db.exists("Payment Ledger Entry", {"voucher_no": cheque.name}))

	@staticmethod
	def events(cheque) -> list[str]:
		cheque.reload()
		return [row.event for row in cheque.status_log]


class TestSecurityCheque(SecurityChequeTestCase):
	# ------------------------------------------------------------------ creation and validation

	def test_defaults_on_insert(self):
		incoming = make_security_cheque(amount=2500)
		self.assertTrue(incoming.name.startswith("SEC-IN-"))
		self.assertEqual(incoming.status, "Created")
		self.assertEqual(incoming.currency, "EGP")
		self.assertEqual(incoming.party_name, CUSTOMER)
		self.assertEqual([row.event for row in incoming.status_log], ["Created"])

		outgoing = make_security_cheque("Outgoing", currency="USD", amount=100)
		self.assertTrue(outgoing.name.startswith("SEC-OUT-"))
		self.assertEqual((outgoing.currency, outgoing.party_name), ("USD", SUPPLIER))

	def test_amount_rules(self):
		self.assertRaises(frappe.ValidationError, make_security_cheque, amount=0)
		self.assertRaises(frappe.ValidationError, make_security_cheque, amount=-10)
		open_cheque = make_security_cheque(amount_type="Open / Blank")
		self.assertEqual(flt(open_cheque.amount), 0)
		# an open cheque carries no amount
		open_cheque.amount = 100
		self.assertRaises(frappe.ValidationError, open_cheque.save)
		self.assertRaises(frappe.ValidationError, make_security_cheque, amount_type="Sometimes")

	def test_dates(self):
		self.assertRaises(
			frappe.ValidationError, make_security_cheque, issue_date=day(0), expiry_date=day(-1)
		)
		undated = make_security_cheque(cheque_date=None, expiry_date=day(30))
		self.assertFalse(undated.cheque_date)

	def test_incoming_currency_on_insert(self):
		# no currency given: the company currency, not the user / site default Frappe injects on insert
		cheque = make_security_cheque(amount=100)
		self.assertEqual(frappe.db.get_value(DOCTYPE, cheque.name, "currency"), "EGP")
		# a currency deliberately chosen is kept
		fields = ("direction", "company", "party_type", "party", "purpose", "drawer_bank", "amount_type")
		chosen = frappe.get_doc(
			{
				"doctype": DOCTYPE,
				**{f: cheque.get(f) for f in fields},
				"issue_date": cheque.issue_date,
				"cheque_no": unique_cheque_no(),
				"amount": 100,
				"currency": "USD",
			}
		).insert()
		self.assertEqual(frappe.db.get_value(DOCTYPE, chosen.name, "currency"), "USD")

	def test_incoming_currency_is_editable_before_the_first_action(self):
		cheque = make_security_cheque(amount=100)
		cheque.currency = "USD"
		cheque.save()
		cheque.receive(receipt_date=day(-10))
		cheque.reload()
		cheque.currency = "EGP"
		self.assertRaises(frappe.ValidationError, cheque.save)

	def test_bank_fields_only_on_outgoing(self):
		self.assertRaises(frappe.ValidationError, make_security_cheque, bank_account=bank_account_name("EGP"))
		self.assertRaises(frappe.ValidationError, make_security_cheque, "Outgoing", bank_account=None)

	def test_party_types(self):
		other = make_security_cheque(party_type="Other", party=None, party_name_other=" Tender Committee ")
		self.assertEqual((other.party, other.party_name), (None, "Tender Committee"))
		self.assertRaises(frappe.ValidationError, make_security_cheque, party_type="Other", party=None)
		self.assertRaises(frappe.ValidationError, make_security_cheque, party_type="Shareholder")

		employee = ensure_employee()
		custody = make_security_cheque(party_type="Employee", party=employee, purpose="Employee Custody")
		self.assertEqual(custody.party, employee)
		self.assertTrue(custody.party_name)

	def test_reference_rules(self):
		order = make_sales_order(1000, transaction_date=day(-70))
		cheque = make_security_cheque(reference_doctype="Sales Order", reference_name=order.name)
		self.assertEqual(cheque.reference_name, order.name)

		invoice = make_sales_invoice(100, posting_date=day(-70))
		self.assertRaises(
			frappe.ValidationError,
			make_security_cheque,
			reference_doctype="Sales Invoice",
			reference_name=invoice.name,
		)
		self.assertRaises(frappe.ValidationError, make_security_cheque, reference_doctype="Sales Order")
		self.assertRaises(frappe.ValidationError, make_security_cheque, reference_name=order.name)

	# ------------------------------------------------------------------ no accounting, ever

	def test_incoming_lifecycle_never_posts(self):
		before = ledger_counts()
		cheque = make_security_cheque(amount=7000)
		self.assert_no_accounting(cheque, before)

		cheque.receive(receipt_date=day(-50), custody_location=SAFE, custodian=CUSTODIAN)
		cheque.reload()
		self.assertEqual(
			(cheque.status, cheque.custody_location, cheque.custodian), ("In Custody", SAFE, CUSTODIAN)
		)
		self.assertEqual(getdate(cheque.handover_date), day(-50))
		self.assert_no_accounting(cheque, before)

		cheque.transfer_custody(transfer_date=day(-40), custody_location=VAULT, remarks="Moved to the vault")
		cheque.reload()
		self.assertEqual(
			(cheque.status, cheque.custody_location, cheque.custodian), ("In Custody", VAULT, None)
		)
		self.assert_no_accounting(cheque, before)

		cheque.return_cheque(return_date=day(-5), person="Customer's accountant", reason="Contract completed")
		cheque.reload()
		self.assertEqual(cheque.status, "Returned")
		self.assertEqual((cheque.custody_location, cheque.custodian), (None, None))
		self.assertEqual(
			(getdate(cheque.return_date), cheque.return_person), (day(-5), "Customer's accountant")
		)
		self.assert_no_accounting(cheque, before)

		self.assertEqual(
			self.events(cheque), ["Created", "Received", "Stored", "Custody Transferred", "Returned"]
		)
		self.assertEqual(
			[(r.from_location, r.to_location, r.from_custodian, r.to_custodian) for r in cheque.custody_log],
			[(None, SAFE, None, CUSTODIAN), (SAFE, VAULT, CUSTODIAN, None), (VAULT, None, None, None)],
		)
		self.assertEqual([getdate(r.event_date) for r in cheque.custody_log], [day(-50), day(-40), day(-5)])
		returned = cheque.status_log[-1]
		self.assertEqual((returned.from_status, returned.to_status), ("In Custody", "Returned"))
		self.assertEqual(getdate(returned.event_date), day(-5))

	def test_outgoing_lifecycle_never_posts(self):
		before = ledger_counts()
		cheque = make_security_cheque("Outgoing", amount=9000)
		cheque.issue(handover_date=day(-30))
		cheque.reload()
		self.assertEqual((cheque.status, getdate(cheque.handover_date)), ("Issued", day(-30)))
		self.assert_no_accounting(cheque, before)

		cheque.mark_lost(lost_date=day(-10), reason="Lost by the tender committee")
		cheque.reload()
		self.assertEqual((cheque.status, getdate(cheque.lost_date)), ("Lost", day(-10)))
		self.assert_no_accounting(cheque, before)

		returned = make_security_cheque("Outgoing", amount=4000)
		returned.issue(handover_date=day(-30))
		returned.return_cheque(return_date=day(-2), person="Our messenger", reason="Tender closed")
		returned.reload()
		self.assertEqual(returned.status, "Returned")
		self.assert_no_accounting(returned, before)

	def test_lost_incoming_and_cancel_never_post(self):
		before = ledger_counts()
		lost = make_security_cheque()
		lost.receive(receipt_date=day(-20), custody_location=SAFE)
		lost.mark_lost(lost_date=day(-1), reason="Missing from the safe")
		lost.reload()
		self.assertEqual((lost.status, lost.custody_location), ("Lost", None))
		self.assert_no_accounting(lost, before)

		cancelled = make_security_cheque()
		cancelled.cancel_cheque(reason="Entered twice")
		cancelled.reload()
		self.assertEqual(cancelled.status, "Cancelled")
		self.assert_no_accounting(cancelled, before)

	# ------------------------------------------------------------------ transitions

	def test_receive_then_store(self):
		cheque = make_security_cheque()
		cheque.receive(receipt_date=day(-20))
		cheque.reload()
		self.assertEqual((cheque.status, cheque.custody_location), ("Received", None))
		self.assertFalse(cheque.custody_log)

		self.assertRaises(frappe.ValidationError, cheque.transfer_custody, day(-10), VAULT)
		cheque.store(store_date=day(-19), custody_location=SAFE)
		cheque.reload()
		self.assertEqual((cheque.status, cheque.custody_location), ("In Custody", SAFE))
		self.assertRaises(frappe.ValidationError, cheque.store, day(-18), VAULT)

	def test_actions_are_idempotent(self):
		incoming = make_security_cheque()
		incoming.receive(receipt_date=day(-20), custody_location=SAFE)
		log_rows = len(frappe.get_doc(DOCTYPE, incoming.name).status_log)
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			incoming.receive(receipt_date=day(-20))
		self.assertEqual(len(frappe.get_doc(DOCTYPE, incoming.name).status_log), log_rows)

		incoming.return_cheque(return_date=day(-1), reason="Done")
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			incoming.return_cheque(return_date=day(-1), reason="Done")

		outgoing = make_security_cheque("Outgoing")
		outgoing.issue(handover_date=day(-5))
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			outgoing.issue(handover_date=day(-5))

		cancelled = make_security_cheque()
		cancelled.cancel_cheque(reason="Duplicate")
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			cancelled.cancel_cheque(reason="Duplicate")

	def test_transition_rules(self):
		incoming = make_security_cheque()
		self.assertRaises(frappe.ValidationError, incoming.issue, day(-5))  # outgoing only
		self.assertRaises(frappe.ValidationError, incoming.return_cheque, day(-5), None, "Not received yet")
		self.assertRaises(frappe.ValidationError, incoming.mark_lost, day(-5), "Not received yet")
		self.assertRaises(frappe.ValidationError, incoming.cancel_cheque)  # reason required

		outgoing = make_security_cheque("Outgoing")
		self.assertRaises(frappe.ValidationError, outgoing.receive, day(-5))  # incoming only
		outgoing.issue(handover_date=day(-5))
		self.assertRaises(frappe.ValidationError, outgoing.cancel_cheque, "Too late")
		self.assertRaises(frappe.ValidationError, outgoing.return_cheque, day(-4))  # reason required

		cancelled = make_security_cheque()
		cancelled.cancel_cheque(reason="Wrong party")
		self.assertRaises(frappe.ValidationError, cancelled.receive, day(-5))

	def test_dates_follow_the_events(self):
		cheque = make_security_cheque(issue_date=day(-60))
		self.assertRaises(frappe.ValidationError, cheque.receive, day(-61))  # before the issue date
		cheque.receive(receipt_date=day(-30), custody_location=SAFE)
		self.assertRaises(frappe.ValidationError, cheque.transfer_custody, day(-31), VAULT)
		self.assertRaises(frappe.ValidationError, cheque.return_cheque, day(-31), None, "Early")

	def test_custody_rules(self):
		inactive = ensure_custody_location("_Test Treasury Closed Safe", is_active=0)
		cheque = make_security_cheque()
		self.assertRaises(frappe.ValidationError, cheque.receive, day(-10), inactive)
		self.assertRaises(frappe.ValidationError, cheque.receive, day(-10), "_Test No Such Place")
		self.assertRaises(frappe.ValidationError, cheque.receive, day(-10), None, CUSTODIAN)
		cheque.receive(receipt_date=day(-10), custody_location=SAFE, custodian=CUSTODIAN)
		# same place, same custodian: nothing to transfer
		self.assertRaises(frappe.ValidationError, cheque.transfer_custody, day(-9), SAFE, CUSTODIAN)
		cheque.transfer_custody(transfer_date=day(-9), custody_location=SAFE, custodian=TREASURY_USER)
		cheque.reload()
		self.assertEqual((cheque.custody_location, cheque.custodian), (SAFE, TREASURY_USER))

	def test_is_expired_indicator(self):
		cheque = make_security_cheque(issue_date=day(-60), expiry_date=day(-1))
		self.assertEqual(cheque.is_expired, 0)  # Created: not held yet
		cheque.receive(receipt_date=day(-50))
		cheque.reload()
		self.assertEqual(cheque.is_expired, 1)
		cheque.return_cheque(return_date=day(-1), reason="Expired")
		cheque.reload()
		self.assertEqual(cheque.is_expired, 0)

	# ------------------------------------------------------------------ protections

	def test_lifecycle_fields_cannot_be_changed_directly(self):
		cheque = make_security_cheque()
		cheque.receive(receipt_date=day(-20), custody_location=SAFE, custodian=CUSTODIAN)
		tampering = {
			"status": "Returned",
			"custody_location": VAULT,
			"custodian": TREASURY_USER,
			"handover_date": day(-19),
			"return_date": day(-1),
			"converted_pdc_cheque": make_pdc_cheque("Incoming").name,
			"encashed_amount": 10,
		}
		for fieldname, value in tampering.items():
			with self.subTest(field=fieldname):
				doc = frappe.get_doc(DOCTYPE, cheque.name)
				doc.set(fieldname, value)
				self.assertRaises(frappe.ValidationError, doc.save)

		# the custody log is append-only, and only the actions append to it
		doc = frappe.get_doc(DOCTYPE, cheque.name)
		doc.custody_log[0].to_location = VAULT
		self.assertRaises(frappe.ValidationError, doc.save)
		doc = frappe.get_doc(DOCTYPE, cheque.name)
		doc.custody_log.pop()
		self.assertRaises(frappe.ValidationError, doc.save)
		doc = frappe.get_doc(DOCTYPE, cheque.name)
		doc.append("custody_log", {"from_location": SAFE, "to_location": VAULT})
		self.assertRaises(frappe.ValidationError, doc.save)
		doc = frappe.get_doc(DOCTYPE, cheque.name)
		doc.status_log[0].remarks = "Edited"
		self.assertRaises(frappe.ValidationError, doc.save)

		cheque.reload()
		self.assertEqual(
			(cheque.status, cheque.custody_location, len(cheque.custody_log)), ("In Custody", SAFE, 1)
		)
		# free fields remain editable
		cheque.remarks = "Kept in the red folder"
		cheque.save()

	def test_lifecycle_fields_are_reset_on_insert(self):
		cheque = make_security_cheque(
			status="Encashed",
			custody_location=SAFE,
			custodian=CUSTODIAN,
			handover_date=day(-1),
			encashed_amount=999,
			custody_log=[{"from_location": SAFE, "to_location": VAULT}],
			status_log=[{"event": "Encashed", "to_status": "Encashed"}],
		)
		self.assertEqual(cheque.status, "Created")
		self.assertFalse(cheque.custody_location or cheque.custodian or cheque.handover_date)
		self.assertEqual(flt(cheque.encashed_amount), 0)
		self.assertFalse(cheque.custody_log)
		self.assertEqual([row.event for row in cheque.status_log], ["Created"])

	def test_identity_fields_locked_after_the_first_action(self):
		cheque = make_security_cheque(amount=3000)
		cheque.amount = 3500  # still Created: editable
		cheque.save()
		cheque.receive(receipt_date=day(-10))
		changes = {
			"amount": 4000,
			"amount_type": "Open / Blank",
			"cheque_no": unique_cheque_no(),
			"party": ensure_customer("_Test Treasury Sec Other Customer"),
			"drawer_bank": "Another Bank",
		}
		for fieldname, value in changes.items():
			with self.subTest(field=fieldname):
				doc = frappe.get_doc(DOCTYPE, cheque.name)
				doc.set(fieldname, value)
				if fieldname == "amount_type":
					doc.amount = 0
				self.assertRaises(frappe.ValidationError, doc.save)
		# the expiry moves only through the (logged) Extend Expiry action
		doc = frappe.get_doc(DOCTYPE, cheque.name)
		doc.expiry_date = day(400)
		self.assertRaisesRegex(frappe.ValidationError, "Extend Expiry", doc.save)
		# what it guarantees stays editable while it is live
		doc = frappe.get_doc(DOCTYPE, cheque.name)
		doc.required_amount = 5000
		doc.save()

	def test_extend_expiry(self):
		cheque = make_security_cheque(issue_date=day(-60), expiry_date=day(10))
		cheque.receive(receipt_date=day(-50), custody_location=SAFE)
		before = ledger_counts()

		self.assertRaises(frappe.ValidationError, cheque.extend_expiry, day(40))  # no reason
		self.assertRaises(frappe.ValidationError, cheque.extend_expiry, day(10), "Same date")
		self.assertRaises(frappe.ValidationError, cheque.extend_expiry, day(5), "Shorter")
		cheque.extend_expiry(new_date=day(40), reason="Contract extended")

		cheque.reload()
		self.assertEqual((getdate(cheque.expiry_date), cheque.status), (day(40), "In Custody"))
		row = cheque.status_log[-1]
		self.assertEqual(
			(row.event, row.from_status, row.to_status, row.event_date),
			("Expiry Extended", "In Custody", "In Custody", None),
		)
		self.assertIn(formatdate(day(10)), row.remarks)
		self.assertIn(formatdate(day(40)), row.remarks)
		self.assertIn("Contract extended", row.remarks)
		self.assert_no_accounting(cheque, before)
		# later actions keep their own dates (the extension has no event date)
		cheque.return_cheque(return_date=day(-40), reason="Completed")

		cheque.reload()
		self.assertRaisesRegex(
			frappe.ValidationError, "not allowed", cheque.extend_expiry, day(90), "Too late"
		)

	def test_final_cheque_keeps_what_it_guaranteed(self):
		order = make_sales_order(20000, transaction_date=day(-70))
		cheque = make_security_cheque(
			issue_date=day(-60),
			expiry_date=day(10),
			required_amount=8000,
			reference_doctype="Sales Order",
			reference_name=order.name,
		)
		cheque.receive(receipt_date=day(-50))
		cheque.return_cheque(return_date=day(-5), reason="Completed")
		changes = {"required_amount": 9000, "reference_doctype": None, "expiry_date": day(20)}
		for fieldname, value in changes.items():
			with self.subTest(field=fieldname):
				doc = frappe.get_doc(DOCTYPE, cheque.name)
				doc.set(fieldname, value)
				if fieldname == "reference_doctype":
					doc.reference_name = None
				self.assertRaises(frappe.ValidationError, doc.save)
		# free fields remain editable
		doc = frappe.get_doc(DOCTYPE, cheque.name)
		doc.remarks = "Archived"
		doc.save()

	def test_delete_only_before_any_action(self):
		draft = make_security_cheque()
		frappe.delete_doc(DOCTYPE, draft.name)
		self.assertFalse(frappe.db.exists(DOCTYPE, draft.name))

		received = make_security_cheque()
		received.receive(receipt_date=day(-1))
		self.assertRaises(frappe.ValidationError, frappe.delete_doc, DOCTYPE, received.name)

	# ------------------------------------------------------------------ roles

	def test_open_amount_requires_the_manager(self):
		with self.set_user(TREASURY_USER):
			self.assertRaises(frappe.PermissionError, make_security_cheque, amount_type="Open / Blank")
			fixed = make_security_cheque(amount=100)
			fixed.amount_type = "Open / Blank"
			fixed.amount = 0
			self.assertRaises(frappe.PermissionError, fixed.save)

		open_incoming = make_security_cheque(amount_type="Open / Blank")  # Administrator: all roles
		open_outgoing = make_security_cheque("Outgoing", amount_type="Open / Blank")
		with self.set_user(TREASURY_USER):
			self.assertRaises(frappe.PermissionError, open_incoming.receive, day(-5))
			self.assertRaises(frappe.PermissionError, open_outgoing.issue, day(-5))
		open_incoming.receive(receipt_date=day(-5))
		self.assertEqual(frappe.db.get_value(DOCTYPE, open_incoming.name, "status"), "Received")

	def test_treasury_user_cannot_encash_or_mark_lost(self):
		cheque = make_security_cheque(amount=1000)
		with self.set_user(TREASURY_USER):
			cheque.receive(receipt_date=day(-10), custody_location=SAFE)
			self.assertRaises(frappe.PermissionError, cheque.mark_lost, day(-5), "Lost")
			self.assertRaises(
				frappe.PermissionError,
				cheque.encash,
				day(-5),
				None,
				None,
				bank_account_name("EGP"),
			)
			cheque.transfer_custody(transfer_date=day(-9), custody_location=VAULT)
		cheque.reload()
		self.assertEqual((cheque.status, cheque.custody_location), ("In Custody", VAULT))
		self.assertFalse(frappe.db.exists("PDC Cheque", {"security_cheque": cheque.name}))

	# ------------------------------------------------------------------ cheque numbers and cheque book

	def test_outgoing_cheque_no_is_unique_per_bank_account(self):
		cheque_no = unique_cheque_no()
		make_security_cheque("Outgoing", cheque_no=cheque_no)
		self.assertRaises(frappe.ValidationError, make_security_cheque, "Outgoing", cheque_no=cheque_no)
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, cheque_no=cheque_no)
		# another bank account, or someone else's (incoming) cheque, may carry the same number
		make_security_cheque("Outgoing", cheque_no=cheque_no, currency="USD", amount=10)
		make_security_cheque(cheque_no=cheque_no)
		make_pdc_cheque("Incoming", cheque_no=cheque_no)

		pdc_no = unique_cheque_no()
		make_pdc_cheque(cheque_no=pdc_no)
		self.assertRaises(frappe.ValidationError, make_security_cheque, "Outgoing", cheque_no=pdc_no)

	def test_cheque_book_leaf_is_consumed_for_good(self):
		start = 1_000_000 + (int(frappe.generate_hash(length=6), 16) % 100_000) * 10
		book = make_cheque_book(start, start + 3)
		security = make_security_cheque("Outgoing", cheque_book=book.name)
		self.assertEqual(security.cheque_no, str(start))

		pdc = make_pdc_cheque(cheque_book=book.name, issue_date=day(0), due_date=day(10))
		self.assertEqual(pdc.cheque_no, str(start + 1))

		# cancelling the security cheque frees nothing: the leaf stays used
		security.cancel_cheque(reason="Tender cancelled")
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, cheque_no=str(start))
		self.assertRaises(frappe.ValidationError, cancel_leaf, book.name, str(start), "Spoilt")
		self.assertRaises(frappe.ValidationError, frappe.delete_doc, DOCTYPE, security.name)
		next_pdc = make_pdc_cheque(cheque_book=book.name, issue_date=day(0), due_date=day(10))
		self.assertEqual(next_pdc.cheque_no, str(start + 2))

		_columns, data = cheque_book_usage({"company": COMPANY, "bank_account": book.bank_account})
		row = next(r for r in data if r.cheque_book == book.name)
		self.assertEqual((row.used_leaves, row.remaining_leaves), (3, 1))

	def test_cheque_book_must_match_the_bank_account(self):
		start = 2_000_000 + (int(frappe.generate_hash(length=6), 16) % 100_000) * 10
		usd_book = make_cheque_book(start, start + 1, currency="USD")
		self.assertRaises(frappe.ValidationError, make_security_cheque, "Outgoing", cheque_book=usd_book.name)


class TestSecurityChequeEncashment(SecurityChequeTestCase):
	def received(self, amount=5000, **values):
		cheque = make_security_cheque(amount=amount, issue_date=day(-90), **values)
		cheque.receive(receipt_date=day(-80), custody_location=SAFE, custodian=CUSTODIAN)
		cheque.reload()
		return cheque

	def pdc_of(self, security) -> frappe._dict:
		security.reload()
		return frappe.get_doc("PDC Cheque", security.converted_pdc_cheque)

	def test_encash_incoming_settles_the_invoice(self):
		invoice = make_sales_invoice(5000, posting_date=day(-30))
		security = self.received(5000)
		before = ledger_counts()

		result = security.encash(
			encashment_date=day(-5),
			bank_account=bank_account_name("EGP"),
			references=[
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": invoice.name,
					"allocated_amount": 5000,
				}
			],
			remarks="Customer defaulted",
		)

		security.reload()
		self.assertEqual(result["status"], "Encashed")
		self.assertEqual(result["pdc_cheque"], security.converted_pdc_cheque)
		self.assertEqual(
			(security.status, getdate(security.encashment_date), flt(security.encashed_amount)),
			("Encashed", day(-5), 5000),
		)
		self.assertEqual((security.custody_location, security.custodian), (None, None))
		self.assertEqual(security.custody_log[-1].to_location, None)
		encashed = security.status_log[-1]
		self.assertEqual((encashed.event, encashed.pdc_cheque), ("Encashed", security.converted_pdc_cheque))
		# the security cheque itself posted nothing: the vouchers belong to the PDC Cheque
		self.assertEqual(get_voucher_count(DOCTYPE, security.name), 0)

		pdc = self.pdc_of(security)
		self.assertEqual(pdc.security_cheque, security.name)
		self.assertEqual(
			(pdc.direction, pdc.party, pdc.cheque_no, flt(pdc.amount), pdc.drawer_bank),
			("Incoming", CUSTOMER, security.cheque_no, 5000, security.drawer_bank),
		)
		self.assertEqual((pdc.operational_status, pdc.accounting_status), ("Received", "Party Posted"))
		self.assertEqual((getdate(pdc.handover_date), getdate(pdc.original_due_date)), (day(-5), day(-5)))
		assert_gl_entries(
			self,
			"Payment Entry",
			pdc.party_payment_entry,
			[
				(self.data.egp.pdc_receivable, 5000, 0, None, day(-5)),
				(self.data.receivable, 0, 5000, CUSTOMER, day(-5)),
			],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", invoice.name), 0)
		self.assertEqual(get_voucher_count("PDC Cheque", pdc.name), 1)
		self.assertNotEqual(ledger_counts(), before)

		# idempotent: a second encashment is refused and creates nothing
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		self.assertEqual(frappe.db.count("PDC Cheque", {"security_cheque": security.name}), 1)
		self.assertEqual(get_voucher_count("PDC Cheque", pdc.name), 1)

	def test_encash_incoming_without_invoice_is_an_advance(self):
		customer = ensure_customer("_Test Treasury Sec Advance Customer")
		security = self.received(1500, party=customer)
		balance = get_party_balance("Customer", customer, day(0))

		security.encash(encashment_date=day(-3), due_date=day(10), bank_account=bank_account_name("EGP"))

		pdc = self.pdc_of(security)
		self.assertEqual(getdate(pdc.original_due_date), day(10))
		self.assertFalse(pdc.references)
		self.assertEqual(flt(get_party_balance("Customer", customer, day(0)) - balance, 2), -1500)

	def test_encash_in_cheque_date_mode_waits_for_the_scheduler(self):
		self.use_company_settings(incoming_party_posting="On Cheque Date")
		security = self.received(800)
		security.encash(encashment_date=day(-3), due_date=day(20), bank_account=bank_account_name("EGP"))
		pdc = self.pdc_of(security)
		self.assertEqual((pdc.operational_status, pdc.accounting_status), ("Received", "Not Posted"))
		self.assertEqual(get_voucher_count("PDC Cheque", pdc.name), 0)

	def test_encash_open_cheque_needs_the_amount(self):
		security = self.received(amount_type="Open / Blank")
		self.assertRaises(
			frappe.ValidationError, security.encash, day(-5), None, None, bank_account_name("EGP")
		)
		security.encash(encashment_date=day(-5), amount=1234.5, bank_account=bank_account_name("EGP"))
		security.reload()
		self.assertEqual(flt(security.encashed_amount), 1234.5)
		self.assertEqual(flt(security.amount), 0)
		self.assertEqual(flt(self.pdc_of(security).amount), 1234.5)

	def test_encash_fixed_cheque_for_another_amount_is_refused(self):
		security = self.received(2000)
		self.assertRaises(
			frappe.ValidationError, security.encash, day(-5), 2500, None, bank_account_name("EGP")
		)
		security.encash(encashment_date=day(-5), amount=2000, bank_account=bank_account_name("EGP"))
		self.assertEqual(frappe.db.get_value(DOCTYPE, security.name, "status"), "Encashed")

	def test_encash_needs_a_party(self):
		# Phase 7a: any Party Type may be encashed (an Employee's: tests/test_open_party.py); "Other" may not
		other = self.received(3000, party_type="Other", party=None, party_name_other="Tender Board")
		self.assertRaises(frappe.ValidationError, other.encash, day(-5), None, None, bank_account_name("EGP"))
		self.assertEqual(frappe.db.get_value(DOCTYPE, other.name, "status"), "In Custody")
		self.assertFalse(frappe.db.exists("PDC Cheque", {"security_cheque": other.name}))

	def test_encash_needs_a_deposit_bank_in_the_cheque_currency(self):
		security = self.received(1000)
		# the encash dialog offers only the company banks in the cheque currency
		offered = deposit_bank_accounts(security.name)
		self.assertIn(bank_account_name("EGP"), offered)
		self.assertNotIn(bank_account_name("USD"), offered)
		before = ledger_counts()
		self.assertRaises(frappe.ValidationError, security.encash, day(-5))  # no bank account
		# a USD bank for an EGP cheque: the PDC Cheque is rolled back with everything else
		self.assertRaises(
			frappe.ValidationError, security.encash, day(-5), None, None, bank_account_name("USD")
		)
		self.assertEqual(frappe.db.get_value(DOCTYPE, security.name, "status"), "In Custody")
		self.assertFalse(frappe.db.exists("PDC Cheque", {"security_cheque": security.name}))
		self.assertEqual(ledger_counts(), before)

	def assert_encash_rolled_back(self, security, status: str, custody_rows: int, before: tuple, seen: dict):
		"""After a failure once the PDC Cheque existed (`seen`: what the failing step saw): nothing kept."""
		self.assertTrue(seen.get("pdc_cheque"), "the failure was not after the PDC Cheque insert")
		self.assertFalse(frappe.db.exists("PDC Cheque", seen["pdc_cheque"]))
		self.assertFalse(frappe.db.exists("PDC Cheque", {"security_cheque": security.name}))
		self.assertEqual(get_voucher_count("PDC Cheque", seen["pdc_cheque"]), 0)
		security.reload()
		self.assertEqual((security.status, security.converted_pdc_cheque), (status, None))
		self.assertEqual((security.encashment_date, flt(security.encashed_amount)), (None, 0))
		self.assertEqual(len(security.custody_log), custody_rows)
		self.assertNotIn("Encashed", [row.event for row in security.status_log])
		self.assertEqual(ledger_counts(), before)

	@staticmethod
	def failing_step(seen: dict):
		"""Stands in for the PDC receive / deliver: records the state it saw, then fails."""

		def fail(name, *args, **kwargs):
			seen["pdc_cheque"] = name
			seen["security_status"] = frappe.db.get_value(
				"Security Cheque", {"converted_pdc_cheque": name}, "status"
			)
			seen["vouchers"] = get_voucher_count("PDC Cheque", name)
			frappe.throw("Simulated failure of the PDC step")

		return fail

	def test_encash_incoming_rolls_back_when_the_pdc_receive_fails(self):
		security = self.received(1000)
		custody_rows = len(security.custody_log)
		before = ledger_counts()
		seen = {}
		with patch.object(pdc_lifecycle, "receive", side_effect=self.failing_step(seen)):
			self.assertRaises(
				frappe.ValidationError, security.encash, day(-5), None, None, bank_account_name("EGP")
			)
		# the step failed after the PDC Cheque was inserted and the security cheque saved as Encashed
		self.assertEqual(seen["security_status"], "Encashed")
		self.assert_encash_rolled_back(security, "In Custody", custody_rows, before, seen)

		# nothing left behind: the encashment can be done again
		security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		self.assertEqual(frappe.db.get_value(DOCTYPE, security.name, "status"), "Encashed")

	def test_encash_outgoing_rolls_back_when_the_pdc_delivery_fails(self):
		self.use_company_settings(outgoing_party_posting="On Creation")
		security = make_security_cheque("Outgoing", amount=2500, issue_date=day(-90))
		security.issue(handover_date=day(-80))
		security.reload()
		custody_rows = len(security.custody_log)
		before = ledger_counts()
		seen = {}
		with patch.object(pdc_lifecycle, "deliver", side_effect=self.failing_step(seen)):
			self.assertRaises(frappe.ValidationError, security.encash, day(-5))
		# On Creation: the PDC Cheque's insert had already posted its party Payment Entry
		self.assertEqual((seen["security_status"], seen["vouchers"]), ("Encashed", 1))
		self.assert_encash_rolled_back(security, "Issued", custody_rows, before, seen)

	def test_encash_not_before_the_last_event(self):
		security = self.received(1000)
		self.assertRaises(
			frappe.ValidationError, security.encash, day(-81), None, None, bank_account_name("EGP")
		)

	def test_encash_outgoing_then_clear_through_the_pdc_flow(self):
		security = make_security_cheque("Outgoing", amount=6000, issue_date=day(-90))
		security.issue(handover_date=day(-80))
		self.assertRaises(
			frappe.ValidationError,
			security.encash,
			day(-5),
			None,
			None,
			bank_account_name("USD"),  # an outgoing cheque is paid from its own bank
		)

		security.encash(encashment_date=day(-5))

		pdc = self.pdc_of(security)
		self.assertEqual(
			(pdc.direction, pdc.bank_account, pdc.cheque_no, pdc.party, flt(pdc.amount)),
			("Outgoing", security.bank_account, security.cheque_no, SUPPLIER, 6000),
		)
		self.assertFalse(pdc.cheque_book)
		self.assertEqual((pdc.operational_status, pdc.accounting_status), ("Delivered", "Party Posted"))
		assert_gl_entries(
			self,
			"Payment Entry",
			pdc.party_payment_entry,
			[
				(self.data.payable, 6000, 0, SUPPLIER, day(-5)),
				(self.data.egp.pdc_payable, 0, 6000, None, day(-5)),
			],
		)

		pdc.clear(clearance_date=day(-2))
		pdc.reload()
		self.assertEqual((pdc.operational_status, pdc.accounting_status), ("Cleared", "Bank Posted"))
		assert_gl_entries(
			self,
			"Journal Entry",
			pdc.bank_journal,
			[
				(self.data.egp.pdc_payable, 6000, 0, None, day(-2)),
				(self.data.egp.bank_gl, 0, 6000, None, day(-2)),
			],
		)
		# the security cheque stays Encashed and never gets a voucher of its own
		security.reload()
		self.assertEqual(security.status, "Encashed")
		self.assertEqual(get_voucher_count(DOCTYPE, security.name), 0)

	def test_back_link_is_insert_only(self):
		security = self.received(700)
		security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		pdc = self.pdc_of(security)
		pdc.security_cheque = None
		self.assertRaises(frappe.ValidationError, pdc.save)

		# a client cannot create a PDC Cheque claiming to come from a security cheque
		forged = make_pdc_cheque("Incoming", security_cheque=security.name)
		self.assertFalse(forged.security_cheque)

	def test_replacement_keeps_the_back_link(self):
		security = self.received(900)
		security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		pdc = self.pdc_of(security)
		result = pdc.replace(
			{"cheque_no": unique_cheque_no(), "replacement_date": day(-4), "reason": "Torn cheque"}
		)
		new = frappe.get_doc("PDC Cheque", result["replaced_by"])
		self.assertEqual((new.security_cheque, new.replaces), (security.name, pdc.name))
		# the security cheque keeps pointing at the first PDC Cheque; `replaced_by` leads on
		security.reload()
		self.assertEqual(security.converted_pdc_cheque, pdc.name)
		self.assertEqual(frappe.db.count("PDC Cheque", {"security_cheque": security.name}), 2)


class TestSecurityChequePrint(SecurityChequeTestCase):
	def setUp(self):
		super().setUp()
		previous = frappe.local.lang
		frappe.local.lang = "en"
		self.addCleanup(setattr, frappe.local, "lang", previous)

	@staticmethod
	def render(cheque) -> str:
		return frappe.get_print(DOCTYPE, cheque.name, print_format="Security Cheque Receipt")

	def test_receipt_fixed_amount(self):
		order = make_sales_order(20000, transaction_date=day(-70))
		cheque = make_security_cheque(
			amount=2500,
			reference_doctype="Sales Order",
			reference_name=order.name,
			expiry_date=day(200),
			purpose="Contract Performance",
		)
		cheque.receive(receipt_date=day(-40), custody_location=SAFE)
		cheque.reload()
		html = self.render(cheque)

		self.assertIn("Security Cheque Receipt", html)
		match = re.search(r'class="sec-print-date-value">([^<]*)<', html)
		self.assertEqual(match.group(1).strip(), formatdate(nowdate()))
		for text in (
			cheque.party_name,
			cheque.cheque_no,
			"Two Thousand, Five Hundred",
			"Contract Performance",
			order.name,
			formatdate(day(200)),
			formatdate(day(-40)),
			SAFE,
		):
			self.assertIn(text, html)
		self.assertIn('dir="ltr"', html)

	def test_receipt_open_amount_and_outgoing(self):
		cheque = make_security_cheque("Outgoing", amount_type="Open / Blank", purpose="Tender")
		html = self.render(cheque)
		self.assertIn("Security Cheque Handover Acknowledgment", html)
		self.assertIn("sec-open-amount", html)
		self.assertIn("Open / Blank", html)
		self.assertNotIn("sec-amount-words", html)

	def test_receipt_in_arabic(self):
		cheque = make_security_cheque(amount=1000)
		frappe.local.lang = "ar"
		html = self.render(cheque)
		self.assertIn('dir="rtl"', html)
		self.assertIn("ألف", html)

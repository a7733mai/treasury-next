"""Phase 7c: PDC Cheque / Cheque Batch mappers for invoices, orders, Journal Entries and party masters, and the
Cheque Batch "Created From" source (spec 7c §3.1, §3.3, §4 A-G, §7)."""

import copy
import random

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, today

from treasury.bank_transfers.api import make_bank_transfer as build_transfer
from treasury.pdc import batch
from treasury.pdc.api import make_cheque_batch as map_batch
from treasury.pdc.api import make_pdc_cheque as map_cheque
from treasury.security_cheques.api import make_security_cheque as map_security
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	OTHER_ABBR,
	OTHER_COMPANY,
	PE_SETTINGS,
	SUPPLIER,
	USD_SUPPLIER,
	TreasuryTestMixin,
	acc,
	add_user_permission,
	assert_gl_entries,
	ensure_account,
	ensure_company,
	ensure_customer,
	ensure_employee,
	ensure_fiscal_year,
	ensure_mode,
	ensure_user,
	get_ledger_open_amount,
	get_party_outstanding,
	isolate_cheque_books,
	make_bank_transfer,
	make_party_journal_entry,
	make_pdc_cheque,
	make_purchase_invoice,
	make_purchase_order,
	make_sales_invoice,
	make_sales_order,
	make_usd_purchase_invoice,
	set_mode_map,
	set_pdc_accounts,
	settings_for_documents,
	unique_cheque_no,
)
from treasury.utils import source_documents

NO_ROLES_USER = "treasury-7c-noroles@example.com"
READER = "treasury-7c-reader@example.com"
TREASURY_USER = "treasury-7c-user@example.com"
OTHER_COMPANY_USER = "treasury-7c-othercompany@example.com"
TREASURY_READER = "treasury-7c-user-reader@example.com"
CHEQUE_OUT_MODE = "_Test Treasury 7c Cheque Out"


def refs(values: dict) -> list[tuple]:
	return [
		(r["reference_doctype"], r["reference_name"], flt(r["allocated_amount"], 2))
		for r in values.get("references") or []
	]


class DocumentPaymentsTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# the mappers date the new documents today
		ensure_fiscal_year(COMPANY, today())

	def setUp(self):
		super().setUp()
		self.use_company_settings(
			**PE_SETTINGS,
			**settings_for_documents(default_outgoing_cheque_bank_account=self.data.egp.bank_account),
		)

	def new_book(self, currency: str = "EGP"):
		"""The only active cheque book of the bank (a random range: leaves never used before)."""
		bank = self.data.egp if currency == "EGP" else self.data.usd
		isolate_cheque_books(self, bank.bank_account)
		start = random.randint(10_000_000, 90_000_000)
		book = frappe.get_doc(
			{
				"doctype": "Cheque Book",
				"company": COMPANY,
				"bank_account": bank.bank_account,
				"start_no": start,
				"end_no": start + 99,
			}
		).insert()
		return book

	def map_cheque_out(self) -> str:
		egp = self.data.egp
		mode = ensure_mode(CHEQUE_OUT_MODE, account=egp.bank_gl)
		set_mode_map(
			self,
			[{"bank_account": egp.bank_account, "method": "Cheque Out", "mode_of_payment": mode}],
		)
		return mode

	def count(self, doctype: str) -> int:
		return frappe.db.count(doctype)


class TestChequeMapper(DocumentPaymentsTestCase):
	def test_cheque_from_purchase_invoice(self):
		egp = self.data.egp
		book = self.new_book()
		mode = self.map_cheque_out()
		pi = make_purchase_invoice(20000)
		before = self.count("PDC Cheque")

		values = map_cheque("Purchase Invoice", pi.name)

		self.assertEqual(self.count("PDC Cheque"), before)
		self.assertEqual(
			(values["doctype"], values.get("__islocal"), values.get("name")), ("PDC Cheque", 1, None)
		)
		self.assertEqual(
			(values["direction"], values["naming_series"], values["party_type"], values["party"]),
			("Outgoing", "PDC-OUT-.YYYY.-.#####", "Supplier", SUPPLIER),
		)
		self.assertEqual(
			(flt(values["amount"]), values["currency"], flt(values["exchange_rate"])), (20000, "EGP", 1)
		)
		self.assertEqual(getdate(values["issue_date"]), getdate(today()))
		self.assertEqual(getdate(values["original_due_date"]), max(getdate(pi.due_date), getdate(today())))
		self.assertEqual((values["bank_account"], values["cheque_book"]), (egp.bank_account, book.name))
		self.assertFalse(values.get("cheque_no"))
		self.assertEqual(refs(values), [("Purchase Invoice", pi.name, 20000)])
		self.assertEqual(values.get("cost_center"), pi.get("cost_center"))
		self.assertEqual(values["mode_of_payment"], mode)

		# the minimal entry: the returned document saves as it is and takes the next leaf of the book
		cheque = frappe.get_doc(values)
		cheque.insert()
		self.assertEqual(cheque.cheque_no, book.format_leaf(book.start_no))
		cheque.deliver(handover_date=today())
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Delivered", "Party Posted"))
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(self.data.payable, 20000, 0, SUPPLIER, today()),
				(egp.pdc_payable, 0, 20000, None, today()),
			],
		)
		self.assertEqual(
			frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "mode_of_payment"), mode
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

	def test_cheque_from_sales_invoice_and_order(self):
		egp = self.data.egp
		self.use_company_settings(default_incoming_cheque_bank_account=egp.bank_account)
		si = make_sales_invoice(3000)
		values = map_cheque("Sales Invoice", si.name)
		self.assertEqual(
			(values["direction"], values["naming_series"], values["party"], flt(values["amount"])),
			("Incoming", "PDC-IN-.YYYY.-.#####", CUSTOMER, 3000),
		)
		self.assertEqual(values["bank_account"], egp.bank_account)
		self.assertFalse(values.get("cheque_book"))
		values["cheque_no"] = unique_cheque_no()
		cheque = frappe.get_doc(values).insert()
		cheque.receive(receipt_date=today())
		cheque.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(egp.pdc_receivable, 3000, 0, None, today()),
				(self.data.receivable, 0, 3000, CUSTOMER, today()),
			],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 0)

		# a Sales Order: an advance allocated to the order
		so = make_sales_order(4000)
		values = map_cheque("Sales Order", so.name)
		self.assertEqual((flt(values["amount"]), refs(values)), (4000, [("Sales Order", so.name, 4000)]))
		values["cheque_no"] = unique_cheque_no()
		cheque = frappe.get_doc(values).insert()
		cheque.receive(receipt_date=today())
		cheque.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[(egp.pdc_receivable, 4000, 0), (self.data.receivable, 0, 4000)],
		)
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "advance_paid")), 4000)

		# without an incoming default: the first mapped bank in the party currency
		self.use_company_settings(default_incoming_cheque_bank_account=None)
		expected = source_documents.default_cheque_bank(COMPANY, "Incoming", "EGP")
		self.assertTrue(expected)
		self.assertEqual(map_cheque("Sales Invoice", make_sales_invoice(100).name)["bank_account"], expected)

	def test_cheque_from_party_masters(self):
		employee = ensure_employee()
		for doctype, name, direction in (
			("Supplier", SUPPLIER, "Outgoing"),
			("Customer", CUSTOMER, "Incoming"),
			("Employee", employee, "Outgoing"),
		):
			with self.subTest(doctype=doctype):
				values = map_cheque(doctype, name, company=COMPANY)
				self.assertEqual(
					(values["direction"], values["party_type"], values["party"], values["company"]),
					(direction, doctype, name, COMPANY),
				)
				self.assertFalse(values.get("amount"))
				self.assertEqual(values.get("references"), [])

		# without `company`: the user's default company (ERPNext's "company" default, as the desk's company
		# selector sets it; the "Company" key is a User Permission key and is not read back by itself)
		previous = frappe.defaults.get_user_default("company")
		frappe.defaults.set_user_default("company", COMPANY)
		if previous:
			self.addCleanup(frappe.defaults.set_user_default, "company", previous)
		else:
			self.addCleanup(frappe.defaults.clear_user_default, "company")
		self.assertEqual(map_cheque("Supplier", SUPPLIER)["company"], COMPANY)
		self.assertEqual(build_transfer("Customer", CUSTOMER)["company"], COMPANY)
		self.assertEqual(map_security("Customer", CUSTOMER)["company"], COMPANY)

	def test_currency(self):
		usd, egp = self.data.usd, self.data.egp
		isolate_cheque_books(self, usd.bank_account)
		# a USD supplier: the EGP default bank cannot settle it, the USD mapping bank can
		usd_pi = make_usd_purchase_invoice(1000)
		values = map_cheque("Purchase Invoice", usd_pi.name)
		self.assertEqual((values["bank_account"], values["currency"]), (usd.bank_account, "USD"))
		self.assertEqual(flt(values["exchange_rate"]), 50)
		self.assertEqual(
			(flt(values["amount"]), refs(values)), (1000, [("Purchase Invoice", usd_pi.name, 1000)])
		)

		# an EGP supplier and a USD default bank: the amount in USD, the allocation in EGP
		self.use_company_settings(default_outgoing_cheque_bank_account=usd.bank_account)
		pi = make_purchase_invoice(20000)
		values = map_cheque("Purchase Invoice", pi.name)
		self.assertEqual((values["currency"], flt(values["amount"])), ("USD", 400))
		self.assertEqual(refs(values), [("Purchase Invoice", pi.name, 20000)])

		# a USD supplier and only EGP banks mapped
		self.use_company_settings(default_outgoing_cheque_bank_account=egp.bank_account)
		set_pdc_accounts(self, [egp.bank_account])
		with self.assertRaisesRegex(frappe.ValidationError, "cannot settle the USD outstanding"):
			map_cheque("Purchase Invoice", usd_pi.name)

	def test_reserved_amount(self):
		pi = make_purchase_invoice(10000)
		make_pdc_cheque(amount=6000, invoice=pi)
		values = map_cheque("Purchase Invoice", pi.name)
		self.assertEqual((flt(values["amount"]), refs(values)), (4000, [("Purchase Invoice", pi.name, 4000)]))

		transfer = make_bank_transfer(
			amount=4000,
			execution_date=add_days(today(), 5),
			references=[
				{"reference_doctype": "Purchase Invoice", "reference_name": pi.name, "allocated_amount": 4000}
			],
		)
		self.assertEqual(transfer.status, "Scheduled")
		for mapper in (map_cheque, map_batch):
			with self.subTest(mapper=mapper.__name__):
				with self.assertRaisesRegex(
					frappe.ValidationError, "already covered by open cheques or scheduled transfers"
				):
					mapper("Purchase Invoice", pi.name)

	def test_journal_entry_sources(self):
		egp = self.data.egp
		expense = acc("Cost of Goods Sold")
		je = make_party_journal_entry("Supplier", SUPPLIER, 5000, expense)
		values = map_cheque("Journal Entry", je.name)
		self.assertEqual(
			(values["direction"], values["party_type"], values["party"], flt(values["amount"])),
			("Outgoing", "Supplier", SUPPLIER, 5000),
		)
		self.assertEqual(refs(values), [("Journal Entry", je.name, 5000)])
		values["cheque_no"] = unique_cheque_no()
		values["cheque_book"] = None
		cheque = frappe.get_doc(values).insert()
		cheque.deliver(handover_date=today())
		cheque.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[(self.data.payable, 5000, 0, SUPPLIER), (egp.pdc_payable, 0, 5000, None)],
		)
		self.assertEqual(get_ledger_open_amount("Journal Entry", je.name), 0)

		# two open parties: choose one
		two = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": today(),
				"accounts": [
					{
						"account": self.data.receivable,
						"party_type": "Customer",
						"party": CUSTOMER,
						"debit_in_account_currency": 3000,
						"cost_center": acc("Main"),
					},
					{
						"account": self.data.payable,
						"party_type": "Supplier",
						"party": SUPPLIER,
						"credit_in_account_currency": 3000,
						"cost_center": acc("Main"),
					},
				],
			}
		).insert()
		two.submit()
		with self.assertRaisesRegex(frappe.ValidationError, "several open parties"):
			map_cheque("Journal Entry", two.name)
		values = map_cheque("Journal Entry", two.name, party_type="Customer", party=CUSTOMER)
		self.assertEqual((values["direction"], flt(values["amount"])), ("Incoming", 3000))
		with self.assertRaisesRegex(frappe.ValidationError, "is not an open party"):
			map_cheque("Journal Entry", two.name, party_type="Supplier", party=USD_SUPPLIER)
		parties = {
			(p["party_type"], p["party"]): (p["direction"], flt(p["outstanding"]), p["supported"])
			for p in source_documents.get_journal_parties(two.name)
		}
		self.assertEqual(
			parties,
			{
				("Customer", CUSTOMER): ("Incoming", 3000, True),
				("Supplier", SUPPLIER): ("Outgoing", 3000, True),
			},
		)

		# a party row on another payable account than the party's default one
		other_payable = ensure_account("Treasury Creditors 7c", "Accounts Payable", "Payable")
		je = make_party_journal_entry("Supplier", SUPPLIER, 700, expense, party_account=other_payable)
		[row] = source_documents.get_journal_parties(je.name)
		self.assertEqual((row["account"], row["supported"]), (other_payable, False))
		with self.assertRaisesRegex(frappe.ValidationError, "uses account"):
			map_cheque("Journal Entry", je.name)

	def test_journal_party_on_two_accounts(self):
		# the Supplier is credited 5,000 on Creditors (its default) and debited 1,000 on another payable
		# account: the ledger outstanding (4,000) sums both, a Treasury Payment Entry would settle only
		# Creditors and leave the two accounts open by opposite amounts (F12): every mapper refuses it
		other_payable = ensure_account("Treasury Creditors 7c", "Accounts Payable", "Payable")
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": today(),
				"accounts": [
					{
						"account": self.data.payable,
						"party_type": "Supplier",
						"party": SUPPLIER,
						"credit_in_account_currency": 5000,
						"cost_center": acc("Main"),
					},
					{
						"account": other_payable,
						"party_type": "Supplier",
						"party": SUPPLIER,
						"debit_in_account_currency": 1000,
						"cost_center": acc("Main"),
					},
					{
						"account": acc("Cost of Goods Sold"),
						"debit_in_account_currency": 4000,
						"cost_center": acc("Main"),
					},
				],
			}
		).insert()
		je.submit()

		[row] = source_documents.get_journal_parties(je.name)
		self.assertEqual(
			(row["account"], row["accounts"], flt(row["outstanding"]), row["supported"]),
			(self.data.payable, [self.data.payable, other_payable], 4000, False),
		)
		for mapper, kwargs in (
			(map_cheque, {}),
			(map_cheque, {"party_type": "Supplier", "party": SUPPLIER}),
			(map_batch, {}),
			(build_transfer, {}),
		):
			with self.subTest(mapper=mapper.__name__, **kwargs):
				with self.assertRaisesRegex(
					source_documents.TreasuryPartyAccountError, "uses account"
				) as caught:
					mapper("Journal Entry", je.name, **kwargs)
				self.assertIn(other_payable, str(caught.exception))

	def test_party_master_company_and_status(self):
		# an Employee's documents are prepared for the Employee's own company, whatever the user's default
		employee = ensure_employee()
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		self.assertEqual(map_cheque("Employee", employee, company=OTHER_COMPANY)["company"], COMPANY)
		self.assertEqual(map_batch("Employee", employee, company=OTHER_COMPANY)["company"], COMPANY)
		self.assertEqual(build_transfer("Employee", employee, company=OTHER_COMPANY)["company"], COMPANY)
		self.assertEqual(map_security("Employee", employee, company=OTHER_COMPANY)["company"], COMPANY)

		# disabled / inactive masters get nothing (the buttons are hidden; the server refuses too)
		# restore explicitly: the rollback does not reset the cached documents other tests read
		status = frappe.db.get_value("Employee", employee, "status")
		self.addCleanup(frappe.db.set_value, "Employee", employee, "status", status)
		self.addCleanup(frappe.db.set_value, "Supplier", SUPPLIER, "disabled", 0)
		frappe.db.set_value("Supplier", SUPPLIER, "disabled", 1)
		frappe.db.set_value("Employee", employee, "status", "Left")
		for mapper, doctype, name, status in (
			(map_cheque, "Supplier", SUPPLIER, "Disabled"),
			(map_batch, "Supplier", SUPPLIER, "Disabled"),
			(build_transfer, "Supplier", SUPPLIER, "Disabled"),
			(map_security, "Supplier", SUPPLIER, "Disabled"),
			(map_cheque, "Employee", employee, "Left"),
			(build_transfer, "Employee", employee, "Left"),
			(map_security, "Employee", employee, "Left"),
		):
			with self.subTest(mapper=mapper.__name__, doctype=doctype):
				with self.assertRaisesRegex(frappe.ValidationError, f"is {status}"):
					mapper(doctype, name, company=COMPANY)

	def test_invalid_sources(self):
		egp = self.data.egp
		draft = make_purchase_invoice(100, submit=False)
		with self.assertRaisesRegex(frappe.ValidationError, "nothing outstanding"):
			map_cheque("Purchase Invoice", draft.name)

		paid = make_purchase_invoice(200)
		payment = get_payment_entry("Purchase Invoice", paid.name, bank_account=egp.bank_gl)
		payment.reference_no, payment.reference_date = "7C-PAID", today()
		payment.insert(ignore_permissions=True)
		payment.submit()
		with self.assertRaisesRegex(frappe.ValidationError, "nothing outstanding"):
			map_cheque("Purchase Invoice", paid.name)

		original = make_purchase_invoice(300)
		debit_note = make_return_doc("Purchase Invoice", original.name)
		debit_note.insert(ignore_permissions=True)
		debit_note.submit()
		with self.assertRaisesRegex(frappe.ValidationError, "is a return"):
			map_cheque("Purchase Invoice", debit_note.name)

		held = make_purchase_invoice(400)
		frappe.db.set_value("Purchase Invoice", held.name, "on_hold", 1)
		with self.assertRaisesRegex(frappe.ValidationError, "is on hold"):
			map_cheque("Purchase Invoice", held.name)

		so = make_sales_order(500)
		frappe.db.set_value("Sales Order", so.name, "status", "Closed")
		with self.assertRaisesRegex(frappe.ValidationError, "is Closed"):
			map_cheque("Sales Order", so.name)

		po = make_purchase_order(600)
		frappe.db.set_value("Purchase Order", po.name, "per_billed", 100)
		with self.assertRaisesRegex(frappe.ValidationError, "fully billed"):
			map_cheque("Purchase Order", po.name)

		with self.assertRaisesRegex(frappe.ValidationError, "cannot be created from"):
			map_cheque("Item", "X")

	def test_permissions(self):
		ensure_user(NO_ROLES_USER, [])
		ensure_user(READER, ["Accounts User"])  # reads PDC Cheques, creates none
		ensure_user(TREASURY_USER, ["Treasury User"])  # creates them, reads no invoice / order
		pi = make_purchase_invoice(1000)
		so = make_sales_order(1000)
		cases = (
			(map_cheque, "Purchase Invoice", pi.name),
			(map_batch, "Purchase Invoice", pi.name),
			(map_security, "Sales Order", so.name),
		)
		for user in (NO_ROLES_USER, READER, TREASURY_USER):
			for mapper, doctype, name in cases:
				with self.subTest(user=user, mapper=mapper.__name__), self.set_user(user):
					self.assertRaises(frappe.PermissionError, mapper, doctype, name)

		# a user restricted to another company: the company probe
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(OTHER_COMPANY_USER, ["Treasury User", "Purchase User", "Sales User"])
		add_user_permission(OTHER_COMPANY_USER, "Company", OTHER_COMPANY)
		for mapper in (map_cheque, map_batch, map_security):
			with self.subTest(mapper=mapper.__name__), self.set_user(OTHER_COMPANY_USER):
				with self.assertRaisesRegex(frappe.PermissionError, "not permitted to create"):
					mapper("Supplier", SUPPLIER, company=COMPANY)

	def test_idempotent_mapping(self):
		self.new_book()
		pi = make_purchase_invoice(12000)
		so = make_sales_order(5000)
		counts = {d: self.count(d) for d in ("PDC Cheque", "Cheque Batch", "Security Cheque")}
		for mapper, doctype, name in (
			(map_cheque, "Purchase Invoice", pi.name),
			(map_batch, "Purchase Invoice", pi.name),
			(map_security, "Sales Order", so.name),
		):
			with self.subTest(mapper=mapper.__name__):
				self.assertEqual(mapper(doctype, name), mapper(doctype, name))
		self.assertEqual({d: self.count(d) for d in counts}, counts)


class TestBatchMapper(DocumentPaymentsTestCase):
	def test_batch_from_invoice_with_book(self):
		egp = self.data.egp
		self.use_company_settings(default_installment_count=3)
		book = self.new_book()
		pi = make_purchase_invoice(12000)
		values = map_batch("Purchase Invoice", pi.name)

		self.assertEqual((values["doctype"], values.get("__islocal")), ("Cheque Batch", 1))
		self.assertEqual(
			(values["direction"], values["purpose"], values["cheque_book"], values["allocation_method"]),
			("Outgoing", "Installments", book.name, "Manual"),
		)
		self.assertEqual((values["source_doctype"], values["source_name"]), ("Purchase Invoice", pi.name))
		self.assertEqual(
			(
				values["gen_count"],
				values["gen_frequency"],
				values["gen_amount_mode"],
				flt(values["gen_total"]),
			),
			(3, "Monthly", "Split Total", 12000),
		)
		first = max(getdate(pi.due_date), getdate(today()))
		self.assertEqual(getdate(values["gen_first_due_date"]), first)
		dates = batch.schedule_dates(first, 3, "Monthly", 0, values["gen_month_end"])
		self.assertEqual(
			[(getdate(r["due_date"]), flt(r["amount"])) for r in values["cheques"]],
			[(d, 4000) for d in dates],
		)
		self.assertEqual(
			[
				(r["reference_doctype"], r["reference_name"], flt(r["allocated_amount"]))
				for r in values["cheques"]
			],
			[("Purchase Invoice", pi.name, 4000)] * 3,
		)

		doc = frappe.get_doc(values)
		doc.insert()
		self.assertEqual(flt(doc.total_amount), 12000)
		doc.submit()
		result = doc.deliver_all(handover_date=today())
		self.assertEqual((len(result["succeeded"]), result["failed"]), (3, []))
		cheques = batch.get_batch_cheques(doc.name, fields=["name", "party_payment_entry", "cheque_no"])
		self.assertEqual(len(cheques), 3)
		for cheque in cheques:
			assert_gl_entries(
				self,
				"Payment Entry",
				cheque.party_payment_entry,
				[
					(self.data.payable, 4000, 0, SUPPLIER, today()),
					(egp.pdc_payable, 0, 4000, None, today()),
				],
			)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

	def test_batch_incoming_generate_allocates(self):
		self.use_company_settings(default_installment_count=3)
		si = make_sales_invoice(10000)
		values = map_batch("Sales Invoice", si.name)
		self.assertEqual(values["cheques"], [])
		self.assertEqual((values["source_doctype"], values["source_name"]), ("Sales Invoice", si.name))
		self.assertEqual(flt(values["gen_total"]), 10000)

		doc = frappe.get_doc(values)
		doc.gen_first_cheque_no = "A100"
		result = doc.generate_rows()
		self.assertEqual(flt(result["allocated"]), 10000)
		rows = sorted(doc.cheques, key=lambda r: (getdate(r.due_date), r.idx))
		self.assertEqual([flt(r.amount) for r in rows], [3333.33, 3333.33, 3333.34])
		self.assertEqual([r.cheque_no for r in rows], ["A100", "A101", "A102"])
		self.assertEqual(
			[(r.reference_doctype, r.reference_name, flt(r.allocated_amount)) for r in rows],
			[("Sales Invoice", si.name, flt(r.amount)) for r in rows],
		)
		doc.insert()
		self.assertEqual(doc.source_name, si.name)

	def test_batch_source_validation(self):
		si = make_sales_invoice(3000)

		def new_batch(**values):
			doc = frappe.get_doc(map_batch("Sales Invoice", si.name))
			doc.gen_first_cheque_no = unique_cheque_no() + "1"
			doc.generate_rows()
			doc.update(values)
			return doc

		other_customer = ensure_customer("_Test Treasury Customer 7c")
		with self.assertRaisesRegex(frappe.ValidationError, "does not belong to"):
			new_batch(party=other_customer).insert()

		# Frappe validates the Dynamic Link before the controller runs (`Document._validate_links` precedes
		# `before_insert` / `validate`): an existing document of an unsupported type reaches our check on save ...
		with self.assertRaisesRegex(frappe.ValidationError, "Customer cannot be a Cheque Batch source"):
			new_batch(source_doctype="Customer", source_name=CUSTOMER).insert()
		# ... a name that is not a document of that type is refused by Frappe's link check first
		with self.assertRaises(frappe.LinkValidationError):
			new_batch(source_doctype="Item").insert()
		with self.assertRaisesRegex(frappe.ValidationError, "Item cannot be a Cheque Batch source"):
			batch.validate_source(new_batch(source_doctype="Item"))

		# a source name without its type: the pair is cleared
		doc = new_batch(source_doctype=None)
		self.assertIsNone(batch.validate_source(doc))
		self.assertEqual((doc.source_doctype, doc.source_name), (None, None))

		# a cancelled source: Frappe refuses the cancelled link on save; our check refuses it too
		doc = new_batch()
		si.reload()
		si.cancel()
		with self.assertRaisesRegex(frappe.CancelledLinkError, "Cannot link cancelled document"):
			doc.insert()
		with self.assertRaisesRegex(frappe.ValidationError, "does not belong to"):
			batch.validate_source(doc)

		# another company (the batch's company is compared, it need not exist for the check)
		other = frappe._dict(
			source_doctype="Sales Invoice",
			source_name=make_sales_invoice(100).name,
			company=OTHER_COMPANY,
			party_type="Customer",
			party=CUSTOMER,
		)
		with self.assertRaisesRegex(frappe.ValidationError, "does not belong to"):
			batch.validate_source(other)

	def test_batch_source_needs_read_permission(self):
		# the source fields are read only in the form only: a user who cannot read the invoice learns nothing
		# about it through Generate Cheques (its outstanding) or a save (whose it is)
		ensure_user(TREASURY_USER, ["Treasury User"])  # reads no Sales Invoice
		ensure_user(TREASURY_READER, ["Treasury User", "Accounts User"])
		si = make_sales_invoice(3000)
		values = map_batch("Sales Invoice", si.name)
		values["gen_first_cheque_no"] = unique_cheque_no() + "1"

		with self.set_user(TREASURY_USER):
			doc = frappe.get_doc(copy.deepcopy(values))
			self.assertRaises(frappe.PermissionError, doc.generate_rows)
			self.assertRaises(frappe.PermissionError, batch.validate_source, doc)
		with self.set_user(TREASURY_READER):
			doc = frappe.get_doc(copy.deepcopy(values))
			self.assertEqual(flt(doc.generate_rows()["allocated"]), 3000)
			doc.insert()

		# an unchanged source is not checked again on save: another user may still edit the draft ...
		saved = frappe.get_doc("Cheque Batch", doc.name)
		saved.load_doc_before_save()  # what `save` does before `validate`
		with self.set_user(TREASURY_USER):
			self.assertEqual(batch.validate_source(saved).name, si.name)
			# ... but Generate Cheques reads the source's outstanding: checked every time
			self.assertRaises(frappe.PermissionError, saved.generate_rows)

	def test_batch_journal_source_checked_on_save(self):
		# a Journal Entry source set by hand (API): an open party row, on the party's default account
		expense = acc("Cost of Goods Sold")
		self.use_company_settings(auto_select_cheque_book=0)  # rows numbered by hand
		good = make_party_journal_entry("Supplier", SUPPLIER, 900, expense)
		other_payable = ensure_account("Treasury Creditors 7c", "Accounts Payable", "Payable")
		wrong_account = make_party_journal_entry(
			"Supplier", SUPPLIER, 800, expense, party_account=other_payable
		)
		# settled in ERPNext by another Journal Entry against it: the Supplier row is no longer open
		paid = make_party_journal_entry("Supplier", SUPPLIER, 600, expense)
		settlement = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": today(),
				"accounts": [
					{
						"account": self.data.payable,
						"party_type": "Supplier",
						"party": SUPPLIER,
						"debit_in_account_currency": 600,
						"reference_type": "Journal Entry",
						"reference_name": paid.name,
						"cost_center": acc("Main"),
					},
					{"account": expense, "credit_in_account_currency": 600, "cost_center": acc("Main")},
				],
			}
		).insert()
		settlement.submit()
		self.assertEqual(source_documents.get_journal_parties(paid.name), [])

		def batch_from(source_name):
			doc = frappe.get_doc(map_batch("Journal Entry", good.name))
			doc.gen_first_cheque_no = unique_cheque_no() + "1"
			doc.generate_rows()
			doc.source_name = source_name
			return doc

		with self.assertRaisesRegex(source_documents.TreasuryPartyAccountError, "uses account"):
			batch_from(wrong_account.name).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "is not an open party"):
			batch_from(paid.name).insert()
		self.assertEqual(batch_from(good.name).insert().source_name, good.name)

	def test_batch_generate_when_source_covered(self):
		# the source is already covered by an open cheque: the cheques are still generated, all unallocated
		# (an advance), with an orange message instead of an error
		self.use_company_settings(default_installment_count=2)
		si = make_sales_invoice(3000)
		doc = frappe.get_doc(map_batch("Sales Invoice", si.name))
		make_pdc_cheque("Incoming", amount=3000, invoice=si)
		doc.gen_first_cheque_no = unique_cheque_no() + "1"
		frappe.local.message_log = []

		result = doc.generate_rows()

		self.assertEqual((result["rows"], flt(result["allocated"])), (2, 0))
		self.assertTrue(all(not r.reference_name and not flt(r.allocated_amount) for r in doc.cheques))
		messages = [m for m in frappe.local.message_log if isinstance(m, dict)]
		self.assertTrue(
			any(
				"already covered" in str(m.get("message")) and m.get("indicator") == "orange"
				for m in messages
			)
		)

	def test_batch_split_currency_no_allocation(self):
		usd = self.data.usd
		isolate_cheque_books(self, usd.bank_account)
		self.use_company_settings(
			default_outgoing_cheque_bank_account=usd.bank_account, default_installment_count=2
		)
		pi = make_purchase_invoice(12000)
		values = map_batch("Purchase Invoice", pi.name)
		self.assertEqual((values["currency"], flt(values["gen_total"]), values["cheques"]), ("USD", 240, []))

		doc = frappe.get_doc(values)
		doc.gen_first_cheque_no = "U100"
		frappe.local.message_log = []
		result = doc.generate_rows()
		self.assertEqual(flt(result["allocated"]), 0)
		self.assertTrue(all(not r.reference_name for r in doc.cheques))
		self.assertIn(
			"Allocate the cheques by hand",
			" ".join(str(m.get("message") if isinstance(m, dict) else m) for m in frappe.local.message_log),
		)

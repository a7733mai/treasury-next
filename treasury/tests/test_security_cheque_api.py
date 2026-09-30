"""Phase 7c: `make_security_cheque` builds an unsaved Security Cheque for a quotation, order or party master;
it never posts (hard rule 4) (spec 7c §3.4, §4 I, §7)."""

import random

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate, today

from treasury.security_cheques.api import make_security_cheque as map_security
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	SUPPLIER,
	TreasuryTestMixin,
	ensure_employee,
	ensure_fiscal_year,
	ensure_user,
	get_voucher_count,
	isolate_cheque_books,
	make_purchase_order,
	make_quotation,
	make_sales_order,
	make_supplier_quotation,
	settings_for_documents,
)

NO_ROLES_USER = "treasury-7c-sec-noroles@example.com"
READER = "treasury-7c-sec-reader@example.com"


class TestSecurityChequeAPI(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_fiscal_year(COMPANY, today())

	def setUp(self):
		super().setUp()
		self.use_company_settings(
			**settings_for_documents(default_outgoing_cheque_bank_account=self.data.egp.bank_account)
		)

	def new_book(self):
		isolate_cheque_books(self, self.data.egp.bank_account)
		start = random.randint(10_000_000, 90_000_000)
		return frappe.get_doc(
			{
				"doctype": "Cheque Book",
				"company": COMPANY,
				"bank_account": self.data.egp.bank_account,
				"start_no": start,
				"end_no": start + 99,
			}
		).insert()

	def test_tender_bond_from_quotation(self):
		self.use_company_settings(tender_bond_percent=5)
		book = self.new_book()
		quotation = make_quotation(100000)
		before = frappe.db.count("Security Cheque")
		values = map_security("Quotation", quotation.name)

		self.assertEqual(frappe.db.count("Security Cheque"), before)
		self.assertEqual((values["doctype"], values.get("__islocal")), ("Security Cheque", 1))
		self.assertEqual(
			(values["direction"], values["purpose"], values["party_type"], values["party"]),
			("Outgoing", "Tender", "Customer", CUSTOMER),
		)
		self.assertEqual((flt(values["amount"]), flt(values["required_amount"])), (5000, 5000))
		self.assertEqual(getdate(values["expiry_date"]), getdate(quotation.valid_till))
		self.assertEqual((values["reference_doctype"], values["reference_name"]), ("Quotation", quotation.name))
		self.assertEqual(
			(values["bank_account"], values["cheque_book"], values["currency"]),
			(self.data.egp.bank_account, book.name, "EGP"),
		)
		self.assertFalse(values.get("cheque_no"))

		cheque = frappe.get_doc(values)
		cheque.insert()
		self.assertEqual(cheque.cheque_no, book.format_leaf(book.start_no))
		cheque.issue(handover_date=today())
		cheque.reload()
		self.assertEqual(cheque.status, "Issued")
		# a security cheque never posts
		self.assertEqual(get_voucher_count("Security Cheque", cheque.name), 0)

	def test_lead_quotation(self):
		quotation = make_quotation(1000, quotation_to="Lead")
		values = map_security("Quotation", quotation.name)
		self.assertEqual(
			(values["party_type"], values.get("party"), values["party_name_other"]),
			("Other", None, quotation.customer_name or quotation.party_name),
		)

	def test_supplier_quotation_order_and_masters(self):
		employee = ensure_employee()
		cases = (
			("Supplier Quotation", make_supplier_quotation(2000).name, "Incoming", "Tender", "Supplier", SUPPLIER),
			("Sales Order", make_sales_order(3000).name, "Outgoing", "Contract Performance", "Customer", CUSTOMER),
			("Purchase Order", make_purchase_order(4000).name, "Incoming", "Contract Performance", "Supplier", SUPPLIER),
			("Customer", CUSTOMER, "Incoming", "Other", "Customer", CUSTOMER),
			("Supplier", SUPPLIER, "Incoming", "Other", "Supplier", SUPPLIER),
			("Employee", employee, "Incoming", "Employee Custody", "Employee", employee),
		)
		for doctype, name, direction, purpose, party_type, party in cases:
			with self.subTest(doctype=doctype):
				values = map_security(doctype, name, company=COMPANY)
				self.assertEqual(
					(values["direction"], values["purpose"], values["party_type"], values["party"]),
					(direction, purpose, party_type, party),
				)
				self.assertEqual(values["company"], COMPANY)
				# tender_bond_percent = 0: the user enters the amount
				self.assertFalse(flt(values.get("amount")))
				master = doctype in ("Customer", "Supplier", "Employee")
				self.assertEqual(values.get("reference_name"), None if master else name)
				if direction == "Incoming":
					self.assertFalse(values.get("bank_account"))
					self.assertEqual(values["currency"], "EGP")

	def test_cancelled_and_permissions(self):
		quotation = make_quotation(1000, submit=True)
		quotation.cancel()
		with self.assertRaisesRegex(frappe.ValidationError, "is cancelled"):
			map_security("Quotation", quotation.name)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be created from"):
			map_security("Sales Invoice", "X")

		ensure_user(NO_ROLES_USER, [])
		ensure_user(READER, ["Accounts User"])  # reads Security Cheques, creates none
		draft = make_quotation(1000)
		for user in (NO_ROLES_USER, READER):
			with self.subTest(user=user), self.set_user(user):
				self.assertRaises(frappe.PermissionError, map_security, "Quotation", draft.name)

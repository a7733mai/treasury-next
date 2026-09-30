"""Phase 7b: `make_bank_transfer` builds an unsaved Bank Transfer for an invoice, order or party (spec 7b §3.5,
§7 G)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from treasury.bank_transfers.api import make_bank_transfer as build_transfer
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	SUPPLIER,
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	acc,
	ensure_fiscal_year,
	ensure_party_bank_account,
	ensure_user,
	make_iban,
	make_party_journal_entry,
	make_pdc_cheque,
	make_purchase_invoice,
	make_sales_invoice,
	make_sales_order,
	make_usd_purchase_invoice,
)

NO_ACCESS_USER = "treasury-transfer-api-noaccess@example.com"
READER = "treasury-transfer-api-reader@example.com"
TREASURY_USER = "treasury-transfer-api-user@example.com"


class TestBankTransferAPI(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(NO_ACCESS_USER, [])
		# may read Bank Transfers but not create them
		ensure_user(READER, ["Accounts User"])
		# may create Bank Transfers, but holds no permission on Purchase Invoice
		ensure_user(TREASURY_USER, ["Treasury User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS, default_normal_bank_account=self.data.egp.bank_account)

	def test_bank_falls_back_to_the_default_cheque_bank(self):
		# no default bank for the method: the default outgoing cheque bank, so the transfer opens with a bank
		egp = self.data.egp.bank_account
		self.use_company_settings(
			default_fawran_bank_account=None, default_outgoing_cheque_bank_account=egp
		)
		pi = make_purchase_invoice(1500)
		self.assertEqual(build_transfer("Purchase Invoice", pi.name, method="Fawran")["bank_account"], egp)

	def test_from_purchase_invoice(self):
		fawran_bank = self.data.egp.bank_account
		self.use_company_settings(default_transfer_method="Fawran", default_fawran_bank_account=fawran_bank)
		bank_account = ensure_party_bank_account("Supplier", SUPPLIER, iban=make_iban("OTHB"))
		pi = make_purchase_invoice(20000)
		values = build_transfer("Purchase Invoice", pi.name)

		self.assertEqual(values["doctype"], "Bank Transfer")
		self.assertFalse(
			frappe.db.exists("Bank Transfer", {"party": SUPPLIER, "amount": 20000, "docstatus": 0})
		)
		self.assertEqual(
			(values["transfer_type"], values["party_type"], values["party"], values["amount"]),
			("Party Payment", "Supplier", SUPPLIER, 20000),
		)
		self.assertEqual((values["method"], values["bank_account"]), ("Fawran", fawran_bank))
		self.assertEqual(values["party_bank_account"], bank_account)
		self.assertEqual(values.get("cost_center"), pi.get("cost_center"))
		self.assertEqual(
			[
				(r["reference_doctype"], r["reference_name"], r["allocated_amount"])
				for r in values["references"]
			],
			[("Purchase Invoice", pi.name, 20000)],
		)
		# an explicit method takes that method's default bank
		usd_bank = self.data.usd.bank_account
		self.use_company_settings(default_swift_bank_account=usd_bank)
		self.assertEqual(build_transfer("Purchase Invoice", pi.name, "SWIFT")["bank_account"], usd_bank)

		# the minimal entry path: the returned document inserts and submits as it is
		doc = frappe.get_doc(values)
		doc.insert()
		doc.submit()
		self.assertEqual(doc.status, "Sent")

	def test_other_sources(self):
		usd = self.data.usd
		self.use_company_settings(default_normal_bank_account=usd.bank_account)
		usd_pi = make_usd_purchase_invoice(1000)
		self.assertEqual(build_transfer("Purchase Invoice", usd_pi.name)["amount"], 1000)

		self.use_company_settings(default_normal_bank_account=self.data.egp.bank_account)
		si = make_sales_invoice(3000)
		values = build_transfer("Sales Invoice", si.name)
		self.assertEqual(
			(values["transfer_type"], values["party"], values["amount"]), ("Party Receipt", CUSTOMER, 3000)
		)

		so = make_sales_order(4000)
		self.assertEqual(build_transfer("Sales Order", so.name)["amount"], 4000)

		values = build_transfer("Supplier", SUPPLIER, company=COMPANY)
		self.assertEqual((values["party"], values["references"], values["company"]), (SUPPLIER, [], COMPANY))
		self.assertFalse(values.get("amount"))
		values = build_transfer("Customer", CUSTOMER, company=COMPANY)
		self.assertEqual((values["transfer_type"], values["party_type"]), ("Party Receipt", "Customer"))

	def test_errors(self):
		pi = make_purchase_invoice(500)
		paid = make_purchase_invoice(600)
		payment = frappe.get_doc(build_transfer("Purchase Invoice", paid.name))
		payment.beneficiary_iban = make_iban("OTHB")
		payment.insert()
		payment.submit()
		with self.assertRaisesRegex(frappe.ValidationError, "nothing outstanding"):
			build_transfer("Purchase Invoice", paid.name)
		draft = make_purchase_invoice(700, submit=False)
		with self.assertRaisesRegex(frappe.ValidationError, "nothing outstanding"):
			build_transfer("Purchase Invoice", draft.name)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be created from"):
			build_transfer("Item", "X")
		with self.assertRaisesRegex(frappe.ValidationError, "Unknown method"):
			build_transfer("Purchase Invoice", pi.name, "Cash")
		# no create permission on Bank Transfer; no read permission on the invoice
		for user in (READER, NO_ACCESS_USER, TREASURY_USER):
			with self.subTest(user=user), self.set_user(user):
				self.assertRaises(frappe.PermissionError, build_transfer, "Purchase Invoice", pi.name)

	# ------------------------------------------------------------------ Phase 7c

	def test_journal_entry_sources(self):
		ensure_fiscal_year(COMPANY, today())
		supplier_je = make_party_journal_entry("Supplier", SUPPLIER, 5000, acc("Cost of Goods Sold"))
		values = build_transfer("Journal Entry", supplier_je.name)
		self.assertEqual(
			(values["transfer_type"], values["party_type"], values["party"], values["amount"]),
			("Party Payment", "Supplier", SUPPLIER, 5000),
		)
		self.assertEqual(
			[(r["reference_doctype"], r["reference_name"]) for r in values["references"]],
			[("Journal Entry", supplier_je.name)],
		)
		customer_je = make_party_journal_entry("Customer", CUSTOMER, 700, acc("Sales"))
		# no default Fawran bank and no cheque bank either: no bank, and the company currency (never the
		# site's global default currency)
		self.use_company_settings(default_fawran_bank_account=None)
		with patch("treasury.bank_transfers.api._fallback_bank", return_value=None):
			values = build_transfer("Journal Entry", customer_je.name, "Fawran")
		self.assertEqual(
			(values["transfer_type"], values["party"], values["amount"], values["method"]),
			("Party Receipt", CUSTOMER, 700, "Fawran"),
		)
		self.assertEqual(
			(values["bank_account"], values["currency"], values["exchange_rate"]), (None, "EGP", 1)
		)

	def test_reserved_amount(self):
		pi = make_purchase_invoice(10000)
		make_pdc_cheque(amount=6000, invoice=pi)
		values = build_transfer("Purchase Invoice", pi.name)
		self.assertEqual(values["amount"], 4000)
		self.assertEqual(values["references"][0]["allocated_amount"], 4000)

	def test_hrms_sources_gated(self):
		with patch("treasury.utils.hrms.hrms_installed", return_value=False):
			for doctype in ("Expense Claim", "Employee Advance"):
				with self.subTest(doctype=doctype):
					self.assertRaisesRegex(
						frappe.ValidationError, f"{doctype} needs the HRMS app", build_transfer, doctype, "X"
					)

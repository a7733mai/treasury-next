# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.tests.utils import (
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	day,
	ensure_account,
	ensure_bank_account,
	make_pdc_cheque,
	make_purchase_invoice,
	make_sales_invoice,
	unique_cheque_no,
)

# Fixtures are built by treasury.tests.utils; don't let Frappe pull ERPNext's global test records.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Company",
	"Account",
	"Bank Account",
	"Cheque Book",
	"Currency",
	"DocType",
	"Payment Entry",
	"Journal Entry",
	"PDC Cheque",
	"User",
	"Print Format",
]


class TestPDCCheque(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	def test_defaults_on_insert(self):
		cheque = make_pdc_cheque(amount=1234.5, issue_date=day(0), due_date=day(15))
		self.assertTrue(cheque.name.startswith("PDC-OUT-"))
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Created", "Not Posted"))
		self.assertEqual(cheque.cycle, 1)
		self.assertEqual(getdate(cheque.effective_due_date), day(15))
		self.assertEqual((cheque.currency, cheque.exchange_rate), ("EGP", 1))
		self.assertEqual(cheque.party_name, SUPPLIER)
		self.assertTrue(cheque.amount_in_words)
		self.assertEqual([row.event for row in cheque.status_log], ["Created"])

		incoming = make_pdc_cheque("Incoming")
		self.assertTrue(incoming.name.startswith("PDC-IN-"))
		self.assertEqual(incoming.party_name, CUSTOMER)

	def test_currency_comes_from_bank_account(self):
		cheque = make_pdc_cheque(currency="USD", amount=10)
		self.assertEqual(cheque.currency, "USD")
		self.assertFalse(cheque.exchange_rate)  # fetched at the party posting date

	def test_amount_must_be_positive(self):
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, amount=0)
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, amount=-5)

	def test_party_type_is_restricted(self):
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, party_type="Employee")

	def test_due_date_not_before_issue_date(self):
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, issue_date=day(0), due_date=day(-1))

	def test_bank_account_rules(self):
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, bank_account=None)

		gl = ensure_account("Treasury Bank Unmapped", "Bank Accounts", "Bank")
		unmapped = ensure_bank_account("Treasury Unmapped Current", gl)
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, bank_account=unmapped)

		# incoming cheques may get their deposit bank later
		incoming = make_pdc_cheque("Incoming", bank_account=None)
		self.assertFalse(incoming.bank_account)
		self.assertRaises(frappe.ValidationError, incoming.receive, day(0))  # On Receipt needs the bank
		self.use_company_settings(incoming_party_posting="Manual")
		incoming.receive(receipt_date=day(0))
		incoming.reload()
		self.assertEqual(incoming.operational_status, "Received")

	def test_outgoing_cheque_no_is_unique_per_bank_account(self):
		cheque_no = unique_cheque_no()
		first = make_pdc_cheque(cheque_no=cheque_no)
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, cheque_no=cheque_no)

		# another bank account may use the same number
		make_pdc_cheque(cheque_no=cheque_no, currency="USD", amount=10)

		# incoming cheques are other people's cheques: no uniqueness
		make_pdc_cheque("Incoming", cheque_no=cheque_no)
		make_pdc_cheque("Incoming", cheque_no=cheque_no)

		# a cancelled cheque releases its number
		first.cancel_cheque(reason="Spoilt")
		make_pdc_cheque(cheque_no=cheque_no)

	def test_cheque_no_is_required(self):
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, cheque_no="")

	def test_reference_rules(self):
		pi = make_purchase_invoice(500)
		si = make_sales_invoice(500)
		# wrong direction
		self.assertRaises(frappe.ValidationError, make_pdc_cheque, amount=500, invoice=si)
		# another party's invoice
		other = frappe.db.get_value("Supplier", {"name": ["!=", SUPPLIER]}, "name")
		if other:
			self.assertRaises(frappe.ValidationError, make_pdc_cheque, amount=500, invoice=pi, party=other)
		# over-allocation
		self.assertRaises(
			frappe.ValidationError,
			make_pdc_cheque,
			amount=100,
			references=[
				{"reference_doctype": "Purchase Invoice", "reference_name": pi.name, "allocated_amount": 500}
			],
		)
		cheque = make_pdc_cheque(amount=500, invoice=pi)
		self.assertEqual(len(cheque.references), 1)

	def test_locked_fields_after_posting(self):
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-5), due_date=day(20))
		# before posting everything may still change
		cheque.amount = 900
		cheque.save()

		cheque.deliver(handover_date=day(-5))
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")

		cheque.amount = 2000
		self.assertRaises(frappe.ValidationError, cheque.save)
		cheque.reload()
		cheque.party = frappe.db.get_value("Supplier", {"name": ["!=", SUPPLIER]}, "name") or "X"
		self.assertRaises(frappe.ValidationError, cheque.save)
		cheque.reload()
		cheque.append(
			"references",
			{
				"reference_doctype": "Purchase Invoice",
				"reference_name": make_purchase_invoice(100).name,
				"allocated_amount": 100,
			},
		)
		self.assertRaises(frappe.ValidationError, cheque.save)

		# free fields remain editable
		cheque.reload()
		cheque.remarks = "Handed to the supplier's accountant"
		cheque.save()

	def test_posted_cheque_cannot_be_deleted(self):
		cheque = make_pdc_cheque()
		cheque.deliver(handover_date=day(0))
		self.assertRaises(frappe.ValidationError, frappe.delete_doc, "PDC Cheque", cheque.name)

		draft = make_pdc_cheque()
		frappe.delete_doc("PDC Cheque", draft.name)
		self.assertFalse(frappe.db.exists("PDC Cheque", draft.name))

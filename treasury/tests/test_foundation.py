import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate, today

from treasury.accounting import posting
from treasury.install import TREASURY_FIELDNAMES, TREASURY_ROLES, VOUCHER_INSERT_AFTER
from treasury.overrides import voucher_guard
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	SUPPLIER,
	TreasuryTestMixin,
	assert_gl_entries,
	get_account_balance,
	get_party_outstanding,
	make_purchase_invoice,
	make_sales_invoice,
)

SOURCE_DOCTYPE = "Treasury Company Settings"


class TestFoundation(TreasuryTestMixin, IntegrationTestCase):
	# ------------------------------------------------------------------ helpers

	def pay(self, event: str, amount: float = 1000, invoice=None, **kwargs) -> str:
		references = []
		if invoice:
			references = [
				{
					"reference_doctype": invoice.doctype,
					"reference_name": invoice.name,
					"allocated_amount": amount,
				}
			]
		return posting.create_payment_entry(
			source_doctype=SOURCE_DOCTYPE,
			source_name=self.data.settings,
			event=event,
			payment_type="Pay",
			company=COMPANY,
			posting_date=today(),
			party_type="Supplier",
			party=SUPPLIER,
			amount=amount,
			bank_account_head=self.data.egp.pdc_payable,
			reference_no=f"CHQ-{event}",
			reference_date=today(),
			references=references,
			**kwargs,
		)

	def clearing_je(self, event: str, amount: float = 1000, clearance_date=None) -> str:
		return posting.create_journal_entry(
			source_doctype=SOURCE_DOCTYPE,
			source_name=self.data.settings,
			event=event,
			company=COMPANY,
			posting_date=today(),
			accounts=[
				{"account": self.data.egp.pdc_payable, "debit_in_account_currency": amount},
				{"account": self.data.egp.bank_gl, "credit_in_account_currency": amount},
			],
			cheque_no=f"CHQ-{event}",
			cheque_date=today(),
			clearance_date=clearance_date,
		)

	# ------------------------------------------------------------------ install

	def test_roles_and_custom_fields_exist(self):
		for role in TREASURY_ROLES:
			self.assertTrue(frappe.db.exists("Role", role), role)
			self.assertEqual(frappe.db.get_value("Role", role, "desk_access"), 1)

		for doctype in VOUCHER_INSERT_AFTER:
			meta = frappe.get_meta(doctype)
			for fieldname in TREASURY_FIELDNAMES:
				self.assertTrue(meta.has_field(fieldname), f"{doctype}.{fieldname}")
			self.assertTrue(meta.get_field("treasury_source_name").read_only)
			self.assertTrue(meta.get_field("treasury_source_name").no_copy)
			self.assertEqual(meta.get_field("treasury_source_name").options, "treasury_source_doctype")
			self.assertEqual(
				frappe.db.get_value(
					"Custom Field", {"dt": doctype, "fieldname": "treasury_source_doctype"}, "module"
				),
				"Treasury Setup",
			)

	# ------------------------------------------------------------------ spike

	def test_payment_entry_pay_on_pdc_payable(self):
		pi = make_purchase_invoice(1000)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 1000)

		pe_name = self.pay("spike_pay", invoice=pi)
		pe = frappe.get_doc("Payment Entry", pe_name)

		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.paid_from, self.data.egp.pdc_payable)
		self.assertEqual(pe.paid_to, self.data.payable)
		self.assertEqual(pe.treasury_source_doctype, SOURCE_DOCTYPE)
		self.assertEqual(pe.treasury_source_name, self.data.settings)
		self.assertEqual(pe.treasury_event, "spike_pay")
		assert_gl_entries(
			self,
			"Payment Entry",
			pe_name,
			[
				(self.data.payable, 1000, 0, SUPPLIER, today()),
				(self.data.egp.pdc_payable, 0, 1000, None, today()),
			],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

	def test_payment_entry_receive_on_pdc_receivable(self):
		si = make_sales_invoice(750)
		pe_name = posting.create_payment_entry(
			source_doctype=SOURCE_DOCTYPE,
			source_name=self.data.settings,
			event="spike_receive",
			payment_type="Receive",
			company=COMPANY,
			posting_date=today(),
			party_type="Customer",
			party=CUSTOMER,
			amount=750,
			bank_account_head=self.data.egp.pdc_receivable,
			reference_no="CHQ-IN-1",
			reference_date=today(),
			references=[
				{"reference_doctype": "Sales Invoice", "reference_name": si.name, "allocated_amount": 750}
			],
		)
		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(pe.paid_from, self.data.receivable)
		self.assertEqual(pe.paid_to, self.data.egp.pdc_receivable)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe_name,
			[
				(self.data.egp.pdc_receivable, 750, 0, None),
				(self.data.receivable, 0, 750, CUSTOMER),
			],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 0)

	def test_reference_trimmed_to_outstanding(self):
		pi = make_purchase_invoice(400)
		pe_name = self.pay("trim_pay", amount=1000, invoice=pi)
		pe = frappe.get_doc("Payment Entry", pe_name)

		self.assertEqual(len(pe.references), 1)
		self.assertEqual(flt(pe.references[0].allocated_amount), 400)
		self.assertEqual(flt(pe.unallocated_amount), 600)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

	# ------------------------------------------------------------------ clearing JE

	def test_clearing_journal_entry_moves_bank_only_on_clearance(self):
		bank = self.data.egp.bank_gl
		pdc = self.data.egp.pdc_payable
		bank_before = get_account_balance(bank)
		pdc_before = get_account_balance(pdc)

		self.pay("clear_party")
		self.assertEqual(get_account_balance(bank), bank_before)
		self.assertEqual(get_account_balance(pdc), flt(pdc_before - 1000))

		je_name = self.clearing_je("clear_bank", clearance_date=today())
		assert_gl_entries(self, "Journal Entry", je_name, [(pdc, 1000, 0), (bank, 0, 1000)])
		self.assertEqual(getdate(frappe.db.get_value("Journal Entry", je_name, "clearance_date")), getdate(today()))
		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "treasury_event"), "clear_bank")
		self.assertEqual(get_account_balance(bank), flt(bank_before - 1000))
		self.assertEqual(get_account_balance(pdc), pdc_before)

	# ------------------------------------------------------------------ idempotency

	def test_payment_entry_is_idempotent(self):
		first = self.pay("idem_pay")
		second = self.pay("idem_pay")
		self.assertEqual(first, second)
		count = frappe.db.count(
			"Payment Entry",
			{
				"treasury_source_doctype": SOURCE_DOCTYPE,
				"treasury_source_name": self.data.settings,
				"treasury_event": "idem_pay",
			},
		)
		self.assertEqual(count, 1)

	def test_journal_entry_is_idempotent(self):
		first = self.clearing_je("idem_je")
		second = self.clearing_je("idem_je")
		self.assertEqual(first, second)
		self.assertEqual(frappe.db.count("Journal Entry", {"treasury_event": "idem_je"}), 1)

	def test_new_voucher_after_cancellation(self):
		first = self.pay("recreate_pay")
		posting.cancel_voucher("Payment Entry", first)
		second = self.pay("recreate_pay")
		self.assertNotEqual(first, second)
		self.assertEqual(frappe.db.get_value("Payment Entry", second, "docstatus"), 1)

	# ------------------------------------------------------------------ guard

	def test_guard_blocks_direct_cancel_and_trash(self):
		pi = make_purchase_invoice(500)
		pe_name = self.pay("guard_pay", amount=500, invoice=pi)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertRaises(frappe.ValidationError, pe.cancel)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe_name, "docstatus"), 1)
		self.assertRaises(frappe.ValidationError, voucher_guard.on_trash, pe)

		je = frappe.get_doc("Journal Entry", self.clearing_je("guard_je"))
		self.assertRaises(frappe.ValidationError, je.cancel)

		posting.cancel_voucher("Payment Entry", pe_name)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe_name, "docstatus"), 2)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 500)
		self.assertFalse(frappe.flags.treasury_allow_cancel)

	def test_guard_ignores_unmanaged_vouchers(self):
		pe = frappe.get_doc(
			{"doctype": "Payment Entry", "docstatus": 1, "treasury_source_doctype": None, "treasury_source_name": None}
		)
		voucher_guard.before_cancel(pe)
		voucher_guard.on_trash(pe)

	# ------------------------------------------------------------------ reversal

	def test_reversal_mirrors_journal_entry(self):
		original = self.clearing_je("rev_je_original", amount=300)
		reversal = posting.make_reversal("Journal Entry", original, today(), "rev_je_reversal")

		pdc, bank = self.data.egp.pdc_payable, self.data.egp.bank_gl
		assert_gl_entries(self, "Journal Entry", reversal, [(pdc, 0, 300), (bank, 300, 0)])
		rev = frappe.get_doc("Journal Entry", reversal)
		self.assertEqual(rev.reversal_of, original)
		self.assertEqual(rev.treasury_source_name, self.data.settings)
		self.assertEqual(rev.treasury_event, "rev_je_reversal")
		self.assertEqual(posting.make_reversal("Journal Entry", original, today(), "rev_je_reversal"), reversal)

	def test_reversal_of_payment_entry_restores_outstanding(self):
		pi = make_purchase_invoice(650)
		pe_name = self.pay("rev_pe_original", amount=650, invoice=pi)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

		reversal = posting.make_reversal("Payment Entry", pe_name, today(), "rev_pe_reversal")
		assert_gl_entries(
			self,
			"Journal Entry",
			reversal,
			[
				(self.data.payable, 0, 650, SUPPLIER),
				(self.data.egp.pdc_payable, 650, 0, None),
			],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 650)

	def test_linked_vouchers(self):
		pe_name = self.pay("linked_pay")
		je_name = self.clearing_je("linked_je")
		linked = {
			(v["doctype"], v["name"])
			for v in posting.get_linked_vouchers(SOURCE_DOCTYPE, self.data.settings)
		}
		self.assertIn(("Payment Entry", pe_name), linked)
		self.assertIn(("Journal Entry", je_name), linked)

	# ------------------------------------------------------------------ settings lookups

	def test_settings_lookups(self):
		mapping = posting.get_account_mapping(COMPANY, self.data.egp.bank_account)
		self.assertEqual(mapping.pdc_payable_account, self.data.egp.pdc_payable)
		self.assertEqual(posting.get_company_settings(COMPANY).name, COMPANY)
		self.assertRaises(frappe.ValidationError, posting.get_company_settings, "_Missing Company")
		self.assertRaises(frappe.ValidationError, posting.get_account_mapping, COMPANY, "_Missing Bank Account")

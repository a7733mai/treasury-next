import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate, today

from erpnext.setup.utils import get_exchange_rate
from treasury.accounting import posting
from treasury.install import TREASURY_FIELDNAMES, TREASURY_ROLES, VOUCHER_INSERT_AFTER
from treasury.overrides import voucher_guard
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	SUPPLIER,
	USD_RATE,
	USD_SUPPLIER,
	TreasuryTestMixin,
	acc,
	assert_gl_entries,
	get_account_balance,
	get_ledger_open_amount,
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
		values = {
			"source_doctype": SOURCE_DOCTYPE,
			"source_name": self.data.settings,
			"event": event,
			"payment_type": "Pay",
			"company": COMPANY,
			"posting_date": today(),
			"party_type": "Supplier",
			"party": SUPPLIER,
			"amount": amount,
			"bank_account_head": self.data.egp.pdc_payable,
			"reference_no": f"CHQ-{event}",
			"reference_date": today(),
			"references": references,
		}
		values.update(kwargs)
		return posting.create_payment_entry(**values)

	def clearing_je(self, event: str, amount: float = 1000, clearance_date=None, **kwargs) -> str:
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
			**kwargs,
		)

	def make_supplier_journal_entry(self, amount: float) -> str:
		"""A plain (non-Treasury) JE that credits the supplier, i.e. an open payable."""
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": today(),
				"accounts": [
					{
						"account": acc("Cost of Goods Sold"),
						"cost_center": acc("Main"),
						"debit_in_account_currency": amount,
					},
					{
						"account": self.data.payable,
						"party_type": "Supplier",
						"party": SUPPLIER,
						"cost_center": acc("Main"),
						"credit_in_account_currency": amount,
					},
				],
			}
		)
		je.insert(ignore_permissions=True)
		je.submit()
		return je.name

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
			self.assertEqual(meta.get_field("treasury_cycle").fieldtype, "Int")
			self.assertTrue(meta.get_field("treasury_cycle").read_only)
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
		self.assertEqual(pe.treasury_cycle, 1)
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

	# ------------------------------------------------------------------ allocation

	def test_reference_trimmed_to_outstanding(self):
		pi = make_purchase_invoice(400)
		pe_name = self.pay("trim_pay", amount=1000, invoice=pi)
		pe = frappe.get_doc("Payment Entry", pe_name)

		self.assertEqual(len(pe.references), 1)
		self.assertEqual(flt(pe.references[0].allocated_amount), 400)
		self.assertEqual(flt(pe.unallocated_amount), 600)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

	def test_references_trimmed_to_payment_amount(self):
		first, second = make_purchase_invoice(600), make_purchase_invoice(600)
		pe_name = self.pay(
			"trim_amount_pay",
			amount=1000,
			references=[
				{"reference_doctype": "Purchase Invoice", "reference_name": pi.name, "allocated_amount": 600}
				for pi in (first, second)
			],
		)
		pe = frappe.get_doc("Payment Entry", pe_name)
		allocated = {r.reference_name: flt(r.allocated_amount) for r in pe.references}
		self.assertEqual(allocated, {first.name: 600, second.name: 400})
		self.assertEqual(flt(pe.unallocated_amount), 0)
		self.assertEqual(get_party_outstanding("Purchase Invoice", second.name), 200)

	def test_reference_to_journal_entry_uses_ledger_outstanding(self):
		je_name = self.make_supplier_journal_entry(300)
		self.assertEqual(get_ledger_open_amount("Journal Entry", je_name), 300)

		pe_name = self.pay(
			"je_ref_pay",
			amount=500,
			references=[
				{"reference_doctype": "Journal Entry", "reference_name": je_name, "allocated_amount": 500}
			],
		)
		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(flt(pe.references[0].allocated_amount), 300)
		self.assertEqual(flt(pe.unallocated_amount), 200)
		self.assertEqual(get_ledger_open_amount("Journal Entry", je_name), 0)

	def test_unsupported_reference_type_is_rejected(self):
		self.assertRaises(
			frappe.ValidationError,
			self.pay,
			"bad_ref_pay",
			references=[
				{"reference_doctype": "Delivery Note", "reference_name": "X", "allocated_amount": 10}
			],
		)

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
		self.assertEqual(
			getdate(frappe.db.get_value("Journal Entry", je_name, "clearance_date")), getdate(today())
		)
		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "treasury_event"), "clear_bank")
		self.assertEqual(get_account_balance(bank), flt(bank_before - 1000))
		self.assertEqual(get_account_balance(pdc), pdc_before)

	def test_clearance_date_requires_submit(self):
		self.assertRaises(
			frappe.ValidationError, self.clearing_je, "clear_draft", clearance_date=today(), submit=False
		)

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

	def test_draft_is_submitted_by_second_call(self):
		draft = self.pay("draft_pay", submit=False)
		self.assertEqual(frappe.db.get_value("Payment Entry", draft, "docstatus"), 0)
		self.assertEqual(self.pay("draft_pay"), draft)
		self.assertEqual(frappe.db.get_value("Payment Entry", draft, "docstatus"), 1)

		je_draft = self.clearing_je("draft_je", submit=False)
		self.assertEqual(frappe.db.get_value("Journal Entry", je_draft, "docstatus"), 0)
		self.assertEqual(self.clearing_je("draft_je", clearance_date=today()), je_draft)
		je = frappe.db.get_value("Journal Entry", je_draft, ["docstatus", "clearance_date"], as_dict=True)
		self.assertEqual(je.docstatus, 1)
		self.assertEqual(getdate(je.clearance_date), getdate(today()))

	def test_new_voucher_after_cancellation(self):
		first = self.pay("recreate_pay")
		posting.cancel_voucher("Payment Entry", first)
		second = self.pay("recreate_pay")
		self.assertNotEqual(first, second)
		self.assertEqual(frappe.db.get_value("Payment Entry", second, "docstatus"), 1)

	def test_cycle_separates_repeated_events(self):
		# bounce with "Reverse by Journal Entry": the first party posting stays submitted
		first = self.pay("cycle_pay")
		first_reversal = posting.make_reversal("Payment Entry", first, today(), "cycle_reversal")
		self.assertEqual(self.pay("cycle_pay"), first)

		# re-presented cheque: cycle 2 posts again, and a second bounce reverses it again
		second = self.pay("cycle_pay", cycle=2)
		self.assertNotEqual(second, first)
		second_doc = frappe.db.get_value("Payment Entry", second, ["docstatus", "treasury_cycle"], as_dict=True)
		self.assertEqual((second_doc.docstatus, second_doc.treasury_cycle), (1, 2))
		second_reversal = posting.make_reversal("Payment Entry", second, today(), "cycle_reversal", cycle=2)
		self.assertNotEqual(second_reversal, first_reversal)
		self.assertEqual(frappe.db.get_value("Journal Entry", second_reversal, "treasury_cycle"), 2)

		# each cycle stays idempotent
		self.assertEqual(self.pay("cycle_pay", cycle=2), second)
		self.assertEqual(
			posting.make_reversal("Payment Entry", first, today(), "cycle_reversal"), first_reversal
		)
		self.assertEqual(frappe.db.count("Payment Entry", {"treasury_event": "cycle_pay"}), 2)
		self.assertRaises(frappe.ValidationError, self.pay, "cycle_pay", cycle=0)

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
			{
				"doctype": "Payment Entry",
				"docstatus": 1,
				"treasury_source_doctype": None,
				"treasury_source_name": None,
			}
		)
		try:
			voucher_guard.before_cancel(pe)
			voucher_guard.on_trash(pe)
		except frappe.ValidationError as e:
			self.fail(f"Guard blocked an unmanaged voucher: {e}")

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
		again = posting.make_reversal("Journal Entry", original, today(), "rev_je_reversal")
		self.assertEqual(again, reversal)

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

	def test_reversal_of_partly_unallocated_payment_entry(self):
		pi = make_purchase_invoice(400)
		pe_name = self.pay("rev_adv_original", amount=1000, invoice=pi)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		# the unallocated 600 is an open advance against the Payment Entry itself
		self.assertEqual(get_ledger_open_amount("Payment Entry", pe_name), -600)

		reversal = posting.make_reversal("Payment Entry", pe_name, today(), "rev_adv_reversal")
		assert_gl_entries(
			self,
			"Journal Entry",
			reversal,
			[
				(self.data.payable, 0, 400, SUPPLIER),
				(self.data.payable, 0, 600, SUPPLIER),
				(self.data.egp.pdc_payable, 1000, 0, None),
			],
		)
		refs = frappe.get_all(
			"Journal Entry Account",
			filters={"parent": reversal, "party": SUPPLIER},
			fields=["reference_type", "reference_name"],
		)
		self.assertCountEqual(
			[(r.reference_type, r.reference_name) for r in refs],
			[("Purchase Invoice", pi.name), ("Payment Entry", pe_name)],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 400)
		self.assertEqual(get_ledger_open_amount("Payment Entry", pe_name), 0)
		self.assertEqual(get_ledger_open_amount("Journal Entry", reversal), 0)

	def test_linked_vouchers(self):
		pe_name = self.pay("linked_pay")
		je_name = self.clearing_je("linked_je")
		linked = {
			(v["doctype"], v["name"]) for v in posting.get_linked_vouchers(SOURCE_DOCTYPE, self.data.settings)
		}
		self.assertIn(("Payment Entry", pe_name), linked)
		self.assertIn(("Journal Entry", je_name), linked)

	# ------------------------------------------------------------------ multi-currency

	def test_usd_cheque_to_company_currency_party(self):
		usd = self.data.usd
		pi = make_purchase_invoice(5000)
		pe_name = self.pay(
			"usd_pay",
			amount=100,
			bank_account_head=usd.pdc_payable,
			currency="USD",
			exchange_rate=USD_RATE,
			references=[
				{"reference_doctype": "Purchase Invoice", "reference_name": pi.name, "allocated_amount": 5000}
			],
		)
		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(pe.paid_from_account_currency, "USD")
		self.assertEqual(pe.paid_to_account_currency, "EGP")
		self.assertEqual(flt(pe.paid_amount), 100)
		self.assertEqual(flt(pe.received_amount), 100 * USD_RATE)
		self.assertEqual(flt(pe.source_exchange_rate), USD_RATE)
		self.assertEqual(flt(pe.target_exchange_rate), 1)
		base = 100 * USD_RATE
		assert_gl_entries(
			self,
			"Payment Entry",
			pe_name,
			[(self.data.payable, base, 0, SUPPLIER), (usd.pdc_payable, 0, base, None)],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

		reversal = posting.make_reversal("Payment Entry", pe_name, today(), "usd_pay_reversal")
		self.assertEqual(frappe.db.get_value("Journal Entry", reversal, "multi_currency"), 1)
		assert_gl_entries(
			self,
			"Journal Entry",
			reversal,
			[(self.data.payable, 0, base, SUPPLIER), (usd.pdc_payable, base, 0, None)],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), base)

	def test_usd_cheque_with_explicit_rate(self):
		usd = self.data.usd
		pe_name = self.pay("usd_rate_pay", amount=100, bank_account_head=usd.pdc_payable, exchange_rate=48)
		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(flt(pe.received_amount), 4800)
		self.assertEqual(flt(pe.unallocated_amount), 4800)
		assert_gl_entries(
			self, "Payment Entry", pe_name, [(self.data.payable, 4800, 0), (usd.pdc_payable, 0, 4800)]
		)

	def test_usd_cheque_looks_up_exchange_rate(self):
		usd = self.data.usd
		rate = flt(get_exchange_rate("USD", "EGP", today()))
		self.assertTrue(rate)
		pe_name = self.pay("usd_lookup_pay", amount=10, bank_account_head=usd.pdc_payable)
		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(flt(pe.source_exchange_rate), rate)
		self.assertEqual(flt(pe.received_amount, 2), flt(10 * rate, 2))

	def test_usd_cheque_to_usd_party(self):
		usd = self.data.usd
		pe_name = self.pay(
			"usd_party_pay",
			amount=100,
			bank_account_head=usd.pdc_payable,
			party=USD_SUPPLIER,
			exchange_rate=USD_RATE,
		)
		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(pe.paid_to, self.data.usd_payable)
		self.assertEqual(flt(pe.paid_amount), 100)
		self.assertEqual(flt(pe.received_amount), 100)
		self.assertEqual(flt(pe.source_exchange_rate), USD_RATE)
		self.assertEqual(flt(pe.target_exchange_rate), USD_RATE)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe_name,
			[
				(self.data.usd_payable, 100 * USD_RATE, 0, USD_SUPPLIER),
				(usd.pdc_payable, 0, 100 * USD_RATE, None),
			],
		)

	def test_currency_mismatch_is_rejected(self):
		usd = self.data.usd
		self.assertRaises(
			frappe.ValidationError,
			self.pay,
			"usd_mismatch_pay",
			bank_account_head=usd.pdc_payable,
			currency="EGP",
		)
		# USD party account can't be paid from an EGP cheque (neither the bank nor the company currency)
		self.assertRaises(frappe.ValidationError, self.pay, "usd_party_egp_pay", party=USD_SUPPLIER)

	def test_usd_clearing_journal_entry_and_reversal(self):
		usd = self.data.usd
		je_name = posting.create_journal_entry(
			source_doctype=SOURCE_DOCTYPE,
			source_name=self.data.settings,
			event="usd_clear",
			company=COMPANY,
			posting_date=today(),
			accounts=[
				{
					"account": usd.pdc_payable,
					"debit_in_account_currency": 100,
					"exchange_rate": USD_RATE,
				},
				{
					"account": usd.bank_gl,
					"credit_in_account_currency": 100,
					"exchange_rate": USD_RATE,
				},
			],
			cheque_no="CHQ-USD",
			cheque_date=today(),
		)
		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "multi_currency"), 1)
		amount = 100 * USD_RATE
		assert_gl_entries(
			self, "Journal Entry", je_name, [(usd.pdc_payable, amount, 0), (usd.bank_gl, 0, amount)]
		)

		reversal = posting.make_reversal("Journal Entry", je_name, today(), "usd_clear_reversal")
		assert_gl_entries(
			self, "Journal Entry", reversal, [(usd.pdc_payable, 0, amount), (usd.bank_gl, amount, 0)]
		)

	# ------------------------------------------------------------------ settings lookups

	def test_settings_lookups(self):
		mapping = posting.get_account_mapping(COMPANY, self.data.egp.bank_account)
		self.assertEqual(mapping.pdc_payable_account, self.data.egp.pdc_payable)
		self.assertEqual(posting.get_company_settings(COMPANY).name, COMPANY)
		self.assertRaises(frappe.ValidationError, posting.get_company_settings, "_Missing Company")
		self.assertRaises(
			frappe.ValidationError, posting.get_account_mapping, COMPANY, "_Missing Bank Account"
		)

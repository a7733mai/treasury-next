"""Phase 7b: the Payment Entry of every kind of Bank Transfer — exact GL, deductions, allocations, modes and
series, accounting details, rounding, and the posting service extensions (spec 7b §3.4, §4 T1-T6, §7 B)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, today

from treasury.accounting import posting
from treasury.tests.utils import (
	BANK,
	COMPANY,
	CUSTOMER,
	OTHER_BANK,
	SUPPLIER,
	TRANSFER_SETTINGS,
	USD_SUPPLIER,
	TreasuryTestMixin,
	acc,
	assert_deductions,
	assert_gl_entries,
	bank_account_name,
	ensure_branch,
	ensure_branch_dimension,
	ensure_mode,
	ensure_party_bank_account,
	ensure_second_egp_bank,
	fee_rule,
	get_ledger_open_amount,
	get_party_outstanding,
	get_pe,
	make_bank_transfer,
	make_iban,
	make_purchase_invoice,
	make_sales_invoice,
	make_usd_purchase_invoice,
	set_fee_rules,
	set_mode_map,
	standard_fee_rules,
)

DOCTYPE = "Bank Transfer"


def pi_reference(invoice, amount):
	return [
		{"reference_doctype": invoice.doctype, "reference_name": invoice.name, "allocated_amount": amount}
	]


class TestBankTransferPosting(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch_field = ensure_branch_dimension()  # persistent fixture (DDL) before any test data
		super().setUpClass()
		# series registered by a test are Property Setters, rolled back with the class
		cls.addClassCleanup(frappe.clear_cache, doctype="Payment Entry")
		cls.charges = acc("Treasury Bank Charges")
		cls.creditors = cls.data.payable
		cls.debtors = cls.data.receivable

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())
		self.today = getdate(today())

	def pe_of(self, transfer):
		transfer.reload()
		self.assertTrue(transfer.payment_entry)
		return get_pe(transfer.payment_entry)

	# ------------------------------------------------------------------ T1 - T3 party transfers

	def test_party_payment_normal_other_bank(self):
		egp = self.data.egp
		mode = ensure_mode(f"Transfer Mode {frappe.generate_hash(length=4)}", account=egp.bank_gl)
		set_mode_map(
			self, [{"bank_account": egp.bank_account, "method": "Bank Transfer", "mode_of_payment": mode}]
		)
		pi = make_purchase_invoice(20000)
		transfer = make_bank_transfer(amount=20000, references=pi_reference(pi, 20000))

		self.assertEqual((transfer.status, transfer.accounting_status), ("Sent", "Posted"))
		pe = self.pe_of(transfer)
		self.assertEqual(
			(pe.payment_type, flt(pe.paid_amount), flt(pe.received_amount)), ("Pay", 20030, 20030)
		)
		assert_deductions(self, pe, [(self.charges, 30, 0)])
		self.assertEqual((flt(pe.unallocated_amount), flt(pe.difference_amount)), (0, 0))
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[
				(self.creditors, 20000, 0, SUPPLIER, self.today),
				(self.charges, 30, 0, None, self.today),
				(egp.bank_gl, 0, 20030, None, self.today),
			],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		self.assertEqual(pe.mode_of_payment, mode)
		self.assertEqual(pe.reference_no, transfer.name)
		self.assertIsNone(pe.clearance_date)
		self.assertEqual(flt(transfer.base_fee_amount), 30)
		self.assertEqual([r.event for r in transfer.status_log], ["Created", "Sent"])
		self.assertEqual(transfer.status_log[-1].voucher_no, pe.name)

	def test_fawran_advance_no_reference(self):
		egp = self.data.egp
		transfer = make_bank_transfer(method="Fawran", amount=800)
		pe = self.pe_of(transfer)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(self.creditors, 800, 0, SUPPLIER), (self.charges, 0.5, 0, None), (egp.bank_gl, 0, 800.5, None)],
		)
		self.assertEqual(flt(pe.unallocated_amount), 800)
		# the advance is open against the Payment Entry itself (a debit on the payable: negative)
		self.assertEqual(get_ledger_open_amount("Payment Entry", pe.name), -800)

	def test_party_receipt_incoming_fee(self):
		egp = self.data.egp
		si = make_sales_invoice(20000)
		transfer = make_bank_transfer(
			transfer_type="Party Receipt", amount=20000, references=pi_reference(si, 20000)
		)
		self.assertEqual((transfer.fee_amount, transfer.bank_amount), (5, 19995))
		pe = self.pe_of(transfer)
		self.assertEqual(
			(pe.payment_type, flt(pe.paid_amount), flt(pe.received_amount)), ("Receive", 19995, 19995)
		)
		assert_deductions(self, pe, [(self.charges, 5, 0)])
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(egp.bank_gl, 19995, 0, None), (self.charges, 5, 0, None), (self.debtors, 0, 20000, CUSTOMER)],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 0)

	# ------------------------------------------------------------------ T4 - T5 internal transfers

	def test_internal_same_bank_no_fee(self):
		egp, second = self.data.egp, ensure_second_egp_bank()
		transfer = make_bank_transfer(
			transfer_type="Internal Transfer", amount=50000, to_bank_account=second.bank_account
		)
		self.assertEqual((transfer.scope, transfer.fee_amount), ("Same Bank", 0))
		pe = self.pe_of(transfer)
		self.assertEqual(pe.payment_type, "Internal Transfer")
		self.assertFalse(pe.deductions)
		assert_gl_entries(
			self, "Payment Entry", pe.name, [(second.bank_gl, 50000, 0), (egp.bank_gl, 0, 50000)]
		)

	def test_internal_other_bank_fee(self):
		egp, second = self.data.egp, ensure_second_egp_bank()
		# the second bank at another bank: rule R3 (10 + 0.1 % = 60, capped at 50)
		frappe.db.set_value("Bank Account", second.bank_account, "bank", OTHER_BANK)
		self.addCleanup(frappe.db.set_value, "Bank Account", second.bank_account, "bank", BANK)
		transfer = make_bank_transfer(
			transfer_type="Internal Transfer", amount=50000, to_bank_account=second.bank_account
		)
		self.assertEqual((transfer.scope, transfer.fee_amount), ("Other Bank", 50))
		pe = self.pe_of(transfer)
		self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (50050, 50000))
		assert_deductions(self, pe, [(self.charges, 50, 1)])
		self.assertEqual(flt(pe.difference_amount), 0)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(second.bank_gl, 50000, 0), (self.charges, 50, 0), (egp.bank_gl, 0, 50050)],
		)

	def test_internal_usd_to_egp(self):
		egp, usd = self.data.egp, self.data.usd
		set_fee_rules(self, [fee_rule(usd.bank_account, "Normal", scope="Any", fixed_fee=5)])
		transfer = make_bank_transfer(
			transfer_type="Internal Transfer",
			bank_account=usd.bank_account,
			to_bank_account=egp.bank_account,
			amount=1000,
			received_amount=49800,
		)
		pe = self.pe_of(transfer)
		self.assertEqual((flt(pe.source_exchange_rate, 9), flt(pe.target_exchange_rate, 9)), (49.8, 1))
		self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (1005, 49800))
		assert_deductions(self, pe, [(self.charges, 249, 1)])
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(egp.bank_gl, 49800, 0), (self.charges, 249, 0), (usd.bank_gl, 0, 50049)],
		)

	def test_internal_egp_to_usd(self):
		egp, usd = self.data.egp, self.data.usd
		set_fee_rules(self, [fee_rule(egp.bank_account, "Normal", scope="Any", fixed_fee=10)])
		transfer = make_bank_transfer(
			transfer_type="Internal Transfer",
			bank_account=egp.bank_account,
			to_bank_account=usd.bank_account,
			amount=50000,
			received_amount=990,
		)
		pe = self.pe_of(transfer)
		self.assertEqual(
			(flt(pe.source_exchange_rate, 9), flt(pe.target_exchange_rate, 9)), (1, flt(50000 / 990, 9))
		)
		self.assertEqual(flt(pe.base_received_amount), 50000)
		assert_deductions(self, pe, [(self.charges, 10, 1)])
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(usd.bank_gl, 50000, 0), (self.charges, 10, 0), (egp.bank_gl, 0, 50010)],
		)

	# ------------------------------------------------------------------ T6 SWIFT

	def test_swift_our_sha_ben(self):
		usd = self.data.usd
		payable_usd = self.data.usd_payable
		expected = {"OUR": 40, "SHA": 25, "BEN": 0}
		for bearer, fee in expected.items():
			with self.subTest(bearer=bearer):
				pi = make_usd_purchase_invoice(1000, conversion_rate=50)
				transfer = make_bank_transfer(
					method="SWIFT",
					bank_account=usd.bank_account,
					party=USD_SUPPLIER,
					charge_bearer=bearer,
					beneficiary_swift="DEUTDEFF",
					exchange_rate=52,
					amount=1000,
					references=pi_reference(pi, 1000),
				)
				self.assertEqual((transfer.scope, transfer.fee_amount), ("International", fee))
				pe = self.pe_of(transfer)
				self.assertEqual(flt(pe.paid_amount), 1000 + fee)
				self.assertEqual(flt(pe.received_amount), 1000 + fee)
				rows = [(payable_usd, 52000, 0, USD_SUPPLIER), (usd.bank_gl, 0, (1000 + fee) * 52, None)]
				if fee:
					rows.append((self.charges, fee * 52, 0, None))
					assert_deductions(self, pe, [(self.charges, fee * 52, 0)])
				else:
					self.assertFalse(pe.deductions)
				assert_gl_entries(self, "Payment Entry", pe.name, rows)
				self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
				if bearer == "OUR":
					# ERPNext's own Exchange Gain / Loss Journal Entry against the invoice (not a Treasury voucher)
					fx = frappe.get_all(
						"Journal Entry Account",
						filters={
							"reference_type": "Purchase Invoice",
							"reference_name": pi.name,
							"docstatus": 1,
						},
						pluck="parent",
					)
					self.assertTrue(fx)
					self.assertFalse(frappe.db.get_value("Journal Entry", fx[0], "treasury_source_name"))

	# ------------------------------------------------------------------ modes, series, details

	def test_mode_and_series(self):
		egp = self.data.egp
		tag = frappe.generate_hash(length=4).upper()
		fawran_mode = ensure_mode(f"Fawran Mode {tag}", account=egp.bank_gl)
		internal_mode = ensure_mode(f"Internal Mode {tag}", account=egp.bank_gl)
		transfer_mode = ensure_mode(f"Transfer Mode {tag}", account=egp.bank_gl)
		series = f"PE-TTC-FWR{tag}-.YYYY.-"
		set_mode_map(
			self,
			[
				{
					"bank_account": egp.bank_account,
					"method": "Fawran",
					"mode_of_payment": fawran_mode,
					"pe_naming_series": series,
				},
				{
					"bank_account": egp.bank_account,
					"method": "Bank Transfer",
					"mode_of_payment": transfer_mode,
				},
				{
					"bank_account": egp.bank_account,
					"method": "Internal Transfer",
					"mode_of_payment": internal_mode,
				},
			],
		)
		frappe.get_doc("Treasury Company Settings", COMPANY).add_series_to_payment_entry(confirm=1)

		fawran = self.pe_of(make_bank_transfer(method="Fawran", amount=700))
		self.assertTrue(fawran.name.startswith(f"PE-TTC-FWR{tag}-"), fawran.name)
		self.assertEqual(fawran.mode_of_payment, fawran_mode)

		second = ensure_second_egp_bank()
		internal = self.pe_of(
			make_bank_transfer(
				transfer_type="Internal Transfer", amount=700, to_bank_account=second.bank_account
			)
		)
		self.assertEqual(internal.mode_of_payment, internal_mode)

		# without the Internal Transfer row, the method's row applies
		set_mode_map(
			self,
			[{"bank_account": egp.bank_account, "method": "Bank Transfer", "mode_of_payment": transfer_mode}],
		)
		internal = self.pe_of(
			make_bank_transfer(
				transfer_type="Internal Transfer", amount=701, to_bank_account=second.bank_account
			)
		)
		self.assertEqual(internal.mode_of_payment, transfer_mode)

	def test_accounting_details_and_dimension(self):
		branch = ensure_branch()
		cost_center = frappe.get_cached_value("Company", COMPANY, "cost_center")
		project_name = f"BT {frappe.generate_hash(length=5)}"
		project = (
			frappe.get_doc({"doctype": "Project", "project_name": project_name, "company": COMPANY})
			.insert(ignore_permissions=True)
			.name
		)
		bank_account = ensure_party_bank_account("Supplier", SUPPLIER, iban=make_iban("OTHB"), is_default=0)
		contact = (
			frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": f"BT Contact {frappe.generate_hash(length=4)}",
					"links": [{"link_doctype": "Supplier", "link_name": SUPPLIER}],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		transfer = make_bank_transfer(
			amount=20000,
			beneficiary_iban=None,
			project=project,
			party_bank_account=bank_account,
			contact_person=contact,
			**{self.branch_field: branch},
		)
		self.assertEqual(transfer.cost_center, cost_center)
		pe = self.pe_of(transfer)
		self.assertEqual(
			(pe.cost_center, pe.project, pe.party_bank_account, pe.contact_person, pe.get(self.branch_field)),
			(cost_center, project, bank_account, contact, branch),
		)
		self.assertEqual([d.get(self.branch_field) for d in pe.deductions], [branch])
		self.assertEqual([d.cost_center for d in pe.deductions], [cost_center])
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Payment Entry", "voucher_no": pe.name, "is_cancelled": 0},
			pluck=self.branch_field,
		)
		self.assertEqual(set(gl), {branch})

	def test_rounding_reconciliation(self):
		"""Receive into a USD bank from an EGP customer at 50.00004: 200 USD principal is 10,000.01 EGP, the net
		100 USD and the fee 100 USD are 5,000.00 each — the 0.01 goes into the charges row."""
		usd = self.data.usd
		si = make_sales_invoice(20000)
		name = posting.create_payment_entry(
			source_doctype="Sales Invoice",
			source_name=si.name,
			event="test_bt_rounding",
			payment_type="Receive",
			company=COMPANY,
			posting_date=today(),
			party_type="Customer",
			party=CUSTOMER,
			amount=200,
			bank_account_head=usd.bank_gl,
			reference_no="RND",
			reference_date=today(),
			currency="USD",
			exchange_rate=50.00004,
			references=pi_reference(si, 10000.01),
			charges=100,
			charges_account=self.charges,
		)
		pe = get_pe(name)
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(flt(pe.difference_amount), 0)
		assert_deductions(self, pe, [(self.charges, 5000.01, 0)])
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 9999.99)

	def test_rounding_unallocated_absorbed(self):
		"""The other form of the gap, at 50.00006: the principal 200 USD is 10,000.01 EGP (fully allocated), the
		net 100 USD and the fee 100 USD are 5,000.01 each — the party side is over by 0.01, which ERPNext
		would leave as an unallocated advance of 0.01. It goes into the charges row (5,000.00) instead."""
		usd = self.data.usd
		si = make_sales_invoice(20000)
		name = posting.create_payment_entry(
			source_doctype="Sales Invoice",
			source_name=si.name,
			event="test_bt_rounding_unallocated",
			payment_type="Receive",
			company=COMPANY,
			posting_date=today(),
			party_type="Customer",
			party=CUSTOMER,
			amount=200,
			bank_account_head=usd.bank_gl,
			reference_no="RNDU",
			reference_date=today(),
			currency="USD",
			exchange_rate=50.00006,
			references=pi_reference(si, 10000.01),
			charges=100,
			charges_account=self.charges,
		)
		pe = get_pe(name)
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual((flt(pe.unallocated_amount), flt(pe.difference_amount)), (0, 0))
		assert_deductions(self, pe, [(self.charges, 5000.00, 0)])
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 9999.99)
		self.assertFalse(
			frappe.get_all(
				"Payment Ledger Entry",
				filters={"voucher_no": name, "against_voucher_no": name, "delinked": 0},
				pluck="name",
			),
			"no unallocated advance on the Payment Entry",
		)

	def test_real_advance_is_not_absorbed(self):
		"""A principal larger than the invoice outstanding leaves a real advance, even a small one."""
		egp = self.data.egp
		pi = make_purchase_invoice(1000)
		name = posting.create_payment_entry(
			source_doctype="Purchase Invoice",
			source_name=pi.name,
			event="test_bt_real_advance",
			payment_type="Pay",
			company=COMPANY,
			posting_date=today(),
			party_type="Supplier",
			party=SUPPLIER,
			amount=1000.03,
			bank_account_head=egp.bank_gl,
			reference_no="ADV",
			reference_date=today(),
			references=pi_reference(pi, 1000),
			charges=7,
			charges_account=self.charges,
		)
		pe = get_pe(name)
		self.assertEqual(flt(pe.unallocated_amount), 0.03)
		assert_deductions(self, pe, [(self.charges, 7, 0)])

	# ------------------------------------------------------------------ posting service

	def test_posting_service_charges(self):
		egp = self.data.egp
		pi = make_purchase_invoice(3000)
		values = {
			"source_doctype": "Purchase Invoice",
			"source_name": pi.name,
			"event": "test_bt_charges",
			"payment_type": "Pay",
			"company": COMPANY,
			"posting_date": today(),
			"party_type": "Supplier",
			"party": SUPPLIER,
			"amount": 1000,
			"bank_account_head": egp.bank_gl,
			"reference_no": "CHG",
			"reference_date": today(),
			"charges": 7,
			"charges_account": self.charges,
		}
		first = posting.create_payment_entry(**values)
		self.assertEqual(posting.create_payment_entry(**values), first)
		assert_gl_entries(
			self,
			"Payment Entry",
			first,
			[(self.creditors, 1000, 0, SUPPLIER), (self.charges, 7, 0, None), (egp.bank_gl, 0, 1007, None)],
		)
		errors = {
			"negative charges": {"charges": -1},
			"no account": {"charges_account": None},
			"account in another currency": {"charges_account": self.data.usd.bank_gl},
			"receive with charges above the amount": {"payment_type": "Receive", "charges": 1000},
		}
		for case, changes in errors.items():
			with self.subTest(case=case):
				self.assertRaises(
					frappe.ValidationError,
					posting.create_payment_entry,
					**{**values, "event": "test_bt_charges_errors", **changes},
				)
		self.assertFalse(frappe.db.exists("Payment Entry", {"treasury_event": "test_bt_charges_errors"}))

		self.assertRaisesRegex(
			frappe.ValidationError,
			"Received Amount is required",
			posting.create_internal_transfer,
			source_doctype="Purchase Invoice",
			source_name=pi.name,
			event="test_bt_cross",
			company=COMPANY,
			posting_date=today(),
			paid_from=egp.bank_gl,
			paid_to=self.data.usd.bank_gl,
			paid_amount=100,
			reference_no="X",
			reference_date=today(),
		)

	def test_existing_callers_unchanged(self):
		"""A PDC party posting (no charges) books exactly what it did before."""
		from treasury.tests.utils import DEFAULT_PDC_SETTINGS, day, make_pdc_cheque

		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		egp = self.data.egp
		cheque = make_pdc_cheque(amount=1234.5, issue_date=day(-40), due_date=day(-10))
		cheque.deliver(handover_date=day(-39))
		cheque.reload()
		pe = get_pe(cheque.party_payment_entry)
		self.assertFalse(pe.deductions)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(self.creditors, 1234.5, 0, SUPPLIER), (egp.pdc_payable, 0, 1234.5, None)],
		)

	def test_future_transfer_posts_nothing_until_sent(self):
		transfer = make_bank_transfer(amount=500, execution_date=add_days(today(), 3))
		self.assertEqual((transfer.status, transfer.accounting_status), ("Scheduled", "Not Posted"))
		self.assertFalse(transfer.payment_entry)
		self.assertEqual(BANK, frappe.db.get_value("Bank Account", transfer.bank_account, "bank"))

"""Phase 7d: the vouchers of Bank Charges and refunds (spec 7d §4, §7 A): exact GL rows in Payment Entry and
Journal Entry mode, in EGP and USD, the bank supplier, clearance, modes / series, accounting details and the
posting service itself."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from treasury.accounting import posting
from treasury.bank_transfers import charges
from treasury.tests.utils import (
	BANK,
	BANK_CHARGE_SETTINGS,
	COMPANY,
	SUPPLIER_GROUP,
	TreasuryTestMixin,
	acc,
	assert_deductions,
	assert_gl_entries,
	bank_account_name,
	day,
	ensure_account,
	ensure_branch,
	ensure_branch_dimension,
	ensure_mode,
	ensure_supplier,
	get_charge,
	get_party_balance,
	get_pe,
	get_voucher_count,
	make_bank_charge,
	set_bank_suppliers,
	set_mode_map,
)

DOCTYPE = "Bank Charge"


def own_bank_account(bank: str | None) -> str:
	"""A company bank account of its own GL account at `bank` (None: the Bank is removed after insert)."""
	tag = frappe.generate_hash(length=6)
	gl = ensure_account(f"BC Bank {tag}", "Bank Accounts", "Bank")
	if bank and not frappe.db.exists("Bank", bank):
		frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert(ignore_permissions=True)
	doc = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": f"BC {tag}",
			"bank": bank or BANK,
			"account": gl,
			"company": COMPANY,
			"is_company_account": 1,
		}
	).insert(ignore_permissions=True)
	if not bank:
		frappe.db.set_value("Bank Account", doc.name, "bank", None)
	return doc.name


def ple_count(voucher_no: str) -> int:
	return frappe.db.count(
		"Payment Ledger Entry", {"voucher_type": "Payment Entry", "voucher_no": voucher_no}
	)


class TestBankChargePosting(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch_field = ensure_branch_dimension()  # persistent fixture (DDL) before any test data
		super().setUpClass()
		# series registered by a test are Property Setters, rolled back with the class
		cls.addClassCleanup(frappe.clear_cache, doctype="Payment Entry")
		cls.charges_account = acc("Treasury Bank Charges")

	def setUp(self):
		super().setUp()
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		set_bank_suppliers(self, [])

	def bank_supplier(self) -> str:
		return frappe.db.get_value(
			"Treasury Bank Supplier", {"parent": "Treasury Settings", "bank": BANK}, "supplier"
		)

	# ------------------------------------------------------------------ C1 / C2: Payment Entry mode

	def test_pe_charge_egp(self):
		egp = self.data.egp
		charge = make_bank_charge()
		self.assertEqual((charge.status, charge.accounting_status), ("Posted", "Posted"))
		self.assertTrue(charge.payment_entry)
		self.assertFalse(charge.journal_entry)
		pe = get_pe(charge.payment_entry)
		supplier = self.bank_supplier()
		self.assertEqual((pe.payment_type, pe.party_type, pe.party), ("Pay", "Supplier", supplier))
		self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (150, 150))
		self.assertEqual((pe.paid_from, pe.paid_to), (egp.bank_gl, self.data.payable))
		assert_deductions(self, pe, [(self.charges_account, 150, 0)])
		self.assertEqual((flt(pe.unallocated_amount), flt(pe.difference_amount)), (0, 0))
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(self.charges_account, 150, 0, None, day(-5)), (egp.bank_gl, 0, 150, None, day(-5))],
		)
		self.assertEqual(ple_count(pe.name), 0)
		self.assertEqual(get_party_balance("Supplier", supplier, day(0)), 0)
		self.assertEqual(pe.reference_no, charge.name)
		self.assertEqual(frappe.utils.getdate(pe.clearance_date), day(-5))
		self.assertEqual(frappe.utils.getdate(charge.clearance_date), day(-5))
		self.assertEqual((pe.treasury_source_doctype, pe.treasury_source_name), (DOCTYPE, charge.name))
		self.assertEqual(pe.treasury_event, charges.EVENT_CHARGE)
		self.assertEqual([r.event for r in charge.status_log], ["Created", "Posted"])
		self.assertEqual(charge.status_log[-1].voucher_no, pe.name)

	def test_pe_charge_usd(self):
		usd = self.data.usd
		charge = make_bank_charge(bank_account=bank_account_name("USD"), amount=20)
		self.assertEqual(
			(charge.currency, flt(charge.exchange_rate), flt(charge.base_amount)), ("USD", 50, 1000)
		)
		pe = get_pe(charge.payment_entry)
		self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (20, 1000))
		assert_deductions(self, pe, [(self.charges_account, 1000, 0)])
		assert_gl_entries(
			self, "Payment Entry", pe.name, [(self.charges_account, 1000, 0), (usd.bank_gl, 0, 1000)]
		)
		bank_row = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name, "account": usd.bank_gl, "is_cancelled": 0},
			fields=["credit_in_account_currency"],
		)
		self.assertEqual([flt(r.credit_in_account_currency) for r in bank_row], [20])
		self.assertEqual(ple_count(pe.name), 0)

	# ------------------------------------------------------------------ C3 / C4: Journal Entry mode

	def test_je_charge_egp(self):
		self.use_company_settings(bank_charge_voucher="Journal Entry")
		egp = self.data.egp
		charge = make_bank_charge(bank_reference="BC-JE-1")
		self.assertTrue(charge.journal_entry)
		self.assertFalse(charge.payment_entry)
		je = frappe.get_doc("Journal Entry", charge.journal_entry)
		self.assertEqual((je.voucher_type, je.cheque_no), ("Bank Entry", "BC-JE-1"))
		assert_gl_entries(
			self,
			"Journal Entry",
			je.name,
			[(self.charges_account, 150, 0, None, day(-5)), (egp.bank_gl, 0, 150, None, day(-5))],
		)
		self.assertEqual(frappe.utils.getdate(je.clearance_date), day(-5))
		self.assertEqual((charge.status, charge.accounting_status), ("Posted", "Posted"))

	def test_je_charge_usd(self):
		self.use_company_settings(bank_charge_voucher="Journal Entry")
		usd = self.data.usd
		charge = make_bank_charge(bank_account=bank_account_name("USD"), amount=20)
		je = frappe.get_doc("Journal Entry", charge.journal_entry)
		self.assertEqual((je.voucher_type, je.cheque_no, je.multi_currency), ("Bank Entry", charge.name, 1))
		assert_gl_entries(
			self, "Journal Entry", je.name, [(self.charges_account, 1000, 0), (usd.bank_gl, 0, 1000)]
		)
		bank_row = next(row for row in je.accounts if row.account == usd.bank_gl)
		self.assertEqual((flt(bank_row.credit_in_account_currency), flt(bank_row.exchange_rate)), (20, 50))

	# ------------------------------------------------------------------ R1 / R2: refunds

	def test_refund_pe(self):
		egp = self.data.egp
		charge = make_bank_charge()
		refund = make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=50)
		pe = get_pe(refund.payment_entry)
		self.assertEqual((pe.payment_type, pe.party), ("Receive", self.bank_supplier()))
		self.assertEqual((pe.paid_from, pe.paid_to), (self.data.payable, egp.bank_gl))
		assert_deductions(self, pe, [(self.charges_account, -50, 0)])
		assert_gl_entries(
			self, "Payment Entry", pe.name, [(egp.bank_gl, 50, 0, None), (self.charges_account, 0, 50, None)]
		)
		self.assertEqual(ple_count(pe.name), 0)

		charge.reload()
		self.assertEqual(flt(charge.refunded_amount), 50)
		self.assertEqual(charge.status_log[-1].event, "Refunded")
		self.assertEqual(
			(charge.status_log[-1].voucher_no, charge.status_log[-1].remarks), (pe.name, refund.name)
		)

	def test_refund_je(self):
		self.use_company_settings(bank_charge_voucher="Journal Entry")
		egp = self.data.egp
		charge = make_bank_charge()
		refund = make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=50)
		assert_gl_entries(
			self, "Journal Entry", refund.journal_entry, [(egp.bank_gl, 50, 0), (self.charges_account, 0, 50)]
		)
		self.assertEqual(flt(get_charge(charge.name).refunded_amount), 50)

	# ------------------------------------------------------------------ clearance, bank supplier

	def test_clearance_setting(self):
		for voucher in ("Payment Entry", "Journal Entry"):
			for clearance in ("On Posting", "On Bank Transaction"):
				with self.subTest(voucher=voucher, clearance=clearance):
					self.use_company_settings(bank_charge_voucher=voucher, bank_charge_clearance=clearance)
					charge = make_bank_charge()
					name = charge.payment_entry if voucher == "Payment Entry" else charge.journal_entry
					cleared = frappe.db.get_value(voucher, name, "clearance_date")
					expected = day(-5) if clearance == "On Posting" else None
					self.assertEqual(frappe.utils.getdate(cleared) if cleared else None, expected)
					self.assertEqual(charge.status, "Posted")

	def test_bank_supplier_on_demand(self):
		bank_name = frappe.db.get_value("Bank", BANK, "bank_name") or BANK
		existed = frappe.db.exists("Supplier", {"supplier_name": bank_name})
		charge = make_bank_charge()
		supplier = self.bank_supplier()
		self.assertTrue(supplier)
		self.assertEqual(frappe.db.get_value("Supplier", supplier, "supplier_name"), bank_name)
		self.assertEqual(get_pe(charge.payment_entry).party, supplier)
		if not existed:  # created now, in the Payee Supplier Group
			self.assertEqual(
				frappe.db.get_value("Supplier", supplier, "supplier_group"),
				frappe.db.get_single_value("Treasury Settings", "payee_supplier_group"),
			)

		# a second charge reuses it
		make_bank_charge(amount=151)
		self.assertEqual(frappe.db.count("Supplier", {"supplier_name": bank_name}), 1)
		self.assertEqual(self.bank_supplier(), supplier)

		# an existing enabled Supplier named like another Bank is reused
		other_bank = f"_Test BC Bank {frappe.generate_hash(length=5)}"
		existing = ensure_supplier(other_bank)
		charge = make_bank_charge(bank_account=own_bank_account(other_bank))
		self.assertEqual(get_pe(charge.payment_entry).party, existing)
		self.assertEqual(frappe.db.get_value("Supplier", existing, "supplier_group"), SUPPLIER_GROUP)
		self.assertEqual(frappe.db.count("Supplier", {"supplier_name": other_bank}), 1)

		# a mapped disabled Supplier is an error
		disabled = ensure_supplier(f"_Test BC Disabled {frappe.generate_hash(length=5)}")
		frappe.db.set_value("Supplier", disabled, "disabled", 1)
		self.addCleanup(frappe.db.set_value, "Supplier", disabled, "disabled", 0)
		set_bank_suppliers(self, [{"bank": BANK, "supplier": disabled}])
		draft = make_bank_charge(submit=False, amount=152)
		self.assertRaisesRegex(frappe.ValidationError, "is disabled", draft.submit)

		# a Bank Account without a Bank: only by Journal Entry
		no_bank = own_bank_account(None)
		draft = make_bank_charge(submit=False, bank_account=no_bank, amount=153)
		self.assertRaisesRegex(frappe.ValidationError, "has no Bank", draft.submit)
		self.use_company_settings(bank_charge_voucher="Journal Entry")
		charge = make_bank_charge(bank_account=no_bank, amount=154)
		self.assertTrue(charge.journal_entry)

	# ------------------------------------------------------------------ modes, series, accounting details

	def test_mode_and_series(self):
		egp = self.data.egp
		tag = frappe.generate_hash(length=4).upper()
		mode = ensure_mode(f"Charge Mode {tag}", account=egp.bank_gl)
		set_mode_map(
			self,
			[
				{
					"bank_account": egp.bank_account,
					"method": "Bank Charge",
					"mode_of_payment": mode,
					"pe_naming_series": f"PE-TTC-BCH{tag}-.YYYY.-",
				}
			],
		)
		frappe.get_doc("Treasury Company Settings", COMPANY).add_series_to_payment_entry(confirm=1)
		charge = make_bank_charge()
		self.assertEqual(charge.mode_of_payment, mode)
		pe = get_pe(charge.payment_entry)
		self.assertTrue(pe.name.startswith(f"PE-TTC-BCH{tag}-"), pe.name)
		self.assertEqual(pe.mode_of_payment, mode)

	def test_accounting_details_and_dimension(self):
		branch = ensure_branch()
		cost_center = frappe.get_cached_value("Company", COMPANY, "cost_center")
		project = (
			frappe.get_doc(
				{
					"doctype": "Project",
					"project_name": f"BC {frappe.generate_hash(length=5)}",
					"company": COMPANY,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		charge = make_bank_charge(project=project, **{self.branch_field: branch})
		self.assertEqual(charge.cost_center, cost_center)
		pe = get_pe(charge.payment_entry)
		self.assertEqual(
			(pe.cost_center, pe.project, pe.get(self.branch_field)), (cost_center, project, branch)
		)
		self.assertEqual([d.get(self.branch_field) for d in pe.deductions], [branch])
		self.assertEqual([d.cost_center for d in pe.deductions], [cost_center])
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Payment Entry", "voucher_no": pe.name, "is_cancelled": 0},
			pluck=self.branch_field,
		)
		self.assertEqual(set(gl), {branch})

		self.use_company_settings(bank_charge_voucher="Journal Entry")
		charge = make_bank_charge(project=project, amount=160, **{self.branch_field: branch})
		je = frappe.get_doc("Journal Entry", charge.journal_entry)
		expense = next(row for row in je.accounts if row.account == self.charges_account)
		self.assertEqual(
			(expense.cost_center, expense.project, expense.get(self.branch_field)),
			(cost_center, project, branch),
		)

	# ------------------------------------------------------------------ posting service, idempotency

	def test_posting_service_direct(self):
		egp = self.data.egp
		source = make_bank_charge(submit=False)

		def create(**values):
			args = {
				"source_doctype": DOCTYPE,
				"source_name": source.name,
				"event": "bank_charge_test",
				"payment_type": "Pay",
				"company": COMPANY,
				"posting_date": day(-5),
				"party_type": "Supplier",
				"party": ensure_supplier("_Test BC Direct Supplier"),
				"amount": 75,
				"bank_account_head": egp.bank_gl,
				"expense_account": self.charges_account,
				"reference_no": "DIRECT",
				"reference_date": day(-5),
				**values,
			}
			return posting.create_bank_charge_payment(**args)

		first = create()
		self.assertEqual(create(), first)
		self.assertEqual(get_pe(first).docstatus, 1)
		self.assertRaisesRegex(
			frappe.ValidationError, "Pay or Receive", create, event="bt_1", payment_type="Internal Transfer"
		)
		self.assertRaisesRegex(frappe.ValidationError, "greater than zero", create, event="bt_2", amount=0)
		usd_expense = ensure_account("Treasury USD Expense", "Indirect Expenses", currency="USD")
		self.assertRaisesRegex(
			frappe.ValidationError, "company currency", create, event="bt_3", expense_account=usd_expense
		)

	def test_idempotent_post(self):
		charge = make_bank_charge()
		charges.post(charge)
		charges.post(charge)
		self.assertEqual(get_voucher_count(DOCTYPE, charge.name), 1)

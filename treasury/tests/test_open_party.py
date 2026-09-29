"""Phase 7a: open party — cheques of any Party Type, the reference rules per party, "New Payee"
(`create_payee`), encashment of a security cheque of any party and the Projected Party Statement of any
party (spec 7a §2.4, §2.7, §3.4, §3.5, §7 E)."""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.pdc.report.projected_party_statement.projected_party_statement import (
	execute as projected_party_statement,
)
from treasury.tests.utils import (
	COMPANY,
	PE_SETTINGS,
	SHAREHOLDER_TITLE,
	TreasuryTestMixin,
	acc,
	assert_gl_entries,
	bank_account_name,
	day,
	ensure_custody_location,
	ensure_employee,
	ensure_shareholder,
	ensure_supplier,
	ensure_user,
	get_pe,
	make_pdc_cheque,
	make_purchase_invoice,
	make_security_cheque,
)
from treasury.utils.party import create_payee, ensure_payee_supplier_group

TREASURY_USER = "treasury-payee-user@example.com"
NO_CHEQUE_USER = "treasury-payee-nocheque@example.com"


class TestOpenParty(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.employee = ensure_employee()
		cls.employee_name = frappe.db.get_value("Employee", cls.employee, "employee_name")
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(NO_CHEQUE_USER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)
		self.tag = frappe.generate_hash(length=5)

	def use_payee_group(self, group: str):
		previous = frappe.db.get_single_value("Treasury Settings", "payee_supplier_group")
		frappe.db.set_single_value("Treasury Settings", "payee_supplier_group", group)
		self.addCleanup(frappe.db.set_single_value, "Treasury Settings", "payee_supplier_group", previous)

	# ------------------------------------------------------------------ any Party Type

	def test_outgoing_cheque_to_employee(self):
		egp = self.data.egp
		cheque = make_pdc_cheque(
			party_type="Employee", party=self.employee, amount=5000, issue_date=day(-20), due_date=day(-5)
		)
		self.assertEqual(cheque.party_name, self.employee_name)
		cheque.deliver(handover_date=day(-19))
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")

		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual((pe.payment_type, pe.party_type, pe.party), ("Pay", "Employee", self.employee))
		self.assertEqual(pe.party_name, self.employee_name)
		self.assertEqual(pe.paid_to, self.data.payable)  # the company default payable account
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[
				(self.data.payable, 5000, 0, self.employee, day(-19)),
				(egp.pdc_payable, 0, 5000, None, day(-19)),
			],
		)

		# the bank settlement does not depend on the party
		cheque.clear(clearance_date=day(-5))
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Cleared", "Bank Posted"))

	def test_incoming_cheque_from_shareholder(self):
		egp = self.data.egp
		shareholder = ensure_shareholder()
		cheque = make_pdc_cheque(
			"Incoming", party_type="Shareholder", party=shareholder, amount=3000, issue_date=day(-20)
		)
		self.assertEqual(cheque.party_name, SHAREHOLDER_TITLE)
		cheque.receive(receipt_date=day(-19))
		cheque.reload()

		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual(
			(pe.payment_type, pe.party_type, pe.party_name), ("Receive", "Shareholder", SHAREHOLDER_TITLE)
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[
				(egp.pdc_receivable, 3000, 0, None, day(-19)),
				(self.data.payable, 0, 3000, shareholder, day(-19)),
			],
		)

	def test_party_type_must_exist(self):
		cheque = frappe.new_doc("PDC Cheque")
		cheque.update({"direction": "Outgoing", "company": COMPANY, "party_type": "Bank Account"})
		cheque.party = bank_account_name("EGP")
		with self.assertRaisesRegex(frappe.ValidationError, "is not a Party Type"):
			cheque.validate_party()
		self.assertRaises(
			frappe.ValidationError, make_pdc_cheque, party_type="Bank Account", party=bank_account_name("EGP")
		)

	def test_reference_rules_per_party(self):
		pi = make_purchase_invoice(1000, posting_date=day(-20))
		invoice_row = {
			"reference_doctype": "Purchase Invoice",
			"reference_name": pi.name,
			"allocated_amount": 1000,
		}
		self.assertRaises(
			frappe.ValidationError,
			make_pdc_cheque,
			party_type="Employee",
			party=self.employee,
			references=[invoice_row],
		)

		journal = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": day(-20),
				"accounts": [
					{
						"account": self.data.payable,
						"party_type": "Employee",
						"party": self.employee,
						"credit_in_account_currency": 1000,
					},
					{
						"account": acc("Cost of Goods Sold"),
						"debit_in_account_currency": 1000,
						"cost_center": acc("Main"),
					},
				],
			}
		)
		journal.insert(ignore_permissions=True)
		journal.submit()
		cheque = make_pdc_cheque(
			party_type="Employee",
			party=self.employee,
			references=[
				{"reference_doctype": "Journal Entry", "reference_name": journal.name, "allocated_amount": 1000}
			],
		)
		self.assertEqual([r.reference_name for r in cheque.references], [journal.name])

		# an incoming cheque from a Supplier cannot allocate invoices either (only Journal Entries)
		self.assertRaises(
			frappe.ValidationError,
			make_pdc_cheque,
			"Incoming",
			party_type="Supplier",
			party=self.data.supplier,
			references=[invoice_row],
		)

	# ------------------------------------------------------------------ New Payee

	def test_create_payee(self):
		group = ensure_payee_supplier_group()
		self.assertTrue(group)
		self.use_payee_group(group)
		name = f"Ali Rent {self.tag}"

		frappe.set_user(TREASURY_USER)
		try:
			result = create_payee(f"  {name}  ")
			again = create_payee(name.lower())
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(
			(result["party_type"], result["party_name"], result["created"]), ("Supplier", name, True)
		)
		supplier = frappe.db.get_value(
			"Supplier", result["party"], ["supplier_name", "supplier_group", "owner"], as_dict=True
		)
		self.assertEqual((supplier.supplier_name, supplier.supplier_group), (name, group))
		self.assertEqual(supplier.owner, TREASURY_USER)
		self.assertEqual((again["party"], again["created"]), (result["party"], False))

		# the payee can be used as the party of a cheque at once
		cheque = make_pdc_cheque(party=result["party"])
		self.assertEqual(cheque.party_name, name)

	def test_create_payee_errors(self):
		group = ensure_payee_supplier_group()
		self.use_payee_group(group)
		disabled = ensure_supplier(f"_Test Treasury Disabled Payee {self.tag}")
		frappe.db.set_value("Supplier", disabled, "disabled", 1)

		for value in ("", "   ", "x" * 141, disabled):
			with self.subTest(value=value[:20]):
				self.assertRaises(frappe.ValidationError, create_payee, value)

		frappe.set_user(NO_CHEQUE_USER)
		try:
			self.assertRaises(frappe.PermissionError, create_payee, f"Blocked {self.tag}")
		finally:
			frappe.set_user("Administrator")

		# a group node cannot hold suppliers
		self.use_payee_group(frappe.db.get_value("Supplier Group", {"is_group": 1}, "name"))
		self.assertRaises(frappe.ValidationError, create_payee, f"No Group {self.tag}")
		self.assertFalse(frappe.db.exists("Supplier", {"supplier_name": f"No Group {self.tag}"}))

	# ------------------------------------------------------------------ security cheque, report

	def test_security_cheque_of_employee_is_encashed(self):
		egp = self.data.egp
		security = make_security_cheque(
			amount=2500,
			party_type="Employee",
			party=self.employee,
			purpose="Employee Custody",
			issue_date=day(-90),
		)
		security.receive(receipt_date=day(-80), custody_location=ensure_custody_location())
		security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		security.reload()
		self.assertEqual(security.status, "Encashed")

		pdc = frappe.get_doc("PDC Cheque", security.converted_pdc_cheque)
		self.assertEqual(
			(pdc.party_type, pdc.party, pdc.party_name), ("Employee", self.employee, self.employee_name)
		)
		self.assertEqual(pdc.accounting_status, "Party Posted")
		assert_gl_entries(
			self,
			"Payment Entry",
			pdc.party_payment_entry,
			[
				(egp.pdc_receivable, 2500, 0, None, day(-5)),
				(self.data.payable, 0, 2500, self.employee, day(-5)),
			],
		)

		other = make_security_cheque(party_type="Other", party=None, party_name_other=f"Board {self.tag}")
		other.receive(receipt_date=day(-50), custody_location=ensure_custody_location())
		self.assertRaises(
			frappe.ValidationError, other.encash, day(-5), None, None, bank_account_name("EGP")
		)

	def test_projected_party_statement_of_employee(self):
		cheque = make_pdc_cheque(
			party_type="Employee", party=self.employee, amount=700, issue_date=day(-20), due_date=day(-5)
		)
		cheque.deliver(handover_date=day(-19))
		columns, data, *_rest = projected_party_statement(
			{"company": COMPANY, "party_type": "Employee", "party": self.employee}
		)
		self.assertTrue(columns)
		self.assertTrue(data)

"""Phase 7a: Accounting Details (cost center, project, mode of payment, party bank account, contact person)
and Accounting Dimensions on PDC Cheque / Cheque Batch — defaults, validation, lock, and their copy to every
voucher and to the cheques of a batch (spec 7a §2.4, §2.5, §7 D).

The Accounting Dimension on Branch is a persistent fixture of the test site (`ensure_branch_dimension`):
creating it is DDL, so it is created once, before any test data, and never deleted.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.tests.utils import (
	ABBR,
	BANK,
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	PE_SETTINGS,
	TreasuryTestMixin,
	bank_account_name,
	day,
	ensure_branch,
	ensure_branch_dimension,
	ensure_company,
	ensure_employee,
	ensure_mode,
	ensure_supplier,
	get_pe,
	make_pdc_cheque,
	set_mode_map,
	unique_cheque_no,
)

DETAILS_SUPPLIER = "_Test Treasury Details Supplier"
OTHER_SUPPLIER = "_Test Treasury Details Other Supplier"


class TestAccountingDetails(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch_field = ensure_branch_dimension()  # before any test data: see the module docstring
		super().setUpClass()
		ensure_supplier(DETAILS_SUPPLIER)
		ensure_supplier(OTHER_SUPPLIER)
		cls.cost_center = cls.ensure_cost_center("Treasury Details")
		cls.default_cost_center = frappe.get_cached_value("Company", COMPANY, "cost_center")

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)
		self.tag = frappe.generate_hash(length=5)

	# ------------------------------------------------------------------ fixtures

	@staticmethod
	def ensure_cost_center(name: str) -> str:
		full_name = f"{name} - {ABBR}"
		if not frappe.db.exists("Cost Center", full_name):
			frappe.get_doc(
				{
					"doctype": "Cost Center",
					"cost_center_name": name,
					"parent_cost_center": f"{COMPANY} - {ABBR}",
					"company": COMPANY,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		return full_name

	def party_bank_account(self, supplier: str, is_default: int = 1) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Bank Account",
					"account_name": f"Party {self.tag} {supplier[-12:]}",
					"bank": BANK,
					"party_type": "Supplier",
					"party": supplier,
					"is_company_account": 0,
					"is_default": is_default,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def contact(self, supplier: str, primary: int = 1) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": f"Contact {self.tag}",
					"is_primary_contact": primary,
					"links": [{"link_doctype": "Supplier", "link_name": supplier}],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def project(self) -> str:
		return (
			frappe.get_doc({"doctype": "Project", "project_name": f"Treasury {self.tag}", "company": COMPANY})
			.insert(ignore_permissions=True)
			.name
		)

	# ------------------------------------------------------------------ defaults

	def test_defaults(self):
		egp = self.data.egp
		mode = ensure_mode(f"Details Out {self.tag}", account=egp.bank_gl)
		set_mode_map(
			self, [{"bank_account": egp.bank_account, "method": "Cheque Out", "mode_of_payment": mode}]
		)
		supplier = ensure_supplier(f"_Test Treasury Details Defaults {self.tag}")
		bank_account = self.party_bank_account(supplier)
		contact = self.contact(supplier)

		cheque = make_pdc_cheque(party=supplier, issue_date=day(-10), due_date=day(20))
		self.assertEqual(
			(cheque.cost_center, cheque.mode_of_payment, cheque.party_bank_account, cheque.contact_person),
			(self.default_cost_center, mode, bank_account, contact),
		)
		self.assertFalse(cheque.project)

		# an Employee gets no contact person
		employee_cheque = make_pdc_cheque(
			party_type="Employee", party=ensure_employee(), issue_date=day(-10), due_date=day(20)
		)
		self.assertFalse(employee_cheque.contact_person)
		self.assertEqual(employee_cheque.cost_center, self.default_cost_center)

	def test_vouchers_carry_the_details(self):
		egp = self.data.egp
		project = self.project()
		bank_account = self.party_bank_account(DETAILS_SUPPLIER, is_default=0)
		contact = self.contact(DETAILS_SUPPLIER, primary=0)
		cheque = make_pdc_cheque(
			party=DETAILS_SUPPLIER,
			issue_date=day(-40),
			due_date=day(-20),
			cost_center=self.cost_center,
			project=project,
			party_bank_account=bank_account,
			contact_person=contact,
			remarks="July rent",
		)
		cheque.deliver(handover_date=day(-39))
		cheque.clear(clearance_date=day(-10), bank_charges=5)
		cheque.reload()

		party = get_pe(cheque.party_payment_entry)
		self.assertEqual(
			(party.cost_center, party.project, party.party_bank_account, party.contact_person),
			(self.cost_center, project, bank_account, contact),
		)
		self.assertEqual((party.reference_no, getdate(party.reference_date)), (cheque.cheque_no, day(-20)))
		self.assertEqual(party.bank_account, egp.bank_account)
		self.assertIn(cheque.name, party.remarks)
		self.assertIn("July rent", party.remarks)

		bank = get_pe(cheque.bank_payment_entry)
		self.assertEqual((bank.cost_center, bank.project), (self.cost_center, project))
		self.assertFalse(bank.party_bank_account or bank.contact_person)
		self.assertEqual([d.cost_center for d in bank.deductions], [self.cost_center])
		self.assertIn("July rent", bank.remarks)

	def test_dimension_copied_to_vouchers_and_gl(self):
		branch = ensure_branch()
		cheque = make_pdc_cheque(
			"Incoming", amount=6000, issue_date=day(-40), due_date=day(-20), **{self.branch_field: branch}
		)
		cheque.receive(receipt_date=day(-39))
		cheque.deposit(deposit_date=day(-20))
		cheque.clear(clearance_date=day(-18), bank_charges=10)
		cheque.reload()

		vouchers = (
			cheque.party_payment_entry,
			cheque.under_collection_payment_entry,
			cheque.bank_payment_entry,
		)
		for name in vouchers:
			pe = get_pe(name)
			self.assertEqual(pe.get(self.branch_field), branch, pe.payment_type)
			gl = frappe.get_all(
				"GL Entry",
				filters={"voucher_type": "Payment Entry", "voucher_no": name, "is_cancelled": 0},
				pluck=self.branch_field,
			)
			self.assertTrue(gl)
			self.assertEqual(set(gl), {branch}, name)
		bank = get_pe(cheque.bank_payment_entry)
		self.assertEqual([d.get(self.branch_field) for d in bank.deductions], [branch])

	def test_dimension_hook_and_fields(self):
		doctypes = frappe.get_hooks("accounting_dimension_doctypes")
		for doctype in ("PDC Cheque", "Cheque Batch", "Payment Entry", "Journal Entry Account"):
			self.assertIn(doctype, doctypes)
		for doctype in ("PDC Cheque", "Cheque Batch"):
			self.assertTrue(frappe.get_meta(doctype).has_field(self.branch_field), doctype)

	# ------------------------------------------------------------------ validation and lock

	def test_validation_errors(self):
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		other_cost_center = frappe.get_cached_value("Company", OTHER_COMPANY, "cost_center")
		foreign_contact = self.contact(OTHER_SUPPLIER)
		foreign_bank_account = self.party_bank_account(OTHER_SUPPLIER, is_default=0)
		cases = {
			"cost center of another company": {"cost_center": other_cost_center},
			"group cost center": {"cost_center": f"{COMPANY} - {ABBR}"},
			"contact not linked": {"contact_person": foreign_contact},
			"party bank account of another party": {"party_bank_account": foreign_bank_account},
			"company bank account": {"party_bank_account": bank_account_name("EGP")},
		}
		for case, values in cases.items():
			with self.subTest(case=case):
				self.assertRaises(
					frappe.ValidationError,
					make_pdc_cheque,
					party=DETAILS_SUPPLIER,
					cheque_no=unique_cheque_no(),
					**values,
				)

	def test_lock_after_posting(self):
		cheque = make_pdc_cheque(party=DETAILS_SUPPLIER, issue_date=day(-10), due_date=day(20))
		# not posted: the details may still change
		cheque.cost_center = self.cost_center
		cheque.save()
		cheque.deliver(handover_date=day(-9))
		cheque.reload()
		self.assertEqual(get_pe(cheque.party_payment_entry).cost_center, self.cost_center)

		cheque.cost_center = self.default_cost_center
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be changed after the cheque is posted"):
			cheque.save()
		cheque.reload()
		cheque.set(self.branch_field, ensure_branch())
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be changed after the cheque is posted"):
			cheque.save()

	def test_bank_change_before_posting_takes_the_new_banks_mode(self):
		egp = self.data.egp
		usd = self.data.usd
		egp_mode = ensure_mode(f"Details In EGP {self.tag}", account=egp.bank_gl)
		usd_mode = ensure_mode(f"Details In USD {self.tag}", account=usd.bank_gl)
		set_mode_map(
			self,
			[
				{"bank_account": egp.bank_account, "method": "Cheque In", "mode_of_payment": egp_mode},
				{"bank_account": usd.bank_account, "method": "Cheque In", "mode_of_payment": usd_mode},
			],
		)
		cheque = make_pdc_cheque("Incoming", issue_date=day(-10), due_date=day(20), bank_account=None)
		self.assertFalse(cheque.mode_of_payment)
		cheque.bank_account = egp.bank_account
		cheque.save()
		self.assertEqual(cheque.mode_of_payment, egp_mode)
		# the mode that came with the old bank follows the bank
		cheque.bank_account = usd.bank_account
		cheque.save()
		self.assertEqual((cheque.currency, cheque.mode_of_payment), ("USD", usd_mode))
		# a mode chosen by the user is kept
		chosen = ensure_mode(f"Details Chosen {self.tag}")
		cheque.mode_of_payment = chosen
		cheque.save()
		cheque.bank_account = egp.bank_account
		cheque.save()
		self.assertEqual(cheque.mode_of_payment, chosen)

	# ------------------------------------------------------------------ Cheque Batch

	def test_batch_copies_the_details(self):
		branch = ensure_branch()
		project = self.project()
		batch = frappe.get_doc(
			{
				"doctype": "Cheque Batch",
				"direction": "Outgoing",
				"company": COMPANY,
				"party_type": "Supplier",
				"party": DETAILS_SUPPLIER,
				"bank_account": bank_account_name("EGP"),
				"issue_date": day(-60),
				"handover_date": day(-60),
				"cost_center": self.cost_center,
				"project": project,
				self.branch_field: branch,
				"cheques": [
					{"due_date": day(-30 + 30 * i), "amount": 1000, "cheque_no": unique_cheque_no()}
					for i in range(2)
				],
			}
		)
		batch.insert()
		self.assertEqual(batch.cost_center, self.cost_center)
		batch.submit()

		cheques = frappe.get_all(
			"PDC Cheque",
			filters={"cheque_batch": batch.name},
			fields=["name", "cost_center", "project", self.branch_field, "party_payment_entry"],
			order_by="creation asc",
		)
		self.assertEqual(len(cheques), 2)
		expected = (self.cost_center, project, branch)
		for row in cheques:
			self.assertEqual((row.cost_center, row.project, row.get(self.branch_field)), expected)
			pe = get_pe(row.party_payment_entry)
			self.assertEqual((pe.cost_center, pe.project, pe.get(self.branch_field)), expected)

		replaced = frappe.get_doc("PDC Cheque", cheques[0].name)
		values = {"cheque_no": unique_cheque_no(), "replacement_date": day(-50)}
		result = replaced.replace(new_cheque_values=values)
		new = frappe.get_doc("PDC Cheque", result["replaced_by"])
		self.assertEqual(
			(new.cost_center, new.project, new.get(self.branch_field)), (self.cost_center, project, branch)
		)

"""Phase 7d: Bank Charge validation, protected fields, cancel / amend, refunds and reconciliation (spec 7d §3.3,
§3.4, §7 B)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.bank_transfers.doctype.bank_charge.bank_charge import make_refund
from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	acc,
	bank_account_name,
	day,
	ensure_account,
	ensure_charge_category,
	ensure_company,
	ensure_user,
	get_charge,
	get_voucher_count,
	make_bank_charge,
	make_bank_transaction,
	set_bank_suppliers,
)

DOCTYPE = "Bank Charge"
TREASURY_USER = "treasury-bank-charge-user@example.com"
READ_ONLY_USER = "treasury-bank-charge-reader@example.com"


class TestBankChargeLifecycle(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(READ_ONLY_USER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		set_bank_suppliers(self, [])

	# ------------------------------------------------------------------ validation

	def test_validation_errors(self):
		def rejects(pattern, **values):
			self.assertRaisesRegex(frappe.ValidationError, pattern, make_bank_charge, submit=False, **values)

		rejects("greater than zero", amount=0)
		rejects("greater than zero", amount=-5)

		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		other_gl = ensure_account("BC Other Co Bank", "Bank Accounts", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR)
		other_account = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": f"BC Other {frappe.generate_hash(length=5)}",
				"bank": frappe.db.get_value("Bank Account", bank_account_name("EGP"), "bank"),
				"account": other_gl,
				"company": OTHER_COMPANY,
				"is_company_account": 1,
			}
		).insert(ignore_permissions=True)
		rejects("must be a company bank account", bank_account=other_account.name)

		disabled = ensure_charge_category("_Test BC Disabled Category", keywords=())
		frappe.db.set_value("Bank Charge Category", disabled, "disabled", 1)
		self.addCleanup(frappe.db.set_value, "Bank Charge Category", disabled, "disabled", 0)
		rejects("is disabled", category=disabled)

		no_account = ensure_charge_category("_Test BC No Account", keywords=(), account=False)
		rejects("Set the Expense Account of Bank Charge Category", category=no_account)

		rejects("Root Type", expense_account=acc("Creditors"))
		rejects("group account", expense_account=acc("Indirect Expenses"))

		other_cost_center = frappe.get_cached_value("Company", OTHER_COMPANY, "cost_center")
		rejects("does not belong to company", cost_center=other_cost_center)

		# refunds: 100 + 60 of 150 -> the second one is too much
		charge = make_bank_charge()
		make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=100)
		rejects("cannot exceed", entry_type="Refund", against_bank_charge=charge.name, amount=60)

		draft = make_bank_charge(submit=False)
		rejects("not a submitted charge", entry_type="Refund", against_bank_charge=draft.name, amount=10)
		cancelled = make_bank_charge()
		cancelled.cancel()
		rejects("not a submitted charge|Cannot link cancelled document", entry_type="Refund", against_bank_charge=cancelled.name, amount=10)
		usd_charge = make_bank_charge(bank_account=bank_account_name("USD"), amount=10)
		rejects("not a submitted charge", entry_type="Refund", against_bank_charge=usd_charge.name, amount=5)

		other_category = ensure_charge_category("_Test BC Other Category", keywords=())
		rejects(
			"category and expense account",
			entry_type="Refund",
			against_bank_charge=charge.name,
			category=other_category,
			amount=10,
		)

		# a charge refunds nothing
		plain = make_bank_charge(submit=False, against_bank_charge=charge.name)
		self.assertFalse(plain.against_bank_charge)

	def test_protected_fields(self):
		charge = make_bank_charge()
		for fieldname, value in (
			("status", "Reconciled"),
			("payment_entry", None),
			("bank_transaction", "BT-X"),
		):
			with self.subTest(fieldname=fieldname):
				doc = get_charge(charge.name)
				doc.set(fieldname, value)
				self.assertRaises(frappe.ValidationError, doc.save)

		draft = make_bank_charge(submit=False)
		draft.append("status_log", {"event": "Posted", "to_status": "Posted"})
		self.assertRaises(frappe.ValidationError, draft.save)

	# ------------------------------------------------------------------ cancel, amend

	def test_cancel_rules(self):
		charge = make_bank_charge()
		with self.set_user(TREASURY_USER):
			self.assertRaises(frappe.PermissionError, get_charge(charge.name).cancel)

		charge.cancel()
		charge.reload()
		self.assertEqual((charge.status, charge.accounting_status), ("Cancelled", "Not Posted"))
		self.assertEqual(frappe.db.get_value("Payment Entry", charge.payment_entry, "docstatus"), 2)
		self.assertEqual(charge.status_log[-1].event, "Cancelled")
		self.assertEqual(charge.status_log[-1].voucher_no, charge.payment_entry)

		charge = make_bank_charge()
		refund = make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=40)
		self.assertRaisesRegex(frappe.ValidationError, "has submitted refunds", get_charge(charge.name).cancel)
		refund.cancel()
		charge.reload()
		self.assertEqual(flt(charge.refunded_amount), 0)
		self.assertEqual(charge.status_log[-1].event, "Refund Cancelled")
		charge.cancel()
		self.assertEqual(get_charge(charge.name).status, "Cancelled")

	def test_voucher_guard(self):
		charge = make_bank_charge()
		pe = frappe.get_doc("Payment Entry", charge.payment_entry)
		self.assertRaisesRegex(frappe.ValidationError, "managed by", pe.cancel)

		self.use_company_settings(bank_charge_voucher="Journal Entry")
		charge = make_bank_charge()
		je = frappe.get_doc("Journal Entry", charge.journal_entry)
		self.assertRaisesRegex(frappe.ValidationError, "managed by", je.cancel)

	def test_cancel_reconciled_unreconciles_bt(self):
		bt = make_bank_transaction(bank_account_name("EGP"), day(-4), withdrawal=150)
		charge = make_bank_charge(bank_transaction=bt.name)
		self.assertEqual(charge.status, "Reconciled")
		bt.reload()
		self.assertEqual(bt.status, "Reconciled")

		charge.cancel()
		charge.reload()
		self.assertEqual((charge.status, charge.accounting_status), ("Cancelled", "Not Posted"))
		self.assertEqual(frappe.db.get_value("Payment Entry", charge.payment_entry, "docstatus"), 2)
		bt.reload()
		self.assertEqual(bt.status, "Unreconciled")
		self.assertEqual(flt(bt.unallocated_amount), 150)

	def test_amend(self):
		charge = make_bank_charge()
		charge.cancel()
		amended = frappe.copy_doc(get_charge(charge.name), ignore_no_copy=False)  # like the desk "Amend"
		amended.amended_from = charge.name
		amended.insert()
		self.assertEqual((amended.status, amended.accounting_status), ("Draft", "Not Posted"))
		self.assertEqual(amended.amended_from, charge.name)
		self.assertFalse(amended.payment_entry or amended.journal_entry or amended.bank_transaction)
		self.assertEqual([r.event for r in amended.status_log], ["Created"])

	# ------------------------------------------------------------------ refunds

	def test_make_refund(self):
		charge = make_bank_charge(amount=150)
		count = frappe.db.count(DOCTYPE)
		refund = make_refund(charge.name)
		self.assertEqual(frappe.db.count(DOCTYPE), count)
		self.assertEqual(
			(refund["entry_type"], refund["against_bank_charge"], flt(refund["amount"])),
			("Refund", charge.name, 150),
		)
		for fieldname in ("company", "bank_account", "category", "expense_account", "cost_center"):
			self.assertEqual(refund[fieldname], charge.get(fieldname), fieldname)

		make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=100)
		self.assertEqual(flt(make_refund(charge.name)["amount"]), 50)
		make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=50)
		self.assertRaisesRegex(frappe.ValidationError, "nothing left", make_refund, charge.name)

		other = make_bank_charge(amount=90)
		with self.set_user(READ_ONLY_USER):
			self.assertRaises(frappe.PermissionError, make_refund, other.name)

	# ------------------------------------------------------------------ reconciliation

	def test_reconcile_action(self):
		self.use_company_settings(bank_charge_clearance="On Bank Transaction")
		egp = bank_account_name("EGP")
		charge = make_bank_charge()
		self.assertFalse(frappe.db.get_value("Payment Entry", charge.payment_entry, "clearance_date"))

		# the wrong statement lines
		other_bank_bt = make_bank_transaction(bank_account_name("USD"), day(-3), withdrawal=150)
		deposit_bt = make_bank_transaction(egp, day(-3), deposit=150)
		other_amount_bt = make_bank_transaction(egp, day(-3), withdrawal=151)
		for bt, pattern in (
			(other_bank_bt, "not an open transaction"),
			(deposit_bt, "does not match"),
			(other_amount_bt, "does not match"),
		):
			with self.subTest(bt=bt.name):
				self.assertRaisesRegex(
					frappe.ValidationError, pattern, get_charge(charge.name).reconcile_bank_transaction, bt.name
				)

		bt = make_bank_transaction(egp, day(-3), withdrawal=150, reference_number="BCR-1")
		result = get_charge(charge.name).reconcile_bank_transaction(bt.name)
		self.assertEqual(result["status"], "Reconciled")
		charge.reload()
		self.assertEqual((charge.bank_transaction, getdate(charge.bank_transaction_date)), (bt.name, day(-3)))
		self.assertEqual(getdate(charge.clearance_date), day(-3))
		self.assertEqual(
			getdate(frappe.db.get_value("Payment Entry", charge.payment_entry, "clearance_date")), day(-3)
		)
		bt.reload()
		self.assertEqual(bt.status, "Reconciled")
		self.assertEqual(charge.status_log[-1].event, "Reconciled")
		self.assertRaisesRegex(
			frappe.ValidationError, "already Reconciled", get_charge(charge.name).reconcile_bank_transaction, bt.name
		)

		# a statement line already used by another charge
		used_bt = make_bank_transaction(egp, day(-3), withdrawal=150)
		make_bank_charge(submit=False, bank_transaction=used_bt.name)
		other = make_bank_charge()
		self.assertRaisesRegex(
			frappe.ValidationError, "already has Bank Charge", other.reconcile_bank_transaction, used_bt.name
		)

		# a refund reconciles against a deposit
		refund = make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=30)
		deposit = make_bank_transaction(egp, day(-2), deposit=30)
		refund.reconcile_bank_transaction(deposit.name)
		self.assertEqual(get_charge(refund.name).status, "Reconciled")
		deposit.reload()
		self.assertEqual(deposit.status, "Reconciled")

	def test_submit_with_bank_transaction(self):
		bt = make_bank_transaction(bank_account_name("EGP"), day(-3), withdrawal=150)
		charge = make_bank_charge(bank_transaction=bt.name)
		self.assertEqual((charge.status, charge.accounting_status), ("Reconciled", "Posted"))
		self.assertEqual(getdate(charge.bank_transaction_date), day(-3))
		# the statement date wins over On Posting
		self.assertEqual(
			getdate(frappe.db.get_value("Payment Entry", charge.payment_entry, "clearance_date")), day(-3)
		)
		self.assertEqual([r.event for r in charge.status_log], ["Created", "Posted", "Reconciled"])
		bt.reload()
		self.assertEqual(bt.status, "Reconciled")
		self.assertEqual(get_voucher_count(DOCTYPE, charge.name), 1)

		self.assertRaisesRegex(
			frappe.ValidationError, "is not an open transaction", make_bank_charge, submit=False, bank_transaction=bt.name
		)
		open_bt = make_bank_transaction(bank_account_name("EGP"), day(-3), withdrawal=150)
		make_bank_charge(submit=False, bank_transaction=open_bt.name)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"already has Bank Charge",
			make_bank_charge,
			submit=False,
			bank_transaction=open_bt.name,
		)

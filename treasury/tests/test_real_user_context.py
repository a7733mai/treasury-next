"""Vouchers are built, submitted and cancelled under the REAL session user (not Administrator), and ERPNext's
role-based accounting guards apply to that user, while Treasury's own permission + role check stays the
authorization gate (treasury.utils.context.service_context)."""

import frappe
from erpnext.accounts.party import get_party_account as erpnext_get_party_account
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from treasury.accounting import posting
from treasury.bank_transfers.scheduler import post_scheduled_transfers
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	SUPPLIER,
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	day,
	ensure_user,
	make_bank_transfer,
	make_pdc_cheque,
	make_purchase_invoice,
)
from treasury.utils.context import REAL_USER_FLAG, real_user, service_context

TREASURY_USER = "treasury-real-user@example.com"
TREASURY_MANAGER = "treasury-real-manager@example.com"
OUTSIDER = "treasury-real-outsider@example.com"
FROZEN_MODIFIER_ROLE = "_Test Treasury Frozen Modifier"
FROZEN_MODIFIER_USER = "treasury-real-frozen-modifier@example.com"
SAVEPOINT = "treasury_real_user_context"


class TestRealUserContext(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Role", FROZEN_MODIFIER_ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": FROZEN_MODIFIER_ROLE, "desk_access": 1}).insert(
				ignore_permissions=True
			)
		ensure_user(TREASURY_USER, ["Treasury User", "Cheque Approver"])
		ensure_user(TREASURY_MANAGER, ["Treasury Manager"])
		ensure_user(OUTSIDER, ["Accounts User"])
		ensure_user(FROZEN_MODIFIER_USER, ["Treasury User", "Cheque Approver", FROZEN_MODIFIER_ROLE])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	# ------------------------------------------------------------------ helpers

	def assert_authored_by(self, doctype: str, name: str, user: str):
		"""The voucher, its GL rows and its Version history carry `user`."""
		owner, modified_by = frappe.db.get_value(doctype, name, ["owner", "modified_by"])
		self.assertEqual((owner, modified_by), (user, user), (doctype, name))
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": doctype, "voucher_no": name},
			fields=["owner", "modified_by"],
		)
		self.assertTrue(gl, f"no GL Entry of {doctype} {name}")
		self.assertEqual({(r.owner, r.modified_by) for r in gl}, {(user, user)}, f"GL Entry of {name}")
		versions = frappe.get_all("Version", filters={"ref_doctype": doctype, "docname": name}, pluck="owner")
		self.assertEqual(set(versions) - {user}, set(), f"Version of {name}")

	# ------------------------------------------------------------------ the context itself

	def test_service_context_keeps_the_session_user(self):
		with self.set_user(TREASURY_USER):
			with service_context() as acting:
				self.assertEqual(acting, TREASURY_USER)
				self.assertEqual(frappe.session.user, TREASURY_USER)
				self.assertEqual(real_user(), TREASURY_USER)
				self.assertTrue(frappe.flags.ignore_account_permission)
				with service_context(actor="Administrator"):
					self.assertEqual(real_user(), TREASURY_USER)  # nested blocks keep the outer actor
			self.assertIsNone(frappe.flags.get(REAL_USER_FLAG))
			self.assertFalse(frappe.flags.get("ignore_account_permission"))

	def test_service_context_restores_flags_after_an_exception(self):
		with self.set_user(TREASURY_USER):
			with self.assertRaises(ValueError):
				with service_context():
					raise ValueError("forced")
			self.assertEqual(frappe.session.user, TREASURY_USER)
			self.assertIsNone(frappe.flags.get(REAL_USER_FLAG))
			self.assertFalse(frappe.flags.get("ignore_account_permission"))

	def test_party_account_matches_erpnext(self):
		# the permission-free resolution must not drift from ERPNext's own (called as Administrator)
		for party_type, party in (("Supplier", SUPPLIER), ("Customer", CUSTOMER)):
			self.assertEqual(
				posting.get_party_account(party_type, party, COMPANY),
				erpnext_get_party_account(party_type, party, COMPANY),
				party,
			)
		with self.set_user(TREASURY_USER):
			self.assertFalse(frappe.has_permission("Account", "read"))
			self.assertEqual(posting.get_party_account("Supplier", SUPPLIER, COMPANY), self.data.payable)

	# ------------------------------------------------------------------ vouchers carry the real user

	def test_pdc_receive_deposit_clear_flow_is_authored_by_the_user(self):
		cheque = make_pdc_cheque("Incoming", amount=1500, issue_date=day(-50), due_date=day(-20))
		with self.set_user(TREASURY_MANAGER):
			cheque.receive(receipt_date=day(-49))
			cheque.deposit(deposit_date=day(-21))
			cheque.clear(clearance_date=day(-19))
			self.assertEqual(frappe.session.user, TREASURY_MANAGER)
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Cleared", "Bank Posted"))
		self.assert_authored_by("Payment Entry", cheque.party_payment_entry, TREASURY_MANAGER)
		self.assert_authored_by("Journal Entry", cheque.under_collection_journal, TREASURY_MANAGER)
		self.assert_authored_by("Journal Entry", cheque.bank_journal, TREASURY_MANAGER)

	def test_cancelled_voucher_gl_is_written_by_the_canceller(self):
		egp = self.data.egp
		self.use_company_settings(on_bounce="Cancel Payment Entry", bounce_charges_account=egp.bank_charges)
		cheque = make_pdc_cheque("Incoming", amount=900, issue_date=day(-50), due_date=day(-20))
		with self.set_user(TREASURY_USER):
			cheque.receive(receipt_date=day(-49))
		with self.set_user(TREASURY_MANAGER):
			cheque.bounce(bounce_date=day(-10), reason="Insufficient funds")
		cheque.reload()
		pe = cheque.party_payment_entry
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "modified_by"), TREASURY_MANAGER)
		cancelled = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Payment Entry", "voucher_no": pe, "is_cancelled": 1},
			fields=["owner", "modified_by"],
		)
		self.assertTrue(cancelled)
		# the originals were written by the receiving user, the reversing rows by the cancelling manager
		self.assertIn(TREASURY_USER, {r.owner for r in cancelled})
		self.assertIn(TREASURY_MANAGER, {r.owner for r in cancelled})

	def test_bank_transfer_flow_is_authored_by_the_user(self):
		self.use_company_settings(**TRANSFER_SETTINGS)
		with self.set_user(TREASURY_USER):
			transfer = make_bank_transfer(submit=False)
			transfer.submit()
			self.assertEqual(frappe.session.user, TREASURY_USER)
		transfer.reload()
		self.assertTrue(transfer.payment_entry)
		self.assert_authored_by("Payment Entry", transfer.payment_entry, TREASURY_USER)

	def test_scheduler_vouchers_stay_administrator(self):
		self.use_company_settings(**TRANSFER_SETTINGS)
		execution = getdate(add_days(today(), 3))
		with self.set_user(TREASURY_USER):
			transfer = make_bank_transfer(submit=False, execution_date=execution)
			transfer.submit()
		self.assertEqual(transfer.status, "Scheduled")
		with self.set_user("Administrator"):
			result = post_scheduled_transfers(as_of=execution)
		self.assertIn(transfer.name, result["posted"])
		transfer.reload()
		self.assert_authored_by("Payment Entry", transfer.payment_entry, "Administrator")

	# ------------------------------------------------------------------ the Treasury gate still applies

	def test_user_without_treasury_role_cannot_run_the_actions(self):
		cheque = make_pdc_cheque("Incoming", amount=500, issue_date=day(-50), due_date=day(-20))
		cheque.receive(receipt_date=day(-49))
		with self.set_user(OUTSIDER):
			self.assertRaises(frappe.PermissionError, cheque.deposit, day(-21))
			self.assertRaises(frappe.PermissionError, cheque.bounce, day(-10), "Stopped")
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Received", "Party Posted"))

		self.use_company_settings(**TRANSFER_SETTINGS)
		transfer = make_bank_transfer(execution_date=getdate(add_days(today(), 3)))
		self.assertEqual(transfer.status, "Scheduled")
		with self.set_user(OUTSIDER):
			self.assertRaises(frappe.PermissionError, transfer.send_now)
		transfer.reload()
		self.assertEqual(transfer.status, "Scheduled")

	# ------------------------------------------------------------------ ERPNext's role-based guard: frozen accounts

	def freeze_accounts(self, upto):
		"""Company: books frozen up to `upto`, modifiable by FROZEN_MODIFIER_ROLE only (ERPNext 16 fields)."""
		for field, value in (
			("accounts_frozen_till_date", upto),
			("role_allowed_for_frozen_entries", FROZEN_MODIFIER_ROLE),
		):
			previous = frappe.db.get_value("Company", COMPANY, field)
			frappe.db.set_value("Company", COMPANY, field, value)
			self.addCleanup(frappe.db.set_value, "Company", COMPANY, field, previous)
		frappe.clear_cache()
		self.addCleanup(frappe.clear_cache)

	def test_frozen_accounts_apply_to_the_real_user(self):
		self.freeze_accounts(day(-40))
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)
		frappe.db.savepoint(SAVEPOINT)
		with self.set_user(TREASURY_USER):
			# the party Payment Entry is dated day(-59), inside the frozen period
			self.assertRaises(frappe.ValidationError, cheque.deliver, day(-59))
		frappe.db.rollback(save_point=SAVEPOINT)
		self.assertEqual(frappe.db.get_value("PDC Cheque", cheque.name, "operational_status"), "Created")

		with self.set_user(FROZEN_MODIFIER_USER):
			cheque.reload()
			cheque.deliver(day(-59))
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Delivered", "Party Posted"))
		self.assert_authored_by("Payment Entry", cheque.party_payment_entry, FROZEN_MODIFIER_USER)

	def test_posting_after_the_frozen_date_is_not_blocked(self):
		self.freeze_accounts(day(-70))
		cheque = make_pdc_cheque(amount=700, issue_date=day(-60), due_date=day(-30))
		with self.set_user(TREASURY_USER):
			cheque.deliver(day(-59))
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")

	# ------------------------------------------------------------------ allocation against an invoice

	def test_invoice_allocation_by_a_user_without_invoice_permission_leaves_no_share(self):
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)
		with self.set_user(TREASURY_USER):
			self.assertFalse(frappe.has_permission("Purchase Invoice", "read", pi.name))
			cheque.deliver(day(-59))
			# the temporary read share is gone again
			self.assertFalse(frappe.has_permission("Purchase Invoice", "read", pi.name))
		cheque.reload()
		self.assert_authored_by("Payment Entry", cheque.party_payment_entry, TREASURY_USER)
		shares = {"share_doctype": "Purchase Invoice", "share_name": pi.name}
		self.assertFalse(frappe.db.exists("DocShare", shares))
		reference = frappe.db.get_value(
			"Payment Entry Reference", {"parent": cheque.party_payment_entry}, "reference_name"
		)
		self.assertEqual(reference, pi.name)

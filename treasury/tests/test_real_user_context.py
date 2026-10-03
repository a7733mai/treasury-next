"""Vouchers are built, submitted and cancelled under the REAL session user (not Administrator), and ERPNext's
role-based accounting guards apply to that user, while Treasury's own permission + role check stays the
authorization gate (treasury.utils.context.service_context)."""

from unittest.mock import patch

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
	ensure_account,
	ensure_user,
	make_bank_transfer,
	make_pdc_cheque,
	make_purchase_invoice,
	make_purchase_order,
	make_sales_order,
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
		# the invoice is posted before the books are frozen
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)
		self.freeze_accounts(day(-40))
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
		# nor is the party left shared with the user
		self.assertFalse(
			frappe.db.exists("DocShare", {"share_doctype": "Supplier", "share_name": pi.supplier})
		)
		reference = frappe.db.get_value(
			"Payment Entry Reference", {"parent": cheque.party_payment_entry}, "reference_name"
		)
		self.assertEqual(reference, pi.name)

	def share_row(self, doctype: str, name: str, user: str, **perms):
		"""A DocShare row that exists before the Treasury action (rolled back with the class transaction)."""
		share = frappe.get_doc(
			{"doctype": "DocShare", "user": user, "share_doctype": doctype, "share_name": name, **perms}
		)
		share.flags.ignore_share_permission = True
		return share.insert(ignore_permissions=True)

	def test_preexisting_share_without_read_is_restored_not_deleted(self):
		"""A DocShare row of the user that exists but grants no read: the temporary read access is added to it
		and, on exit, the row is back exactly as it was (never deleted)."""
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)
		prior = self.share_row("Purchase Invoice", pi.name, TREASURY_USER, read=0, write=0, share=0, submit=0)
		with self.set_user(TREASURY_USER):
			self.assertFalse(frappe.has_permission("Purchase Invoice", "read", pi.name))
			cheque.deliver(day(-59))
			self.assertFalse(frappe.has_permission("Purchase Invoice", "read", pi.name))
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")
		after = frappe.db.get_value(
			"DocShare", prior.name, ["read", "write", "share", "submit", "user"], as_dict=True
		)
		self.assertIsNotNone(after, "the pre-existing share row was deleted")
		self.assertEqual(
			(after.read, after.write, after.share, after.submit, after.user),
			(0, 0, 0, 0, TREASURY_USER),
		)

	def test_preexisting_read_share_is_kept(self):
		"""The user already holds a read share of the invoice: nothing is added and the row stays as it was."""
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)
		prior = self.share_row("Purchase Invoice", pi.name, TREASURY_USER, read=1, write=0, share=0)
		with self.set_user(TREASURY_USER):
			cheque.deliver(day(-59))
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")
		after = frappe.db.get_value("DocShare", prior.name, ["read", "write", "share"], as_dict=True)
		self.assertIsNotNone(after, "the pre-existing share row was deleted")
		self.assertEqual((after.read, after.write, after.share), (1, 0, 0))

	# ------------------------------------------------------------------ orders, advance accounts, side effects

	def assert_no_share_left(self, *documents: tuple[str, str]):
		for doctype, name in documents:
			self.assertFalse(
				frappe.db.exists(
					"DocShare", {"share_doctype": doctype, "share_name": name, "user": TREASURY_USER}
				),
				f"{doctype} {name} is still shared with the user",
			)
			self.assertFalse(
				frappe.has_permission(doctype, "read", name, user=TREASURY_USER), (doctype, name)
			)

	def enable_separate_advance_accounts(self):
		"""The company books advances in a separate party account (restored by a cleanup)."""
		fields = [
			"book_advance_payments_in_separate_party_account",
			"default_advance_received_account",
			"default_advance_paid_account",
		]
		previous = frappe.db.get_value("Company", COMPANY, fields, as_dict=True)
		self.addCleanup(self.set_company_values, dict(previous))
		self.set_company_values(
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_received_account": ensure_account(
					"Treasury Advance Received", "Current Liabilities", "Receivable"
				),
				"default_advance_paid_account": ensure_account(
					"Treasury Advance Paid", "Current Assets", "Payable"
				),
			}
		)
		return frappe.db.get_value("Company", COMPANY, fields, as_dict=True)

	@staticmethod
	def set_company_values(values: dict):
		frappe.db.set_value("Company", COMPANY, values)
		frappe.clear_document_cache("Company", COMPANY)

	def test_sales_order_allocation_by_a_treasury_user_posts_and_leaves_no_share(self):
		so = make_sales_order(2500)
		cheque = make_pdc_cheque("Incoming", amount=2500, issue_date=day(-5), due_date=day(20), invoice=so)
		with self.set_user(TREASURY_USER):
			self.assertFalse(frappe.has_permission("Account", "read", self.data.receivable))
			cheque.receive(receipt_date=day(-4))
			self.assertFalse(frappe.has_permission("Account", "read", self.data.receivable))
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")
		self.assert_authored_by("Payment Entry", cheque.party_payment_entry, TREASURY_USER)
		self.assert_no_share_left(
			("Sales Order", so.name), ("Customer", so.customer), ("Account", self.data.receivable)
		)

	def test_purchase_order_allocation_by_a_treasury_user_posts_and_leaves_no_share(self):
		po = make_purchase_order(1800)
		cheque = make_pdc_cheque(amount=1800, issue_date=day(-5), due_date=day(20), invoice=po)
		with self.set_user(TREASURY_USER):
			cheque.deliver(day(-4))
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")
		self.assert_authored_by("Payment Entry", cheque.party_payment_entry, TREASURY_USER)
		self.assert_no_share_left(
			("Purchase Order", po.name), ("Supplier", po.supplier), ("Account", self.data.payable)
		)

	def test_separate_advance_accounts_for_a_treasury_user(self):
		"""Company.book_advance_payments_in_separate_party_account: ERPNext also reads the party's advance account
		(set_liability_account) - for a receive against an order and a pay against an order."""
		company = self.enable_separate_advance_accounts()
		advance_received = company.default_advance_received_account
		advance_paid = company.default_advance_paid_account
		so = make_sales_order(1200)
		receive = make_pdc_cheque("Incoming", amount=1200, issue_date=day(-5), due_date=day(20), invoice=so)
		po = make_purchase_order(700)
		pay = make_pdc_cheque(amount=700, issue_date=day(-5), due_date=day(20), invoice=po)
		with self.set_user(TREASURY_USER):
			self.assertFalse(frappe.has_permission("Account", "read", advance_received))
			receive.receive(receipt_date=day(-4))
			pay.deliver(day(-4))
			self.assertFalse(frappe.has_permission("Account", "read", advance_received))
			self.assertFalse(frappe.has_permission("Account", "read", advance_paid))
		for cheque in (receive, pay):
			cheque.reload()
			self.assertEqual(cheque.accounting_status, "Party Posted")
			self.assert_authored_by("Payment Entry", cheque.party_payment_entry, TREASURY_USER)
		self.assertEqual(
			frappe.db.get_value("Payment Entry", receive.party_payment_entry, "paid_from"), advance_received
		)
		self.assertFalse(
			frappe.get_all("DocShare", filters={"user": TREASURY_USER, "share_doctype": "Account"}),
		)
		self.assert_no_share_left(
			("Sales Order", so.name),
			("Purchase Order", po.name),
			("Account", advance_received),
			("Account", advance_paid),
		)

	def test_temporary_share_leaves_no_follow_row_and_no_timeline_comment(self):
		"""A user with follow_shared_documents + document_follow_notify: the technical share neither makes the
		user follow the invoice nor adds a Shared / Un-shared Comment to it."""
		frappe.db.set_value(
			"User", TREASURY_USER, {"follow_shared_documents": 1, "document_follow_notify": 1}
		)
		frappe.clear_document_cache("User", TREASURY_USER)
		self.addCleanup(self.reset_follow_flags)
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		so = make_sales_order(900)
		shared = (("Purchase Invoice", pi.name), ("Sales Order", so.name))
		comments_before = frappe.db.count("Comment", {"comment_type": ["in", ["Shared", "Unshared"]]})
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)
		receive = make_pdc_cheque("Incoming", amount=900, issue_date=day(-5), due_date=day(20), invoice=so)
		with self.set_user(TREASURY_USER):
			cheque.deliver(day(-59))
			receive.receive(receipt_date=day(-4))
		for doctype, name in shared:
			self.assertFalse(
				frappe.db.exists("Document Follow", {"ref_doctype": doctype, "ref_docname": name})
			)
		self.assertFalse(frappe.db.exists("Document Follow", {"user": TREASURY_USER}))
		self.assertEqual(
			frappe.db.count("Comment", {"comment_type": ["in", ["Shared", "Unshared"]]}), comments_before
		)
		self.assert_no_share_left(*shared)

	def reset_follow_flags(self):
		frappe.db.set_value(
			"User", TREASURY_USER, {"follow_shared_documents": 0, "document_follow_notify": 0}
		)
		frappe.clear_document_cache("User", TREASURY_USER)

	def test_cleanup_failure_does_not_mask_the_original_exception(self):
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		reference = frappe._dict(reference_doctype="Purchase Invoice", reference_name=pi.name)
		self.addCleanup(
			frappe.db.delete, "DocShare", {"share_doctype": "Purchase Invoice", "share_name": pi.name}
		)

		def failing_delete(*args, **kwargs):
			raise RuntimeError("cleanup failed")

		with self.set_user(TREASURY_USER):
			with patch.object(frappe.db, "delete", failing_delete), patch.object(frappe, "log_error") as log:
				with self.assertRaises(ValueError):
					with posting._reference_read_access([reference]):
						raise ValueError("original")
				self.assertTrue(log.called)
				# no original exception: the cleanup failure surfaces
				with self.assertRaises(RuntimeError):
					with posting._reference_read_access([reference]):
						pass

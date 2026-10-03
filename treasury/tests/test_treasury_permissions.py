"""Treasury roles without any ERPNext accounting role can run the posting actions end to end.

The app grants the Treasury roles no permission on core doctypes: the posting service builds the vouchers
as the real user in `treasury.utils.context.service_context` after the lifecycle checked the user's permission
on the cheque. Also covers the one-time removal of the Custom DocPerm grants of earlier versions.
"""

import json
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry
from frappe.cache_manager import clear_user_cache
from frappe.permissions import add_permission, reset_perms
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.install import (
	LEGACY_CLEANUP_DONE_KEY,
	LEGACY_GRANTS_KEY,
	LEGACY_PERMISSION_DOCTYPES,
	LEGACY_PERMISSION_ROLES,
	remove_legacy_permissions,
)
from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	assert_gl_entries,
	day,
	ensure_charge_category,
	ensure_user,
	get_party_outstanding,
	make_bank_charge,
	make_pdc_cheque,
	make_purchase_invoice,
	make_sales_invoice,
	set_bank_suppliers,
)
from treasury.utils.context import REAL_USER_FLAG, real_user, service_context

TREASURY_USER = "treasury-perm-user@example.com"
TREASURY_MANAGER = "treasury-perm-manager@example.com"
SAVEPOINT = "treasury_legacy_permissions_test"
# stands for another app's role holding Custom DocPerm rows on core doctypes (hrms: Employee Self Service)
OTHER_APP_ROLE = "_Test Treasury Other App Role"
SAVEPOINT_FAILED_POSTING = "treasury_failed_posting_test"


class TestTreasuryRolePermissions(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(TREASURY_USER, ["Treasury User", "Cheque Approver"])
		ensure_user(TREASURY_MANAGER, ["Treasury Manager"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	def assert_status(self, doc, operational: str, accounting: str):
		doc.reload()
		self.assertEqual((doc.operational_status, doc.accounting_status), (operational, accounting))

	def assert_owned_by(self, doctype: str, name: str, user: str):
		owner, modified_by = frappe.db.get_value(doctype, name, ["owner", "modified_by"])
		self.assertEqual((owner, modified_by), (user, user), (doctype, name))

	def test_no_custom_docperm_on_core_doctypes(self):
		# after_migrate removed the legacy grants; other Custom DocPerm rows (an administrator's own
		# customisations of other roles) are none of the app's business
		self.assertTrue(frappe.db.get_global(LEGACY_CLEANUP_DONE_KEY))
		for doctype in LEGACY_PERMISSION_DOCTYPES:
			self.assertFalse(
				frappe.db.exists(
					"Custom DocPerm", {"parent": doctype, "role": ["in", LEGACY_PERMISSION_ROLES]}
				),
				doctype,
			)

	def test_service_context_restores_state_after_exception(self):
		with self.set_user(TREASURY_USER):
			with self.assertRaises(ValueError):
				with service_context():
					self.assertEqual(frappe.session.user, TREASURY_USER)  # the session is never switched
					self.assertEqual(real_user(), TREASURY_USER)
					raise ValueError("forced")
			self.assertEqual(frappe.session.user, TREASURY_USER)
			self.assertIsNone(frappe.flags.get(REAL_USER_FLAG))
			self.assertEqual(real_user(), TREASURY_USER)

	def test_failed_posting_restores_user(self):
		# the voucher fails inside the posting service's service_context block (as a closed period would)
		cheque = make_pdc_cheque(amount=1111, issue_date=day(-60), due_date=day(-30))
		frappe.db.savepoint(SAVEPOINT_FAILED_POSTING)
		self.addCleanup(frappe.db.rollback, save_point=SAVEPOINT_FAILED_POSTING)
		with (
			self.set_user(TREASURY_USER),
			patch.object(PaymentEntry, "on_submit", side_effect=frappe.ValidationError("forced")),
		):
			self.assertRaises(frappe.ValidationError, cheque.deliver, day(-59))
			self.assertEqual(frappe.session.user, TREASURY_USER)
			self.assertIsNone(frappe.flags.get(REAL_USER_FLAG))
		self.assertEqual(frappe.db.get_value("PDC Cheque", cheque.name, "operational_status"), "Created")

	def test_roles_hold_no_voucher_permission(self):
		for user in (TREASURY_USER, TREASURY_MANAGER):
			for doctype in ("Payment Entry", "Journal Entry"):
				for ptype in ("read", "write", "create", "submit", "cancel"):
					self.assertFalse(frappe.has_permission(doctype, ptype, user=user), (user, doctype, ptype))
			self.assertFalse(frappe.has_permission("Account", "read", user=user), user)

	def test_treasury_user_delivers_and_clears(self):
		egp = self.data.egp
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)

		with self.set_user(TREASURY_USER):
			cheque.deliver(handover_date=day(-59))
			self.assertEqual(frappe.session.user, TREASURY_USER)  # the service_context block was left
			self.assert_status(cheque, "Delivered", "Party Posted")
			cheque.clear(clearance_date=day(-25))
			self.assert_status(cheque, "Cleared", "Bank Posted")

		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(self.data.payable, 1000, 0, SUPPLIER, day(-59)),
				(egp.pdc_payable, 0, 1000, None, day(-59)),
			],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.pdc_payable, 1000, 0, None, day(-25)), (egp.bank_gl, 0, 1000, None, day(-25))],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		# the status log and the vouchers record the real user, not Administrator
		self.assertEqual(cheque.status_log[-1].user, TREASURY_USER)
		self.assertEqual({row.user for row in cheque.status_log[1:]}, {TREASURY_USER})
		self.assert_owned_by("Payment Entry", cheque.party_payment_entry, TREASURY_USER)
		self.assert_owned_by("Journal Entry", cheque.bank_journal, TREASURY_USER)

	def test_treasury_manager_receives_and_bounces(self):
		egp = self.data.egp
		si = make_sales_invoice(2000, posting_date=day(-50))
		cheque = make_pdc_cheque("Incoming", amount=2000, issue_date=day(-50), due_date=day(-20), invoice=si)

		with self.set_user(TREASURY_MANAGER):
			cheque.receive(receipt_date=day(-49))
			self.assert_status(cheque, "Received", "Party Posted")
			cheque.bounce(bounce_date=day(-10), reason="Insufficient funds")
			self.assert_status(cheque, "Bounced", "Reversed")

		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(egp.pdc_receivable, 2000, 0, None, day(-49)),
				(self.data.receivable, 0, 2000, CUSTOMER, day(-49)),
			],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.reversal_journal,
			[
				(self.data.receivable, 2000, 0, CUSTOMER, day(-10)),
				(egp.pdc_receivable, 0, 2000, None, day(-10)),
			],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 2000)
		self.assertEqual(getdate(cheque.handover_date), day(-49))
		self.assert_owned_by("Journal Entry", cheque.reversal_journal, TREASURY_MANAGER)

	def test_treasury_manager_full_incoming_cycle_with_bounce_charges_and_cancellation(self):
		# deposit (Under Collection), bounce with charges (party account lookup) and voucher cancellation
		egp = self.data.egp
		self.use_company_settings(on_bounce="Cancel Payment Entry", bounce_charges_account=egp.bank_charges)
		cheque = make_pdc_cheque("Incoming", amount=3000, issue_date=day(-50), due_date=day(-20))

		with self.set_user(TREASURY_MANAGER):
			cheque.receive(receipt_date=day(-49))
			cheque.deposit(deposit_date=day(-21))
			self.assert_status(cheque, "Deposited", "Under Collection Posted")
			cheque.bounce(bounce_date=day(-10), reason="Insufficient funds", charges=75)
			self.assert_status(cheque, "Bounced", "Reversed")
			self.assertEqual(frappe.session.user, TREASURY_MANAGER)

		self.assertEqual(frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "docstatus"), 2)
		self.assertEqual(
			frappe.db.get_value("Journal Entry", cheque.under_collection_journal, "docstatus"), 2
		)
		self.assertEqual(
			frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "modified_by"), TREASURY_MANAGER
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bounce_charges_journal,
			[
				(self.data.receivable, 75, 0, CUSTOMER, day(-10)),
				(egp.bank_charges, 0, 75, None, day(-10)),
			],
		)

	def test_bank_charge_roles(self):
		"""Phase 7d: a Treasury User creates and submits a Bank Charge (its Payment Entry is built in service_context and
		owned by the user) but cannot cancel it; an Accounts User may read charges, not create them."""
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		set_bank_suppliers(self, [])
		category = ensure_charge_category()
		with self.set_user(TREASURY_USER):
			charge = make_bank_charge(category=category, amount=321)
			self.assertEqual((charge.status, charge.accounting_status), ("Posted", "Posted"))
			self.assertRaises(frappe.PermissionError, charge.cancel)
		self.assert_owned_by("Payment Entry", charge.payment_entry, TREASURY_USER)
		self.assertEqual(charge.status_log[-1].user, TREASURY_USER)

		reader = ensure_user("treasury-perm-charge-reader@example.com", ["Accounts User"])
		self.assertTrue(frappe.has_permission("Bank Charge", "read", doc=charge.name, user=reader))
		with self.set_user(reader):
			self.assertRaises(frappe.PermissionError, make_bank_charge, category=category, amount=322)

	def test_role_check_still_applies(self):
		# elevation happens only inside the posting service, after the lifecycle's permission / role checks
		cheque = make_pdc_cheque("Incoming", amount=500, issue_date=day(-50), due_date=day(-20))
		cheque.receive(receipt_date=day(-49))
		with self.set_user(TREASURY_USER):
			self.assertRaises(frappe.PermissionError, cheque.bounce, day(-10), "Stopped")
		self.assert_status(cheque, "Received", "Party Posted")

		outsider = ensure_user("treasury-perm-outsider@example.com", ["Accounts User"])
		with self.set_user(outsider):
			self.assertRaises(frappe.PermissionError, cheque.deposit, day(-21))
		self.assert_status(cheque, "Received", "Party Posted")

	def test_treasury_alert_log(self):
		"""Phase 7e: the Treasury Manager reads the alert log (overrides audit), the Treasury User does not;
		nobody creates one through the desk (written by the system only)."""
		self.assertTrue(frappe.has_permission("Treasury Alert Log", "read", user=TREASURY_MANAGER))
		self.assertFalse(frappe.has_permission("Treasury Alert Log", "read", user=TREASURY_USER))
		for user in (TREASURY_MANAGER, TREASURY_USER):
			for ptype in ("create", "write", "delete"):
				self.assertFalse(frappe.has_permission("Treasury Alert Log", ptype, user=user), (user, ptype))
		meta = frappe.get_meta("Treasury Alert Log")
		self.assertEqual((meta.in_create, meta.read_only, meta.track_changes), (1, 1, 0))
		log = frappe.get_doc(
			{
				"doctype": "Treasury Alert Log",
				"alert_type": "Digest",
				"company": COMPANY,
				"alert_date": day(0),
			}
		)
		with self.set_user(TREASURY_MANAGER):
			self.assertRaises(frappe.PermissionError, log.insert)


class TestLegacyPermissionCleanup(IntegrationTestCase):
	"""`remove_legacy_permissions` undoes the Custom DocPerm grants of earlier versions, and only those.

	Other apps keep Custom DocPerm rows on the same core doctypes (hrms: its "Employee Self Service" user type
	adds `select` on Currency / Mode of Payment / Company / Account ..., HR User / HR Manager read Currency), so
	the tests never assume a doctype without customisation: `clean_site` first puts the doctypes they use back
	to their standard permissions (rolled back with the savepoint), and `other_app_row` recreates such a row
	explicitly. They pass the same with or without hrms installed."""

	def setUp(self):
		super().setUp()
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(self.restore)
		frappe.db.set_global(LEGACY_GRANTS_KEY, None)

	@staticmethod
	def restore():
		frappe.db.rollback(save_point=SAVEPOINT)
		for doctype in LEGACY_PERMISSION_DOCTYPES:
			frappe.clear_cache(doctype=doctype)
		clear_user_cache()
		frappe.clear_cache(user="__global")  # the cleanup markers (global defaults) written in the test

	@staticmethod
	def clean_site(*doctypes: str):
		"""The doctypes as on a site without any other app or customisation: standard permissions only."""
		for doctype in doctypes:
			reset_perms(doctype)

	@staticmethod
	def legacy_grant(doctype: str, role: str):
		"""What the legacy version did: copy the standard permissions, then add a read row for the role."""
		add_permission(doctype, role, 0, "read")

	@staticmethod
	def other_app_row(doctype: str) -> str:
		"""A Custom DocPerm row of another app's role (like hrms's Employee Self Service `select` rows)."""
		if not frappe.db.exists("Role", OTHER_APP_ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": OTHER_APP_ROLE, "desk_access": 1}).insert(
				ignore_permissions=True
			)
		name = add_permission(doctype, OTHER_APP_ROLE, 0)
		frappe.db.set_value("Custom DocPerm", name, {"select": 1, "read": 0, "export": 0})
		return name

	@staticmethod
	def treasury_rows(doctype: str) -> list[str]:
		return frappe.get_all(
			"Custom DocPerm",
			filters={"parent": doctype, "role": ["in", LEGACY_PERMISSION_ROLES]},
			pluck="name",
		)

	def test_copied_permissions_are_reset_to_standard(self):
		self.clean_site("Mode of Payment")
		for role in LEGACY_PERMISSION_ROLES:
			self.legacy_grant("Mode of Payment", role)
		self.assertTrue(self.treasury_rows("Mode of Payment"))

		reset = remove_legacy_permissions(force=True)

		self.assertIn("Mode of Payment", reset)
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Mode of Payment"}))
		self.assertTrue(frappe.db.get_global(LEGACY_CLEANUP_DONE_KEY))

	def test_other_apps_permissions_are_kept(self):
		# hrms-like: another app's role holds Custom DocPerm rows on the doctype, before and after the legacy
		# grant; the Treasury rows go, every other row stays, and the doctype is not reset to standard
		self.clean_site("Mode of Payment", "Currency")
		before = self.other_app_row("Mode of Payment")
		for role in LEGACY_PERMISSION_ROLES:
			self.legacy_grant("Mode of Payment", role)
			self.legacy_grant("Currency", role)
		after = self.other_app_row("Currency")
		kept = frappe.get_all(
			"Custom DocPerm",
			filters={
				"parent": ["in", ("Mode of Payment", "Currency")],
				"role": ["not in", LEGACY_PERMISSION_ROLES],
			},
			pluck="name",
		)

		reset = remove_legacy_permissions(force=True)

		self.assertNotIn("Mode of Payment", reset)
		self.assertNotIn("Currency", reset)
		self.assertFalse(self.treasury_rows("Mode of Payment") + self.treasury_rows("Currency"))
		for name in (before, after, *kept):
			self.assertTrue(frappe.db.exists("Custom DocPerm", name), name)
		self.assertEqual(frappe.db.get_value("Custom DocPerm", before, ["select", "read"]), (1, 0))

	def test_other_customisations_are_kept(self):
		self.clean_site("Bank")
		self.legacy_grant("Bank", "Treasury User")
		# an administrator's own change on another role of the copied permissions
		row = frappe.get_all(
			"Custom DocPerm",
			filters={"parent": "Bank", "role": ["not in", LEGACY_PERMISSION_ROLES]},
			fields=["name", "role", "export"],
			limit=1,
		)[0]
		frappe.db.set_value("Custom DocPerm", row.name, "export", 0 if row.export else 1)

		reset = remove_legacy_permissions(force=True)

		self.assertNotIn("Bank", reset)
		self.assertFalse(self.treasury_rows("Bank"))
		self.assertTrue(frappe.db.exists("Custom DocPerm", row.name))

	def test_recorded_grants_undo_only_what_treasury_did(self):
		# the legacy record: Treasury created the Treasury Manager row on Bank (and caused the copy), and only
		# switched `read` on in a Treasury User row an administrator had made before
		self.clean_site("Bank", "Currency")
		admin_row = add_permission("Bank", "Treasury User", 0)
		frappe.db.set_value("Custom DocPerm", admin_row, {"read": 0, "select": 1, "report": 1})
		self.legacy_grant("Bank", "Treasury Manager")
		frappe.db.set_value("Custom DocPerm", admin_row, "read", 1)
		# a Treasury role row the legacy version never recorded (made by an administrator): kept
		unrecorded = add_permission("Currency", "Treasury Manager", 0)
		frappe.db.set_global(
			LEGACY_GRANTS_KEY,
			json.dumps(
				{
					"grants": [
						{"doctype": "Bank", "role": "Treasury Manager", "ptype": "read", "created_row": True},
						{
							"doctype": "Bank",
							"role": "Treasury Manager",
							"ptype": "select",
							"created_row": True,
						},
						{"doctype": "Bank", "role": "Treasury User", "ptype": "read", "created_row": False},
					],
					"copied": [],
				}
			),
		)

		reset = remove_legacy_permissions(force=True)

		self.assertEqual(reset, [])
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Bank", "role": "Treasury Manager"}))
		self.assertEqual(
			frappe.db.get_value("Custom DocPerm", admin_row, ["read", "select", "report"]), (0, 1, 1)
		)
		self.assertTrue(frappe.db.exists("Custom DocPerm", unrecorded))
		self.assertIsNone(frappe.db.get_global(LEGACY_GRANTS_KEY))

	def test_recorded_copy_is_reset_to_standard(self):
		self.clean_site("Bank")
		self.legacy_grant("Bank", "Treasury User")
		frappe.db.set_global(
			LEGACY_GRANTS_KEY,
			json.dumps(
				{
					"grants": [
						{"doctype": "Bank", "role": "Treasury User", "ptype": "read", "created_row": True}
					],
					"copied": ["Bank"],
				}
			),
		)

		self.assertEqual(remove_legacy_permissions(force=True), ["Bank"])
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Bank"}))

	def test_runs_once_and_is_idempotent(self):
		self.clean_site("Currency")
		remove_legacy_permissions(force=True)
		# a grant made by an administrator after the cleanup is left alone by later migrates
		self.legacy_grant("Currency", "Treasury Manager")
		self.assertEqual(remove_legacy_permissions(), [])
		self.assertTrue(self.treasury_rows("Currency"))
		# forced again: removes it, and a second forced run finds nothing to do
		self.assertEqual(remove_legacy_permissions(force=True), ["Currency"])
		self.assertEqual(remove_legacy_permissions(force=True), [])
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Currency"}))

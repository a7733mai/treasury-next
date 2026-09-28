"""Treasury roles without any ERPNext accounting role can run the posting actions end to end.

ERPNext checks the session user's own permissions while the posting service builds vouchers (e.g. Account in
`get_party_account`), so the roles get the read access of `treasury.install.READ_PERMISSIONS` — and no
more: vouchers are only ever created by the posting service.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.install import PERMISSION_ROLES, READ_PERMISSIONS, grant_permissions
from treasury.tests.utils import (
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	assert_gl_entries,
	day,
	ensure_user,
	get_party_outstanding,
	make_pdc_cheque,
	make_purchase_invoice,
	make_sales_invoice,
)

TREASURY_USER = "treasury-perm-user@example.com"
TREASURY_MANAGER = "treasury-perm-manager@example.com"


class TestTreasuryRolePermissions(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		grant_permissions()  # done by after_install / after_migrate; a no-op when already granted
		ensure_user(TREASURY_USER, ["Treasury User", "Cheque Approver"])
		ensure_user(TREASURY_MANAGER, ["Treasury Manager"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	def assert_status(self, doc, operational: str, accounting: str):
		doc.reload()
		self.assertEqual((doc.operational_status, doc.accounting_status), (operational, accounting))

	def test_roles_get_read_access_only(self):
		for user in (TREASURY_USER, TREASURY_MANAGER):
			for doctype, ptypes in READ_PERMISSIONS.items():
				for ptype in ptypes:
					self.assertTrue(frappe.has_permission(doctype, ptype, user=user), (user, doctype, ptype))
			for doctype in ("Payment Entry", "Journal Entry"):
				for ptype in ("write", "create", "submit", "cancel"):
					self.assertFalse(frappe.has_permission(doctype, ptype, user=user), (user, doctype, ptype))
		self.assertEqual(PERMISSION_ROLES, ("Treasury Manager", "Treasury User"))

	def test_treasury_user_delivers_and_clears(self):
		egp = self.data.egp
		pi = make_purchase_invoice(1000, posting_date=day(-60))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)

		with self.set_user(TREASURY_USER):
			cheque.deliver(handover_date=day(-59))
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
		self.assertEqual(cheque.status_log[-1].user, TREASURY_USER)

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

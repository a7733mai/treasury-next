"""Phase 7h: who sees what on the Treasury Dashboard (spec 7h §3.1 / §7 B): sections gated by the read
permission of their DocType and the User Permissions on Company / Bank Account, the Page roles, and the setup
banner only for users who may read the company's settings."""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.pdc.overview import SECTIONS, get_overview
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	BalanceAlertTestMixin,
	add_user_permission,
	bank_account_name,
	ensure_company,
	ensure_user,
	make_alert_fixture_e1,
)

APPROVER = "treasury-dash-approver@example.com"
TREASURY_USER = "treasury-dash-user@example.com"
OTHER_CO_USER = "treasury-dash-otherco@example.com"
BANK_USER = "treasury-dash-bank@example.com"
OUTSIDER = "treasury-dash-outsider@example.com"
OUTSIDER_ROLE = "_Test Treasury Desk Role"


class TestTreasuryDashboardPermissions(BalanceAlertTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(APPROVER, ["Cheque Approver"])
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(OTHER_CO_USER, ["Treasury User"])
		add_user_permission(OTHER_CO_USER, "Company", OTHER_COMPANY)
		ensure_user(BANK_USER, ["Treasury User"])
		add_user_permission(BANK_USER, "Bank Account", bank_account_name("EGP"))
		if not frappe.db.exists("Role", OUTSIDER_ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": OUTSIDER_ROLE, "desk_access": 1}).insert(
				ignore_permissions=True
			)
		ensure_user(OUTSIDER, [OUTSIDER_ROLE])

	def setUp(self):
		super().setUp()
		self.fx = make_alert_fixture_e1(self)

	def test_sections_hidden_for_cheque_approver(self):
		with self.set_user(APPROVER):
			overview = get_overview(COMPANY, None, 7)
		self.assertEqual(set(overview.sections), {"positions", "cheques"})
		self.assertEqual(set(overview.hidden), set(SECTIONS) - {"positions", "cheques"})
		self.assertEqual(len(overview.hidden), 7)

	def test_company_user_permission(self):
		with self.set_user(OTHER_CO_USER):
			self.assertRaisesRegex(
				frappe.PermissionError,
				"not permitted to view the Treasury Dashboard",
				get_overview,
				COMPANY,
				None,
				7,
			)

	def test_bank_user_permission(self):
		with self.set_user(BANK_USER):
			self.assertRaises(frappe.PermissionError, get_overview, COMPANY, [self.fx.bank_account], 7)
			overview = get_overview(COMPANY, None, 7)
		banks = {b for position in overview.sections.positions.banks for b in position.bank_accounts}
		self.assertEqual(banks, {bank_account_name("EGP")})

	def test_page_roles(self):
		page = frappe.get_doc("Page", "treasury-dashboard")
		self.assertEqual(page.module, "PDC")
		with self.set_user(TREASURY_USER):
			self.assertTrue(page.is_permitted())
		with self.set_user(APPROVER):
			self.assertTrue(page.is_permitted())
		with self.set_user(OUTSIDER):
			self.assertFalse(page.is_permitted())

	def test_setup_summary_only_with_settings_access(self):
		with self.set_user(APPROVER):
			self.assertIsNone(get_overview(COMPANY, None, 7).setup)
		with self.set_user(TREASURY_USER):
			setup = get_overview(COMPANY, None, 7).setup
		self.assertEqual(setup.exists, 1)
		self.assertIn("missing", setup)
		self.assertEqual(setup.route, ["Form", "Treasury Company Settings", COMPANY])

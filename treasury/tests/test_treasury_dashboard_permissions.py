"""Phase 7h: who sees what on the Treasury Dashboard (spec 7h §3.1 / §7 B): sections gated by the read
permission of their DocType and the User Permissions on Company / Bank Account, the Page roles, and the setup
banner only for users who may read the company's settings."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from treasury.pdc import overview as overview_module
from treasury.pdc.overview import SECTIONS, get_overview
from treasury.tests.test_bank_positions import add_twin
from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	BalanceAlertTestMixin,
	add_user_permission,
	bank_account_name,
	ensure_charge_category,
	ensure_company,
	ensure_user,
	make_alert_fixture_e1,
	make_bank_charge,
	make_pdc_cheque,
	run_report,
)

APPROVER = "treasury-dash-approver@example.com"
E1_USER = "treasury-dash-e1@example.com"
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

	def test_bank_user_permission_excludes_rows(self):
		"""The cheques, transfers and charges of a forbidden bank count for nobody but those who may see it:
		new rows on the E1 bank move the Administrator's figures and leave the restricted user's unchanged."""
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		category = ensure_charge_category("_Test Dashboard Perm Charge", keywords=("dash perm charge",))

		def figures():
			sections = get_overview(COMPANY, None, 7).sections
			for key in ("cheques", "transfers", "charges"):
				self.assertIn(key, sections)
			return frappe._dict(
				due_out=flt(sections.cheques.due_out.amount, 2),
				issued=flt(sections.cheques.issued.amount, 2),
				awaiting=sections.transfers.awaiting.count,
				charges=flt(sections.charges.month.net, 2),
			)

		def delta(after, before, *keys):
			return tuple(flt(after[k] - before[k], 2) for k in keys)

		with self.set_user(BANK_USER):
			user_before = figures()
		admin_before = figures()

		# cheques and transfers (the transfer's own fee may count as a charge: charges are checked below)
		make_pdc_cheque(amount=777, due_date=self.day(2), bank_account=self.fx.bank_account)
		frappe.get_doc("Bank Transfer", self.fx.tr1).send_now()
		with self.set_user(BANK_USER):
			self.assertEqual(figures(), user_before)
		admin_mid = figures()
		self.assertEqual(delta(admin_mid, admin_before, "due_out", "issued", "awaiting"), (777, 777, 1))

		make_bank_charge(
			category=category, amount=55, posting_date=self.today, bank_account=self.fx.bank_account
		)
		with self.set_user(BANK_USER):
			self.assertEqual(figures(), user_before)
		self.assertEqual(delta(figures(), admin_mid, "charges"), (55,))

	def test_forbidden_gl_sibling_is_not_shown(self):
		"""A bank sharing the GL account of the selected one, that the user may not see, is neither named nor
		counted, and the positions stay visible (the engine is not handed the forbidden bank)."""
		user = ensure_user(E1_USER, ["Treasury User"])
		add_user_permission(user, "Bank Account", self.fx.bank_account)
		twin = add_twin(self.fx.bank)

		overview = get_overview(COMPANY, [self.fx.bank_account], 7)
		self.assertEqual(set(overview.bank_accounts), {self.fx.bank_account, twin})

		with self.set_user(user):
			overview = get_overview(COMPANY, [self.fx.bank_account], 7)
			self.assertEqual(overview.bank_accounts, [self.fx.bank_account])
			self.assertIn("positions", overview.sections)
			self.assertNotIn("positions", overview.hidden)
			listed = {b for p in overview.sections.positions.banks for b in p.bank_accounts}
			self.assertEqual(listed, {self.fx.bank_account})
			self.assertNotIn(twin, frappe.as_json(overview))
			# the Bank Balance Outlook drill-down neither raises nor names it
			_columns, data, *_rest = run_report(
				"Bank Balance Outlook",
				overview.sections.positions.totals.below_minimum.route_options,
			)
			self.assertNotIn(twin, frappe.as_json(data))

	def test_permission_error_in_a_section_hides_it(self):
		def forbidden(ctx):
			frappe.throw("forbidden inside the section", frappe.PermissionError)

		messages = len(frappe.local.message_log)
		with patch.dict(overview_module.SECTION_BUILDERS, {"transfers": forbidden}):
			overview = get_overview(COMPANY, None, 7)
		self.assertNotIn("transfers", overview.sections)
		self.assertIn("transfers", overview.hidden)
		self.assertIn("charges", overview.sections)
		# the section's error message is dropped: the response carries no error popup
		self.assertEqual(len(frappe.local.message_log), messages)

	def test_setup_route_only_for_users_who_may_create_settings(self):
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		frappe.db.delete("Treasury Company Settings", {"name": OTHER_COMPANY})
		with self.set_user(TREASURY_USER):  # reads the settings, may not create them
			setup = overview_module.get_setup(OTHER_COMPANY)
		self.assertEqual((setup.exists, setup.route, setup.route_options), (0, None, None))
		setup = overview_module.get_setup(OTHER_COMPANY)
		self.assertEqual(setup.route, ["Form", "Treasury Company Settings", "new"])
		self.assertEqual(setup.route_options, {"company": OTHER_COMPANY})

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

"""Phase 7d: PDC Bank Clearing with bank charges (spec 7d §3.5, §7 C): Posted charges are matched and
reconciled, unmatched withdrawals with a category keyword are proposed and created in one click.

Each test uses amounts of its own: the charges and statement lines of the other tests of the class stay until
the class rolls back."""

from unittest.mock import patch

import frappe
from frappe.permissions import reset_perms, update_permission_property
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	COMPANY,
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	acc,
	assert_gl_entries,
	day,
	ensure_charge_category,
	ensure_user,
	make_bank_charge,
	make_bank_transaction,
	make_bank_transfer,
	set_bank_suppliers,
	set_fee_rules,
)

TOOL = "PDC Bank Clearing"
DOCTYPE = "Bank Charge"
NO_CREATE_USER = "treasury-bank-charge-no-create@example.com"


def charges_of(bank_transaction: str) -> list[str]:
	return frappe.get_all(
		DOCTYPE, filters={"bank_transaction": bank_transaction, "docstatus": ["<", 2]}, pluck="name"
	)


class TestBankChargeClearing(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(NO_CREATE_USER, ["Treasury User", "Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**BANK_CHARGE_SETTINGS)
		set_bank_suppliers(self, [])
		self.category = ensure_charge_category()

	def tool(self, tolerance=3):
		tool = frappe.get_doc(TOOL)
		tool.company = COMPANY
		tool.bank_account = self.data.egp.bank_account
		tool.from_date = day(-30)
		tool.to_date = day(0)
		tool.date_tolerance = tolerance
		tool.set("matches", [])
		return tool

	@staticmethod
	def rows_for(tool, bt) -> list:
		return [row for row in tool.matches if row.bank_transaction == bt.name]

	@staticmethod
	def select_only(tool, *bts):
		names = {bt.name for bt in bts}
		for row in tool.matches:
			row.selected = 1 if row.bank_transaction in names else 0

	def assert_reconciled(self, bt, voucher_type, voucher):
		bt.reload()
		self.assertEqual(bt.status, "Reconciled")
		self.assertEqual(flt(bt.unallocated_amount), 0)
		self.assertEqual(
			[(p.payment_document, p.payment_entry) for p in bt.payment_entries], [(voucher_type, voucher)]
		)

	# ------------------------------------------------------------------ keyword proposals

	def test_keyword_proposal_create_and_reconcile(self):
		egp = self.data.egp
		bt = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=211.5, description="ACCT MAINT FEE SEP"
		)
		tool = self.tool()
		tool.get_suggestions()
		rows = self.rows_for(tool, bt)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(
			(
				row.match_quality,
				row.reference_doctype,
				row.bank_charge_category,
				row.selected,
				row.bank_charge,
			),
			("Keyword", DOCTYPE, self.category, 1, None),
		)
		self.assertEqual(
			(flt(row.amount), row.description, row.direction), (211.5, "ACCT MAINT FEE SEP", "Outgoing")
		)

		self.select_only(tool, bt)
		result = tool.clear_selected()
		name = row.bank_charge
		self.assertTrue(name)
		self.assertEqual(result["created"], [name])
		self.assertEqual(result["skipped"], [])
		charge = frappe.get_doc(DOCTYPE, name)
		self.assertEqual(
			(
				charge.docstatus,
				charge.status,
				getdate(charge.posting_date),
				flt(charge.amount),
				charge.bank_transaction,
			),
			(1, "Reconciled", day(-4), 211.5, bt.name),
		)
		self.assertEqual(charge.category, self.category)
		self.assertEqual(charge.description, "ACCT MAINT FEE SEP")
		self.assert_reconciled(bt, "Payment Entry", charge.payment_entry)
		assert_gl_entries(
			self,
			"Payment Entry",
			charge.payment_entry,
			[(acc("Treasury Bank Charges"), 211.5, 0, None, day(-4)), (egp.bank_gl, 0, 211.5, None, day(-4))],
		)

	def test_bulk(self):
		egp = self.data.egp
		other = ensure_charge_category("_Test BC Bulk Other", keywords=("chq bk fee",))
		no_account = ensure_charge_category(
			"_Test BC Bulk No Account", keywords=("nacct svc fee",), account=False
		)
		first = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=221.1, description="ACCT MAINT FEE 1"
		)
		second = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=221.2, reference_number="ACCT MAINT FEE 2"
		)
		third = make_bank_transaction(egp.bank_account, day(-4), withdrawal=221.3, description="CHQ BK FEE")
		failing = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=221.4, description="NACCT SVC FEE"
		)

		tool = self.tool()
		tool.get_suggestions()
		failing_row = self.rows_for(tool, failing)[0]
		self.assertEqual((failing_row.bank_charge_category, failing_row.selected), (no_account, 0))
		self.assertIn("Set the Expense Account", failing_row.result)
		self.assertEqual(self.rows_for(tool, third)[0].bank_charge_category, other)
		self.select_only(tool, first, second, third, failing)
		result = tool.clear_selected()

		self.assertEqual(len(result["created"]), 3)
		self.assertEqual([d["document"] for d in result["skipped"]], [failing.name])
		self.assertIn("Set the Expense Account", result["skipped"][0]["reason"])
		for bt in (first, second, third):
			self.assertEqual(len(charges_of(bt.name)), 1, bt.name)
		self.assertEqual(charges_of(failing.name), [])
		self.assertEqual(frappe.db.get_value(DOCTYPE, charges_of(third.name)[0], "category"), other)
		self.assertIsNone(self.rows_for(tool, failing)[0].bank_charge)

	def test_ambiguous_and_auto_select(self):
		egp = self.data.egp
		alpha = ensure_charge_category("_Test BC Tie A", keywords=("tie alpha",))
		ensure_charge_category("_Test BC Tie B", keywords=("tie bravo",))
		longer = ensure_charge_category("_Test BC Longer", keywords=("longer kw fee",))
		ensure_charge_category("_Test BC Shorter", keywords=("kw fee",))
		manual = ensure_charge_category("_Test BC Manual", keywords=("manual pick fee",), auto_select=0)
		disabled = ensure_charge_category("_Test BC Off", keywords=("switched off fee",))
		frappe.db.set_value("Bank Charge Category", disabled, "disabled", 1)
		self.addCleanup(frappe.db.set_value, "Bank Charge Category", disabled, "disabled", 0)

		tie = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=231.1, description="TIE ALPHA TIE BRAVO"
		)
		wins = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=231.2, description="Longer  KW fee"
		)
		unselected = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=231.3, description="MANUAL PICK FEE"
		)
		off = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=231.4, description="SWITCHED OFF FEE"
		)
		deposit = make_bank_transaction(
			egp.bank_account, day(-4), deposit=231.5, description="ACCT MAINT FEE"
		)

		tool = self.tool()
		tool.get_suggestions()
		row = self.rows_for(tool, tie)[0]
		self.assertEqual((row.bank_charge_category, row.selected), (alpha, 0))
		self.assertIn("Several categories match", row.result)
		row = self.rows_for(tool, wins)[0]
		self.assertEqual((row.bank_charge_category, row.selected), (longer, 1))
		row = self.rows_for(tool, unselected)[0]
		self.assertEqual((row.bank_charge_category, row.selected), (manual, 0))
		self.assertEqual(self.rows_for(tool, off), [])
		self.assertEqual(self.rows_for(tool, deposit), [])

	# ------------------------------------------------------------------ documents first

	def test_existing_charge_is_matched_not_proposed(self):
		egp = self.data.egp
		charge = make_bank_charge(amount=241.5, bank_reference="BC-77")
		bt = make_bank_transaction(
			egp.bank_account, day(-4), withdrawal=241.5, description="ACCT MAINT FEE BC-77"
		)
		tool = self.tool()
		tool.get_suggestions()
		rows = self.rows_for(tool, bt)
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].match_quality, rows[0].reference_doctype, rows[0].bank_charge, rows[0].selected),
			("Strong", DOCTYPE, charge.name, 1),
		)
		self.select_only(tool, bt)
		result = tool.clear_selected()
		self.assertEqual((result["cleared"], result["created"]), ([charge.name], []))
		charge.reload()
		self.assertEqual((charge.status, charge.bank_transaction), ("Reconciled", bt.name))
		self.assert_reconciled(bt, "Payment Entry", charge.payment_entry)

	def test_documents_before_keywords(self):
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, [])
		egp = self.data.egp
		transfer = make_bank_transfer(
			amount=2512.5, execution_date=day(-6), request_date=day(-6), bank_reference="FT-BC-5"
		)
		bt = make_bank_transaction(
			egp.bank_account, day(-5), withdrawal=2512.5, description="TRF FT-BC-5 ACCT MAINT FEE"
		)
		tool = self.tool()
		tool.get_suggestions()
		rows = self.rows_for(tool, bt)
		self.assertEqual(
			[(r.reference_doctype, r.bank_transfer) for r in rows], [("Bank Transfer", transfer.name)]
		)

	def test_idempotent_clearing(self):
		bt = make_bank_transaction(
			self.data.egp.bank_account, day(-4), withdrawal=261.5, description="ACCT MAINT FEE"
		)
		tool = self.tool()
		tool.get_suggestions()
		self.select_only(tool, bt)
		self.assertEqual(len(tool.clear_selected()["created"]), 1)

		row = self.rows_for(tool, bt)[0]
		row.selected = 1
		result = tool.clear_selected()
		self.assertEqual((result["created"], result["cleared"]), ([], []))
		self.assertEqual(len(result["skipped"]), 1)
		self.assertEqual(len(charges_of(bt.name)), 1)

	# ------------------------------------------------------------------ permissions

	def test_permissions(self):
		bt = make_bank_transaction(
			self.data.egp.bank_account, day(-4), withdrawal=271.5, description="ACCT MAINT FEE"
		)
		original = frappe.has_permission

		def no_charge_create(doctype=None, ptype="read", *args, **kwargs):
			if doctype == DOCTYPE and ptype in ("create", "submit"):
				if kwargs.get("throw"):
					raise frappe.PermissionError(f"No permission to {ptype} {DOCTYPE}")
				return False
			return original(doctype, ptype, *args, **kwargs)

		tool = self.tool()
		with patch("frappe.has_permission", side_effect=no_charge_create):
			tool.get_suggestions()
			self.assertEqual(self.rows_for(tool, bt), [])

			# a row forced into the tool is skipped
			tool.append(
				"matches",
				{
					"selected": 1,
					"match_quality": "Keyword",
					"reference_doctype": DOCTYPE,
					"bank_transaction": bt.name,
					"bank_charge_category": self.category,
					"amount": 271.5,
				},
			)
			self.select_only(tool, bt)
			result = tool.clear_selected()
		self.assertEqual(result["created"], [])
		self.assertEqual(len(result["skipped"]), 1)
		self.assertIn("No permission", result["skipped"][0]["reason"])
		self.assertEqual(charges_of(bt.name), [])

		# without the patch the proposal is there
		tool = self.tool()
		tool.get_suggestions()
		self.assertEqual(len(self.rows_for(tool, bt)), 1)

	def test_permissions_real_user(self):
		"""The same through the real DocPerm resolution: a Treasury User whose role has lost Bank Charge create
		(Custom DocPerm, as an administrator would set in Role Permissions Manager)."""
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": DOCTYPE}))
		update_permission_property(DOCTYPE, "Treasury User", 0, "create", 0)
		self.addCleanup(frappe.clear_cache, doctype=DOCTYPE)
		self.addCleanup(reset_perms, DOCTYPE)
		frappe.clear_cache(doctype=DOCTYPE)
		bt = make_bank_transaction(
			self.data.egp.bank_account, day(-4), withdrawal=281.5, description="ACCT MAINT FEE"
		)

		tool = self.tool()
		with self.set_user(NO_CREATE_USER):
			self.assertFalse(frappe.has_permission(DOCTYPE, "create"))
			tool.get_suggestions()
			self.assertEqual(self.rows_for(tool, bt), [])
			tool.append(
				"matches",
				{
					"selected": 1,
					"match_quality": "Keyword",
					"reference_doctype": DOCTYPE,
					"bank_transaction": bt.name,
					"bank_charge_category": self.category,
					"amount": 281.5,
				},
			)
			self.select_only(tool, bt)
			result = tool.clear_selected()
		self.assertEqual(result["created"], [])
		self.assertEqual([d["document"] for d in result["skipped"]], [bt.name])
		self.assertEqual(charges_of(bt.name), [])

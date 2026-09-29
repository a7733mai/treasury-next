"""Install / uninstall: what the app adds to core doctypes is exactly what `before_uninstall` removes.

The uninstall itself is not run (it would delete the custom fields of the test site); the checks compare the
list of fields it deletes with the fields install creates and with the fields present on the site.
"""

import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.nestedset import get_root_of

from treasury import install
from treasury.accounting import dimensions, payment_modes
from treasury.patches.v1_1 import set_bank_settlement_voucher
from treasury.tests.utils import COMPANY, ensure_branch_dimension, setup_treasury_test_data
from treasury.utils import party
from treasury.utils.party import DEFAULT_PAYEE_GROUP, ensure_payee_supplier_group


class TestInstall(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# persistent fixture (DDL): created before any test data, see treasury.tests.utils
		cls.dimension_field = ensure_branch_dimension()
		super().setUpClass()

	def test_payee_supplier_group(self):
		# the steps of `setup` (after_install / after_migrate): the group, then the Treasury Settings
		# defaults. Run here: the test depends neither on the site's migrate history nor on the setup wizard.
		self.assertEqual(ensure_payee_supplier_group(), DEFAULT_PAYEE_GROUP)
		install.set_default_settings()

		self.assertEqual(frappe.db.get_value("Supplier Group", DEFAULT_PAYEE_GROUP, "is_group"), 0)
		parent = frappe.db.get_value("Supplier Group", DEFAULT_PAYEE_GROUP, "parent_supplier_group")
		self.assertEqual(parent, get_root_of("Supplier Group"))
		self.assertEqual(frappe.db.get_value("Supplier Group", parent, "is_group"), 1)
		# idempotent
		self.assertEqual(ensure_payee_supplier_group(), DEFAULT_PAYEE_GROUP)
		self.assertEqual(frappe.db.count("Supplier Group", {"name": DEFAULT_PAYEE_GROUP}), 1)
		self.assertTrue(frappe.db.get_single_value("Treasury Settings", "payee_supplier_group"))

	def test_payee_supplier_group_on_a_site_without_root(self):
		# bare site (setup wizard never run): the root is created as a group first, the leaf goes under
		# it. The inserts are recorded, not run: a second root cannot be created on a site that has one.
		with (
			patch.object(party, "get_root_of", return_value=None),
			patch.object(party.frappe.db, "exists", return_value=False),
			patch.object(party.frappe.db, "get_value", return_value=1),
			patch.object(party.frappe, "get_doc") as get_doc,
		):
			self.assertEqual(ensure_payee_supplier_group(), DEFAULT_PAYEE_GROUP)
		inserted = [c.args[0] for c in get_doc.call_args_list]
		self.assertEqual(
			[(d["supplier_group_name"], d["parent_supplier_group"], d["is_group"]) for d in inserted],
			[(party.ROOT_SUPPLIER_GROUP, "", 1), (DEFAULT_PAYEE_GROUP, party.ROOT_SUPPLIER_GROUP, 0)],
		)

	def test_bank_settlement_voucher_patch(self):
		setup_treasury_test_data()
		frappe.db.set_value(
			"Treasury Company Settings", COMPANY, {"bank_settlement_voucher": "", "mode_name_template": None}
		)
		set_bank_settlement_voucher.execute()
		self.assertEqual(
			frappe.db.get_value(
				"Treasury Company Settings", COMPANY, ["bank_settlement_voucher", "mode_name_template"]
			),
			("Payment Entry", "{method} - {bank}"),
		)
		# an explicit choice is kept
		frappe.db.set_value("Treasury Company Settings", COMPANY, "bank_settlement_voucher", "Journal Entry")
		set_bank_settlement_voucher.execute()
		self.assertEqual(
			frappe.db.get_value("Treasury Company Settings", COMPANY, "bank_settlement_voucher"),
			"Journal Entry",
		)

	def test_dimension_fields_are_created_and_listed_for_uninstall(self):
		for doctype in dimensions.TREASURY_DIMENSION_DOCTYPES:
			self.assertTrue(frappe.get_meta(doctype).has_field(self.dimension_field), doctype)
		listed = set(dimensions.dimension_custom_fields())
		self.assertIn(("PDC Cheque", self.dimension_field), listed)
		self.assertIn(("Cheque Batch", self.dimension_field), listed)
		self.assertIn(("Bank Transfer", self.dimension_field), listed)
		self.assertEqual({dt for dt, _f in listed}, set(dimensions.TREASURY_DIMENSION_DOCTYPES))

	def test_bank_transfers_module(self):
		"""Phase 7b: the module, its dimension hook and the Treasury Company Settings defaults patch."""
		install.create_module_defs()
		self.assertEqual(frappe.db.get_value("Module Def", "Bank Transfers", "app_name"), "treasury")
		doctypes = frappe.get_hooks("accounting_dimension_doctypes")
		self.assertIn("Bank Transfer", doctypes)
		self.assertIn("Payment Entry", doctypes)
		self.assertIn("Bank Transfer", dimensions.TREASURY_DIMENSION_DOCTYPES)
		self.assertTrue(frappe.get_meta("Bank Transfer").has_field(self.dimension_field))
		self.assertIn(
			"treasury.bank_transfers.scheduler.post_scheduled_transfers",
			frappe.get_hooks("scheduler_events").get("daily_long", []),
		)
		self.assertIn(
			"treasury.bank_transfers.forecast.get_flows", frappe.get_hooks("treasury_forecast_sources")
		)

	def test_uninstall_keeps_series_modes_and_payees(self):
		"""`before_uninstall` deletes only custom fields: the Payment Entry series (Property Setters), the Modes
		of Payment and the payee group are left, and the series are reported. Nothing is really deleted here:
		the deletions are recorded (a real uninstall would drop the persistent test fixtures)."""
		previous = frappe.db.get_global(payment_modes.SERIES_ADDED_KEY)
		self.addCleanup(frappe.db.set_global, payment_modes.SERIES_ADDED_KEY, previous)
		frappe.db.set_global(payment_modes.SERIES_ADDED_KEY, json.dumps(["PE-TTC-KEPT-.YYYY.-"]))
		deleted = []
		output = io.StringIO()
		with (
			patch.object(install, "delete_custom_fields"),
			patch.object(install, "remove_legacy_permissions"),
			patch.object(install.frappe, "delete_doc", side_effect=lambda dt, name, **kw: deleted.append(dt)),
			redirect_stdout(output),
		):
			install.before_uninstall()

		self.assertEqual(set(deleted), {"Custom Field"})
		self.assertEqual(len(deleted), len(dimensions.dimension_custom_fields()))
		self.assertIn("PE-TTC-KEPT-.YYYY.-", output.getvalue())
		self.assertEqual(payment_modes.get_added_series(), ["PE-TTC-KEPT-.YYYY.-"])

	def test_uninstall_deletes_exactly_the_created_custom_fields(self):
		created = {
			(doctype, field["fieldname"])
			for doctype, fields in install.get_custom_fields().items()
			for field in fields
		}
		to_delete = install.custom_fields_to_delete()

		self.assertEqual(len(to_delete), len(set(to_delete)))
		self.assertEqual(set(to_delete), created)
		# only the two voucher doctypes are touched
		self.assertEqual({doctype for doctype, _fieldname in to_delete}, {"Payment Entry", "Journal Entry"})

	def test_every_custom_field_of_the_app_is_deleted_on_uninstall(self):
		modules = frappe.get_all("Module Def", filters={"app_name": "treasury"}, pluck="name")
		on_site = {
			(row.dt, row.fieldname)
			for row in frappe.get_all(
				"Custom Field", filters={"module": ["in", modules]}, fields=["dt", "fieldname"]
			)
		}
		self.assertEqual(on_site, set(install.custom_fields_to_delete()))

	def test_no_custom_docperm_for_treasury_roles(self):
		# the app ships its permissions in its own DocType JSON and grants nothing on other doctypes
		rows = frappe.get_all(
			"Custom DocPerm", filters={"role": ["in", install.TREASURY_ROLES]}, fields=["parent", "role"]
		)
		self.assertEqual(rows, [])

	def test_default_settings_are_idempotent(self):
		install.set_default_settings()
		self.assertEqual(install.set_default_settings(), [])

	def test_hooks(self):
		for hook in ("after_install", "after_migrate", "before_uninstall"):
			self.assertEqual(frappe.get_hooks(hook, app_name="treasury"), [f"treasury.install.{hook}"])

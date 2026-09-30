"""Install / uninstall: what the app adds to core doctypes is exactly what `before_uninstall` removes.

The uninstall itself is not run (it would delete the custom fields of the test site); the checks compare the
list of fields it deletes with the fields install creates and with the fields present on the site.
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.nestedset import get_root_of

from treasury import install
from treasury.accounting import dimensions, payment_modes
from treasury.patches.v1_1 import set_bank_charge_defaults, set_bank_settlement_voucher
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

	def test_bank_charges(self):
		"""Phase 7d: Bank Charge is a dimension doctype, setup() creates the default categories, and the patch
		fills the Bank Charge settings of existing companies."""
		self.assertIn("Bank Charge", frappe.get_hooks("accounting_dimension_doctypes"))
		self.assertIn("Bank Charge", dimensions.TREASURY_DIMENSION_DOCTYPES)
		self.assertTrue(frappe.get_meta("Bank Charge").has_field(self.dimension_field))
		self.assertIn(("Bank Charge", self.dimension_field), set(dimensions.dimension_custom_fields()))

		# the DDL steps are not run (they would commit the test transaction)
		with (
			patch.object(install, "create_custom_fields"),
			patch.object(install.dimensions, "create_dimension_fields"),
			patch.object(install.charge_defaults, "ensure_default_categories") as ensure_categories,
		):
			install.setup()
		ensure_categories.assert_called_once_with()

		setup_treasury_test_data()
		fields = ["bank_charge_voucher", "bank_charge_clearance"]
		previous = frappe.db.get_value("Treasury Company Settings", COMPANY, fields, as_dict=True)
		self.addCleanup(frappe.db.set_value, "Treasury Company Settings", COMPANY, dict(previous))
		frappe.db.set_value("Treasury Company Settings", COMPANY, dict.fromkeys(fields, ""))
		set_bank_charge_defaults.execute()
		self.assertEqual(
			frappe.db.get_value("Treasury Company Settings", COMPANY, fields),
			("Payment Entry", "On Posting"),
		)
		frappe.db.set_value("Treasury Company Settings", COMPANY, "bank_charge_voucher", "Journal Entry")
		set_bank_charge_defaults.execute()
		self.assertEqual(
			frappe.db.get_value("Treasury Company Settings", COMPANY, "bank_charge_voucher"), "Journal Entry"
		)

	def record_uninstall(self) -> tuple[list[tuple[str, str]], str]:
		"""Run `before_uninstall` with its deletions recorded as (doctype, name), not run (a real uninstall
		would drop the persistent test fixtures); the core custom fields and the legacy cleanup are skipped."""
		deleted = []
		output = io.StringIO()
		with (
			patch.object(install, "delete_custom_fields"),
			patch.object(install, "remove_legacy_permissions"),
			patch.object(
				install.frappe, "delete_doc", side_effect=lambda dt, name, **kw: deleted.append((dt, name))
			),
			redirect_stdout(output),
		):
			install.before_uninstall()
		return deleted, output.getvalue()

	def test_uninstall_keeps_series_modes_and_payees(self):
		"""`before_uninstall` deletes only the customisations of the app's own doctypes (spec 7h §3.6) besides
		the core custom fields: the Payment Entry series (Property Setters on Payment Entry), the Modes of
		Payment and the payee group are left, and the series are reported."""
		previous = frappe.db.get_global(payment_modes.SERIES_ADDED_KEY)
		self.addCleanup(frappe.db.set_global, payment_modes.SERIES_ADDED_KEY, previous)
		frappe.db.set_global(payment_modes.SERIES_ADDED_KEY, json.dumps(["PE-TTC-KEPT-.YYYY.-"]))
		deleted, output = self.record_uninstall()

		expected = [
			(doctype, name) for doctype, names in install.app_doctype_customisations().items() for name in names
		]
		self.assertEqual(deleted, expected)
		self.assertLessEqual(
			{dt for dt, _name in deleted}, {"Custom Field", "Property Setter", "Document Naming Rule"}
		)
		self.assertNotIn("Mode of Payment", {dt for dt, _name in deleted})
		self.assertIn("PE-TTC-KEPT-.YYYY.-", output)
		self.assertEqual(payment_modes.get_added_series(), ["PE-TTC-KEPT-.YYYY.-"])

	def test_uninstall_deletes_app_doctype_customisations(self):
		"""Spec 7h §3.6: a Property Setter on Bank Transfer.naming_series, a Document Naming Rule on PDC Cheque
		and a Custom Field on Cheque Book without a module are deleted; the Payment Entry series Property
		Setter and the Modes of Payment are not. The records are written without DDL or cache effects."""
		setup_treasury_test_data()
		setter = frappe.get_doc(
			{
				"doctype": "Property Setter",
				"name": f"_ttc_ps_{frappe.generate_hash(length=6)}",
				"doctype_or_field": "DocField",
				"doc_type": "Bank Transfer",
				"field_name": "naming_series",
				"property": "options",
				"property_type": "Text",
				"value": "BTX-.YYYY.-.#####\nBT-.YYYY.-.#####",
			}
		)
		setter.db_insert()
		pe_setter = frappe.get_doc(
			{
				"doctype": "Property Setter",
				"name": f"_ttc_pe_{frappe.generate_hash(length=6)}",
				"doctype_or_field": "DocField",
				"doc_type": "Payment Entry",
				"field_name": "naming_series",
				"property": "options",
				"property_type": "Text",
				"value": "ACC-PAY-.YYYY.-",
			}
		)
		pe_setter.db_insert()
		rule = frappe.get_doc(
			{
				"doctype": "Document Naming Rule",
				"name": f"_ttc_rule_{frappe.generate_hash(length=6)}",
				"document_type": "PDC Cheque",
				"prefix": "TTC-RULE-",
				"prefix_digits": 4,
			}
		)
		rule.db_insert()
		field = frappe.get_doc(
			{
				"doctype": "Custom Field",
				"name": f"Cheque Book-_ttc_{frappe.generate_hash(length=6)}",
				"dt": "Cheque Book",
				"fieldname": "_ttc_note",
				"fieldtype": "Data",
				"label": "Note",
			}
		)
		field.db_insert()

		deleted, _output = self.record_uninstall()
		self.assertIn(("Property Setter", setter.name), deleted)
		self.assertIn(("Document Naming Rule", rule.name), deleted)
		self.assertIn(("Custom Field", field.name), deleted)
		self.assertNotIn(("Property Setter", pe_setter.name), deleted)
		self.assertFalse([name for dt, name in deleted if dt == "Mode of Payment"])
		recorded_fields = {
			tuple(frappe.db.get_value("Custom Field", name, ["dt", "fieldname"]))
			for dt, name in deleted
			if dt == "Custom Field"
		}
		self.assertLessEqual(set(dimensions.dimension_custom_fields()), recorded_fields)
		self.assertIn("Cheque Book", install.app_doctypes())
		self.assertNotIn("Payment Entry", install.app_doctypes())

	def test_every_customisation_is_listed(self):
		"""Every Treasury customisation of a core doctype is removed by `delete_custom_fields`; the app's own
		doctypes are covered by `app_doctype_customisations`."""
		modules = frappe.get_all("Module Def", filters={"app_name": "treasury"}, pluck="name")
		app_doctypes = set(install.app_doctypes())
		to_delete = set(install.custom_fields_to_delete())
		missing = [
			(row.dt, row.fieldname)
			for row in frappe.get_all("Custom Field", fields=["dt", "fieldname", "module"])
			if row.dt not in app_doctypes
			and (row.module in modules or (row.fieldname or "").startswith("treasury_"))
			and (row.dt, row.fieldname) not in to_delete
		]
		self.assertEqual(missing, [])
		setters = frappe.get_all(
			"Property Setter", fields=["doc_type", "field_name", "property", "value", "module"]
		)
		self.assertEqual(
			[s.doc_type for s in setters if s.module in modules and s.doc_type not in app_doctypes], []
		)
		mentions = [
			(s.doc_type, s.field_name, s.property)
			for s in setters
			if s.doc_type not in app_doctypes
			and not (s.doc_type == "Payment Entry" and s.field_name == "naming_series")
			and ((s.field_name or "").startswith("treasury_") or "treasury_" in (s.value or ""))
		]
		self.assertEqual(mentions, [])

	def test_uninstall_deletes_exactly_the_created_custom_fields(self):
		created = {
			(doctype, field["fieldname"])
			for doctype, fields in install.get_custom_fields().items()
			for field in fields
		}
		to_delete = install.custom_fields_to_delete()

		self.assertEqual(len(to_delete), len(set(to_delete)))
		self.assertEqual(set(to_delete), created)
		# only the two voucher doctypes and Invoice Discounting (spec 7g §2.8) are touched
		self.assertEqual(
			{doctype for doctype, _fieldname in to_delete},
			{"Payment Entry", "Journal Entry", "Invoice Discounting"},
		)
		self.assertIn(("Invoice Discounting", "treasury_facility"), to_delete)

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

	def test_bank_position_alerts(self):
		"""Phase 7e: the hourly job, the log retention, the Treasury Settings defaults, the default recipient
		and the On Low Projected Balance patch."""
		from treasury.balance_alerts import recipients
		from treasury.patches.v1_1 import set_balance_alert_defaults
		from treasury.utils.settings import stored_single_value

		self.assertIn(
			"treasury.balance_alerts.scheduler.run_hourly",
			frappe.get_hooks("scheduler_events", app_name="treasury").get("hourly", []),
		)
		from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing

		hook = frappe.get_hooks("default_log_clearing_doctypes", app_name="treasury")
		retention = hook["Treasury Alert Log"]
		self.assertEqual(retention[-1] if isinstance(retention, list) else retention, 180)
		self.assertTrue(_supports_log_clearing("Treasury Alert Log"))

		# the defaults of the new Single fields are stored by setup (set_missing_single_defaults)
		fields = (
			"balance_alert_horizon",
			"balance_digest_hour",
			"send_balance_digest",
			"send_balance_alerts",
			"balance_alert_notification",
			"balance_alert_email",
			"digest_zero_days",
		)
		for fieldname in fields:
			frappe.db.delete("Singles", {"doctype": "Treasury Settings", "field": fieldname})
		stored = install.set_default_settings()
		self.assertLessEqual(set(fields), set(stored))
		self.assertEqual(
			[stored_single_value("Treasury Settings", f) for f in fields], ["7", "7", "1", "1", "1", "1", "1"]
		)

		# setup adds the Treasury Manager recipient once
		with (
			patch.object(install, "create_custom_fields"),
			patch.object(install.dimensions, "create_dimension_fields"),
			patch.object(install.recipients, "ensure_default_recipients") as ensure_recipients,
		):
			install.setup()
		ensure_recipients.assert_called_once_with()
		self.assertTrue(recipients.DEFAULT_RECIPIENTS_KEY)

		setup_treasury_test_data()
		previous = frappe.db.get_value("Treasury Company Settings", COMPANY, "on_low_balance")
		self.addCleanup(frappe.db.set_value, "Treasury Company Settings", COMPANY, "on_low_balance", previous)
		frappe.db.set_value("Treasury Company Settings", COMPANY, "on_low_balance", "")
		set_balance_alert_defaults.execute()
		self.assertEqual(frappe.db.get_value("Treasury Company Settings", COMPANY, "on_low_balance"), "Warn")
		self.assertIn(
			"treasury.patches.v1_1.set_balance_alert_defaults",
			(Path(frappe.get_app_path("treasury")) / "patches.txt").read_text(encoding="utf-8"),
		)

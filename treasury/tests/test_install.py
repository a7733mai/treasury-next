"""Install / uninstall: what the app adds to core doctypes is exactly what `before_uninstall` removes.

The uninstall itself is not run (it would delete the custom fields of the test site); the checks compare the
list of fields it deletes with the fields install creates and with the fields present on the site.
"""

import frappe
from frappe.tests import IntegrationTestCase

from treasury import install


class TestInstall(IntegrationTestCase):
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

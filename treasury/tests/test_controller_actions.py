"""Whitelisted controller actions must never shadow a Frappe Document attribute.

A method named like a Document/BaseDocument attribute replaces Frappe's own one on that class: e.g. an action
called `extend` broke `Document.__init__` (which fills child tables through `self.extend`) with an infinite
recursion. This test checks every controller of the Treasury app.
"""

import frappe
from frappe.model.base_document import get_controller
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase


class TestControllerActions(IntegrationTestCase):
	def test_no_action_shadows_a_document_attribute(self):
		modules = frappe.get_all("Module Def", filters={"app_name": "treasury"}, pluck="name")
		doctypes = frappe.get_all("DocType", filters={"module": ["in", modules], "custom": 0}, pluck="name")
		self.assertTrue(doctypes)

		clashes = []
		for doctype in doctypes:
			controller = get_controller(doctype)
			for name, value in vars(controller).items():
				if callable(value) and value in frappe.whitelisted and hasattr(Document, name):
					clashes.append(f"{doctype}.{name}")
		self.assertEqual(clashes, [])
		# the check sees the actions (not a vacuous pass)
		self.assertIn(vars(get_controller("Letter of Guarantee"))["extend_validity"], frappe.whitelisted)

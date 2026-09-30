"""Phase 7h: the Setup Checklist and the Naming Series overview of Treasury Company Settings (spec 7h §3.4,
§3.5, §7 D), and naming per bank through Frappe's Document Naming Rule (F4).

The site may hold company banks of other test classes' fixtures: each check looks at the rows the test itself
changes (in / not in `missing`), and at the status when nothing else is missing."""

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.tests import IntegrationTestCase

from treasury.tests.utils import (
	BANK,
	COMPANY,
	DEFAULT_PDC_SETTINGS,
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	TreasuryTestMixin,
	acc,
	bank_account_name,
	day,
	ensure_alert_bank,
	ensure_charge_category,
	ensure_company,
	ensure_mode,
	ensure_user,
	fee_rule,
	make_bank_transfer,
	make_cheque_layout,
	make_pdc_cheque,
	set_bank_suppliers,
	set_fee_rules,
	set_mode_map,
	set_pdc_accounts,
	set_thresholds,
)
from treasury.treasury_setup import checklist
from treasury.treasury_setup.checklist import ITEMS, MISSING, OK, RECOMMENDED, REQUIRED, WARNING
from treasury.utils.naming import SERIES_DOCTYPES, get_series_overview

SETTINGS = "Treasury Company Settings"
NAMING_READER = "treasury-naming-reader@example.com"
NO_DEFAULT_BANKS = {
	"default_outgoing_cheque_bank_account": None,
	"default_normal_bank_account": None,
	"default_fawran_bank_account": None,
	"default_swift_bank_account": None,
}


class TestSetupChecklist(TreasuryTestMixin, IntegrationTestCase):
	def items(self) -> dict:
		doc = frappe.get_doc(SETTINGS, COMPANY)
		return {item.key: item for item in checklist.get_checklist(doc)}

	def test_items_and_order(self):
		items = checklist.get_checklist(frappe.get_doc(SETTINGS, COMPANY))
		self.assertEqual([i.key for i in items], list(ITEMS))
		for item in items:
			with self.subTest(item=item.key):
				self.assertIn(item.level, (REQUIRED, RECOMMENDED))
				self.assertIn(item.status, (OK, MISSING, WARNING))
				self.assertTrue(item.label)
				self.assertTrue(item.action.get("route") or item.action.get("field"))
		self.assertEqual([i.key for i in items if i.level == REQUIRED], ["pdc_accounts"])

	def test_pdc_accounts(self):
		bank = ensure_alert_bank()
		frappe.db.delete(
			"PDC Account Mapping",
			{"parenttype": SETTINGS, "parent": COMPANY, "bank_account": bank.bank_account},
		)
		item = self.items()["pdc_accounts"]
		self.assertEqual(item.status, MISSING)
		self.assertIn(bank.bank_account, item.missing)
		self.assertEqual(item.action.field, "pdc_accounts")
		ensure_alert_bank(name=bank.bank_account.rsplit(" - ", 1)[0])  # the mapping row again
		item = self.items()["pdc_accounts"]
		self.assertNotIn(bank.bank_account, item.missing)
		if not item.missing:
			self.assertEqual(item.status, OK)

	def test_payment_modes_missing_and_unregistered(self):
		egp = self.data.egp
		set_pdc_accounts(self, [egp.bank_account])
		self.use_company_settings(**NO_DEFAULT_BANKS)
		mode = ensure_mode("_Test Treasury Checklist Mode", account=egp.bank_gl)
		cheque_out = {"bank_account": egp.bank_account, "method": "Cheque Out", "mode_of_payment": mode}
		set_mode_map(self, [cheque_out])
		item = self.items()["payment_modes"]
		self.assertEqual((item.status, item.missing), (MISSING, [f"{egp.bank_account}: Cheque In"]))

		series = "PE-TTC-CHKL-.YYYY.-"
		set_mode_map(
			self,
			[
				cheque_out,
				{
					"bank_account": egp.bank_account,
					"method": "Cheque In",
					"mode_of_payment": mode,
					"pe_naming_series": series,
				},
			],
		)
		item = self.items()["payment_modes"]
		self.assertEqual((item.status, item.missing), (WARNING, []))
		self.assertIn(series, item.detail)

		# a default transfer bank needs a row of its transfer method too
		self.use_company_settings(default_normal_bank_account=egp.bank_account)
		item = self.items()["payment_modes"]
		self.assertEqual(item.missing, [f"{egp.bank_account}: Bank Transfer"])

	def test_default_banks_and_fee_rules(self):
		egp = self.data.egp.bank_account
		set_pdc_accounts(self, [egp])
		self.use_company_settings(**NO_DEFAULT_BANKS)
		items = self.items()
		meta = frappe.get_meta(SETTINGS)
		self.assertEqual(items["default_banks"].status, MISSING)
		self.assertEqual(
			items["default_banks"].missing,
			[
				meta.get_label("default_outgoing_cheque_bank_account"),
				meta.get_label("default_normal_bank_account"),
			],
		)
		self.assertEqual(items["default_banks"].action.field, "default_outgoing_cheque_bank_account")
		self.assertEqual(items["default_banks"].quick_fill, egp)
		self.assertEqual(
			(items["transfer_fee_rules"].status, items["transfer_fee_rules"].detail),
			(OK, "No default transfer bank to check."),
		)

		self.use_company_settings(default_normal_bank_account=egp)
		set_fee_rules(self, [])
		items = self.items()
		self.assertEqual(
			items["default_banks"].missing, [meta.get_label("default_outgoing_cheque_bank_account")]
		)
		self.assertEqual(
			(items["transfer_fee_rules"].status, items["transfer_fee_rules"].missing),
			(MISSING, [f"{egp}: Normal"]),
		)
		set_fee_rules(self, [fee_rule(egp, "Normal", fixed_fee=5)])
		self.assertEqual(self.items()["transfer_fee_rules"].status, OK)

	def test_thresholds_layouts_categories_suppliers(self):
		egp = self.data.egp.bank_account
		# balance thresholds
		set_thresholds(self, [])
		item = self.items()["balance_thresholds"]
		self.assertEqual(item.status, MISSING)
		self.assertIn(egp, item.missing)
		banks = [b.name for b in checklist.company_banks(COMPANY)]
		set_thresholds(self, [{"bank_account": b, "warning_balance": 0, "minimum_balance": 0} for b in banks])
		self.assertEqual(self.items()["balance_thresholds"].status, OK)

		# cheque layouts (the PDC Accounts rows have no Cheque Print Format)
		set_pdc_accounts(self, [egp])
		for name in frappe.get_all(
			"Cheque Layout", filters={"bank_account": egp, "is_default": 1, "disabled": 0}, pluck="name"
		):
			frappe.db.set_value("Cheque Layout", name, "disabled", 1)
			self.addCleanup(frappe.db.set_value, "Cheque Layout", name, "disabled", 0)
		item = self.items()["cheque_layouts"]
		self.assertEqual((item.status, item.missing), (MISSING, [egp]))
		self.assertEqual(item.action.route, ["Form", "Cheque Layout", "new"])
		self.assertEqual(item.action.route_options, {"bank_account": egp})
		make_cheque_layout(egp)
		self.assertEqual(self.items()["cheque_layouts"].status, OK)

		# bank charge categories: an enabled category without an account of the company
		category = ensure_charge_category("_Test Treasury Checklist Charge", keywords=(), account=False)
		item = self.items()["charge_categories"]
		self.assertEqual(item.status, MISSING)
		self.assertIn(category, item.missing)
		doc = frappe.get_doc("Bank Charge Category", category)
		doc.append("accounts", {"company": COMPANY, "expense_account": acc("Treasury Bank Charges")})
		doc.save(ignore_permissions=True)
		self.assertNotIn(category, self.items()["charge_categories"].missing)

		# bank suppliers
		set_bank_suppliers(self, [])
		item = self.items()["bank_suppliers"]
		self.assertEqual(item.status, MISSING)
		self.assertIn(BANK, item.missing)
		self.assertEqual(item.action.field, "bank_suppliers")
		set_bank_suppliers(self, [{"bank": BANK, "supplier": SUPPLIER}])
		self.assertNotIn(BANK, self.items()["bank_suppliers"].missing)

	def test_onload_and_status(self):
		doc = frappe.get_doc(SETTINGS, COMPANY)
		doc.run_method("onload")
		onload = doc.get_onload()
		self.assertEqual([i.key for i in onload.setup_checklist], list(ITEMS))
		self.assertEqual(
			onload.setup_complete,
			all(i.status != MISSING for i in onload.setup_checklist if i.level == REQUIRED),
		)
		self.assertEqual(len(onload.naming_series), len(SERIES_DOCTYPES))

		status = checklist.get_status(COMPANY)
		items = checklist.get_checklist(frappe.get_doc(SETTINGS, COMPANY))
		self.assertEqual(status.exists, 1)
		self.assertEqual(status.missing, sum(1 for i in items if i.status == MISSING))
		required = sum(1 for i in items if i.status == MISSING and i.level == REQUIRED)
		self.assertEqual((status.required_missing, status.complete), (required, 0 if required else 1))
		self.assertEqual(status.route, ["Form", SETTINGS, COMPANY])

		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		frappe.db.delete(SETTINGS, {"name": OTHER_COMPANY})
		status = checklist.get_status(OTHER_COMPANY)
		self.assertEqual(
			(status.exists, status.complete, status.missing, status.required_missing),
			(0, 0, len(ITEMS), 1),
		)
		self.assertEqual(status.route, ["Form", SETTINGS, "new"])
		self.assertEqual(status.route_options, {"company": OTHER_COMPANY})

	def test_checklist_open_until_setup_is_complete(self):
		# Frappe collapses a collapsible section while `collapsible_depends_on` is false (layout.js): the
		# checklist stays open while setup is incomplete and folds once it is complete
		section = frappe.get_meta(SETTINGS).get_field("setup_checklist_section")
		self.assertEqual(section.collapsible, 1)
		self.assertEqual(
			section.collapsible_depends_on, "eval:!(doc.__onload && doc.__onload.setup_complete)"
		)

	def test_naming_rules_only_for_rule_readers(self):
		rule = frappe.get_doc(
			{
				"doctype": "Document Naming Rule",
				"document_type": "Bank Charge",
				"prefix": "BCH-SECRET-",
				"prefix_digits": 4,
				"conditions": [
					{"field": "bank_account", "condition": "=", "value": self.data.egp.bank_account}
				],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "Document Naming Rule", rule.name, ignore_permissions=True, force=True
		)
		row = next(r for r in get_series_overview() if r.doctype == "Bank Charge")
		self.assertIn(rule.name, [r.name for r in row.rules])

		user = ensure_user(NAMING_READER, ["Treasury User"])  # reads the settings, not the rules
		self.assertFalse(frappe.has_permission("Document Naming Rule", "read", user=user))
		with self.set_user(user):
			rows = get_series_overview()
		self.assertEqual(len(rows), len(SERIES_DOCTYPES))
		self.assertTrue(all(r.rules == [] for r in rows))
		self.assertNotIn("BCH-SECRET-", frappe.as_json(rows))

	def test_naming_default_is_the_field_default(self):
		# Bank Transfer's DocField default wins over the first option (Customize Form re-ordering)
		setter = make_property_setter(
			"Bank Transfer", "naming_series", "options", "BTX-.YYYY.-.#####\nBT-.YYYY.-.#####", "Text"
		)
		self.addCleanup(frappe.clear_cache, doctype="Bank Transfer")
		self.addCleanup(
			frappe.delete_doc, "Property Setter", setter.name, ignore_permissions=True, force=True
		)
		frappe.clear_cache(doctype="Bank Transfer")
		field_default = frappe.get_meta("Bank Transfer").get_field("naming_series").default
		self.assertEqual(field_default, "BT-.YYYY.-.#####")
		row = next(r for r in get_series_overview() if r.doctype == "Bank Transfer")
		self.assertEqual(row.default, field_default)
		# a series field without a default: the first option
		self.assertFalse(frappe.get_meta("Letter of Guarantee").get_field("naming_series").default)
		row = next(r for r in get_series_overview() if r.doctype == "Letter of Guarantee")
		self.assertEqual(row.default, row.options[0])

	def test_naming_overview(self):
		rows = {row.doctype: row for row in get_series_overview()}
		self.assertEqual(len(rows), 12)
		self.assertEqual(
			rows["PDC Cheque"].default,
			{"Outgoing": "PDC-OUT-.YYYY.-.#####", "Incoming": "PDC-IN-.YYYY.-.#####"},
		)
		pdc = rows["PDC Cheque"]
		self.assertEqual((pdc.forced_by, pdc.bank_field), ("direction", "bank_account"))
		self.assertEqual(rows["Bank Facility"].bank_field, "default_bank_account")
		self.assertIsNone(rows["Bank Transfer"].forced_by)

		# what Document Naming Settings writes: the options and the default (the first option)
		setters = [
			make_property_setter(
				"Bank Transfer", "naming_series", "options", "BTX-.YYYY.-.#####\nBT-.YYYY.-.#####", "Text"
			),
			make_property_setter("Bank Transfer", "naming_series", "default", "BTX-.YYYY.-.#####", "Text"),
		]
		for setter in setters:
			self.addCleanup(frappe.clear_cache, doctype="Bank Transfer")
			self.addCleanup(
				frappe.delete_doc, "Property Setter", setter.name, ignore_permissions=True, force=True
			)
		frappe.clear_cache(doctype="Bank Transfer")

		row = next(r for r in get_series_overview() if r.doctype == "Bank Transfer")
		self.assertEqual(row.default, "BTX-.YYYY.-.#####")
		self.assertEqual(row.options, ["BTX-.YYYY.-.#####", "BT-.YYYY.-.#####"])
		transfer = make_bank_transfer(submit=False)
		self.assertTrue(transfer.name.startswith("BTX-"), transfer.name)

	def test_document_naming_rule_per_bank(self):
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		bank = ensure_alert_bank()
		rule = frappe.get_doc(
			{
				"doctype": "Document Naming Rule",
				"document_type": "PDC Cheque",
				"prefix": "RYN-OUT-",
				"prefix_digits": 4,
				"priority": 10,
				"conditions": [
					{"field": "bank_account", "condition": "=", "value": bank.bank_account},
					{"field": "direction", "condition": "=", "value": "Outgoing"},
				],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "Document Naming Rule", rule.name, ignore_permissions=True, force=True
		)

		cheque = make_pdc_cheque(amount=500, bank_account=bank.bank_account)
		self.assertEqual(cheque.name, "RYN-OUT-0001")
		cheque.deliver(handover_date=day(0))
		cheque.reload()
		self.assertEqual(cheque.operational_status, "Delivered")
		self.assertTrue(cheque.party_payment_entry)
		self.assertEqual(frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "docstatus"), 1)

		other = make_pdc_cheque(amount=500, bank_account=bank_account_name("EGP"))
		self.assertTrue(other.name.startswith("PDC-OUT-"), other.name)
		incoming = make_pdc_cheque("Incoming", amount=500, bank_account=bank.bank_account)
		self.assertTrue(incoming.name.startswith("PDC-IN-"), incoming.name)

		row = next(r for r in get_series_overview() if r.doctype == "PDC Cheque")
		listed = next(r for r in row.rules if r.name == rule.name)
		self.assertEqual(listed.prefix, "RYN-OUT-")
		self.assertIn(f"bank_account = {bank.bank_account}", listed.conditions)
		self.assertIn("direction = Outgoing", listed.conditions)
		self.assertEqual(listed.counter, 1)

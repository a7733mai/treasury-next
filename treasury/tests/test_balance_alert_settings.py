"""Phase 7e: settings of the bank position alerts, the patch and the default recipients (spec 7e §2, §6, §7 B)."""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.balance_alerts import recipients
from treasury.patches.v1_1 import set_balance_alert_defaults
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	BalanceAlertTestMixin,
	ensure_account,
	ensure_alert_bank,
	ensure_bank_account,
	ensure_company,
	ensure_user,
	set_recipients,
)

MANAGER = "treasury-alert-settings-mgr@example.com"
DISABLED = "treasury-alert-settings-off@example.com"


class TestBalanceAlertSettings(BalanceAlertTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(DISABLED, ["Treasury User"])
		frappe.db.set_value("User", DISABLED, "enabled", 0)

	def settings(self, **values):
		doc = frappe.get_single("Treasury Settings")
		doc.update(values)
		return doc

	def test_horizon_and_hour(self):
		for horizon in (0, 8):
			with self.subTest(horizon=horizon):
				doc = self.settings(balance_alert_horizon=horizon)
				self.assertRaisesRegex(frappe.ValidationError, "between 1 and 7", doc.validate)
		self.assertRaisesRegex(
			frappe.ValidationError, "between 0 and 23", self.settings(balance_digest_hour=24).validate
		)
		self.settings(balance_alert_horizon=1, balance_digest_hour=0).validate()
		self.settings(balance_alert_horizon=7, balance_digest_hour=23).validate()

	def test_recipient_rows(self):
		doc = self.settings()
		doc.set("balance_alert_recipients", [{"recipient_type": "Role"}])
		self.assertRaisesRegex(frappe.ValidationError, "Row #1: select a", doc.validate)
		doc.set("balance_alert_recipients", [{"recipient_type": "User", "role": "Treasury User"}])
		self.assertRaisesRegex(frappe.ValidationError, "Row #1: select a", doc.validate)
		row = {"recipient_type": "Role", "role": "Treasury User"}
		doc.set("balance_alert_recipients", [row, dict(row)])
		self.assertRaisesRegex(frappe.ValidationError, "Row #2: .* is listed twice", doc.validate)
		doc.set("balance_alert_recipients", [{"recipient_type": "User", "user": DISABLED}])
		self.assertRaisesRegex(frappe.ValidationError, "is disabled", doc.validate)

		# the value of the other type is cleared
		doc.set(
			"balance_alert_recipients",
			[
				{"recipient_type": "Role", "role": "Treasury Manager", "user": MANAGER},
				{"recipient_type": "User", "user": MANAGER, "role": "Treasury Manager"},
			],
		)
		doc.validate()
		self.assertEqual(
			[(r.recipient_type, r.role, r.user) for r in doc.balance_alert_recipients],
			[("Role", "Treasury Manager", None), ("User", None, MANAGER)],
		)

	def company_settings(self, rows):
		doc = frappe.get_doc("Treasury Company Settings", COMPANY)
		doc.set("balance_thresholds", rows)
		return doc

	def test_threshold_rows(self):
		egp = ensure_alert_bank()
		usd = ensure_alert_bank("USD")
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		other_gl = ensure_account(
			"Treasury Alert Other GL", "Bank Accounts", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR
		)
		other = ensure_bank_account("Treasury Alert Other Co", other_gl, company=OTHER_COMPANY)

		doc = self.company_settings([{"bank_account": other, "warning_balance": 10, "minimum_balance": 5}])
		self.assertRaisesRegex(
			frappe.ValidationError, "must be a company bank account of", doc.validate_balance_thresholds
		)
		row = {"bank_account": egp.bank_account, "warning_balance": 10, "minimum_balance": 5}
		doc = self.company_settings([row, dict(row)])
		self.assertRaisesRegex(
			frappe.ValidationError, "already has thresholds", doc.validate_balance_thresholds
		)
		doc = self.company_settings(
			[{"bank_account": egp.bank_account, "warning_balance": 4, "minimum_balance": 5}]
		)
		self.assertRaisesRegex(
			frappe.ValidationError, "cannot be lower than Minimum Balance", doc.validate_balance_thresholds
		)

		# currency from the GL account (whatever the client sent); negative values (overdraft) and equal
		# warning / minimum (no separate warning level) are fine
		overdraft = {"warning_balance": -500, "minimum_balance": -1000, "currency": "USD"}
		doc = self.company_settings(
			[
				{"bank_account": egp.bank_account, **overdraft},
				{"bank_account": usd.bank_account, "warning_balance": 100, "minimum_balance": 100},
			]
		)
		doc.validate_balance_thresholds()
		self.assertEqual([r.currency for r in doc.balance_thresholds], ["EGP", "USD"])
		first = doc.balance_thresholds[0]
		self.assertEqual((first.warning_balance, first.minimum_balance), (-500, -1000))

	def test_on_low_balance_default_and_patch(self):
		meta = frappe.get_meta("Treasury Company Settings")
		self.assertEqual(meta.get_field("on_low_balance").default, "Warn")
		self.assertEqual(meta.get_field("on_low_balance").options.split("\n"), ["Ignore", "Warn", "Block"])
		self.use_company_settings(on_low_balance="")
		set_balance_alert_defaults.execute()
		self.assertEqual(frappe.db.get_value("Treasury Company Settings", COMPANY, "on_low_balance"), "Warn")
		frappe.db.set_value("Treasury Company Settings", COMPANY, "on_low_balance", "Block")
		set_balance_alert_defaults.execute()
		self.assertEqual(frappe.db.get_value("Treasury Company Settings", COMPANY, "on_low_balance"), "Block")

	def test_ensure_default_recipients(self):
		previous = frappe.db.get_global(recipients.DEFAULT_RECIPIENTS_KEY)
		self.addCleanup(frappe.db.set_global, recipients.DEFAULT_RECIPIENTS_KEY, previous)
		set_recipients(self, [])
		frappe.db.set_global(recipients.DEFAULT_RECIPIENTS_KEY, None)

		self.assertTrue(recipients.ensure_default_recipients())
		rows = recipients._rows()
		self.assertEqual([(r.recipient_type, r.role) for r in rows], [("Role", "Treasury Manager")])
		self.assertIn(MANAGER, recipients.get_recipients())
		# once only: a table the user empties is never refilled
		self.assertFalse(recipients.ensure_default_recipients())
		set_recipients(self, [])
		self.assertFalse(recipients.ensure_default_recipients())
		self.assertEqual(recipients._rows(), [])
		self.assertEqual(recipients.get_recipients(), [])

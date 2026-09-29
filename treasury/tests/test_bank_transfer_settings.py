"""Phase 7b: the Bank Transfer section of Treasury Company Settings — fee rules, limits, default banks — and the
migration patch (spec 7b §2.6, §3.6, §6, §7 I)."""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.patches.v1_1 import set_bank_transfer_defaults
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	acc,
	bank_account_name,
	ensure_account,
	ensure_company,
	fee_rule,
)

SETTINGS = "Treasury Company Settings"


class TestBankTransferSettings(TreasuryTestMixin, IntegrationTestCase):
	def settings(self, rules=None, limits=None, **values):
		doc = frappe.get_doc(SETTINGS, COMPANY)
		doc.set("transfer_fee_rules", rules or [])
		doc.set("transfer_limits", limits or [])
		doc.update(values)
		return doc

	def assert_invalid(self, **kwargs):
		self.assertRaises(frappe.ValidationError, self.settings(**kwargs).validate)

	def test_valid_rules_and_adjacent_ranges(self):
		b1 = bank_account_name("EGP")
		doc = self.settings(
			rules=[
				fee_rule(b1, "Fawran", amount_from=0, amount_to=1000, fixed_fee=0.5),
				fee_rule(b1, "Fawran", amount_from=1000.01, amount_to=0, fixed_fee=2),
				fee_rule(b1, "Fawran", scope="Same Bank", fixed_fee=0),
				fee_rule(b1, "Normal", charge_bearer="SHA", fixed_fee=10),  # cleared: SWIFT only
			],
			limits=[{"bank_account": b1, "method": "Fawran", "per_transaction_limit": 50000, "daily_limit": 0}],
			default_fawran_bank_account=b1,
		)
		doc.validate()
		self.assertIsNone(doc.transfer_fee_rules[3].charge_bearer)

	def test_overlapping_rules(self):
		b1 = bank_account_name("EGP")
		cases = {
			"same range": [fee_rule(b1, "Normal"), fee_rule(b1, "Normal")],
			"touching": [
				fee_rule(b1, "Normal", amount_from=0, amount_to=1000),
				fee_rule(b1, "Normal", amount_from=1000, amount_to=2000),
			],
			"open end": [
				fee_rule(b1, "Normal", amount_from=5000),
				fee_rule(b1, "Normal", amount_from=100, amount_to=6000),
			],
		}
		for case, rules in cases.items():
			with self.subTest(case=case):
				with self.assertRaisesRegex(frappe.ValidationError, "overlap"):
					self.settings(rules=rules).validate()
		# another scope, bearer or direction does not overlap
		self.settings(
			rules=[
				fee_rule(b1, "Normal"),
				fee_rule(b1, "Normal", scope="Same Bank"),
				fee_rule(b1, "Normal", direction="Incoming"),
			]
		).validate()

	def test_rule_errors(self):
		b1 = bank_account_name("EGP")
		usd = self.data.usd
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		cases = {
			"amount to below amount from": {"amount_from": 500, "amount_to": 100},
			"max below min": {"min_fee": 20, "max_fee": 10},
			"percent above 100": {"fee_percent": 120},
			"negative fixed fee": {"fixed_fee": -1},
			"correspondent fee on Normal": {"correspondent_fee": 5},
			"International on Fawran": {"method": "Fawran", "scope": "International"},
			"expense account not Expense": {"expense_account": usd.bank_gl},
			"expense account in another currency": {
				"expense_account": ensure_account("Treasury USD Charges", "Indirect Expenses", currency="USD")
			},
			"expense group account": {"expense_account": acc("Indirect Expenses")},
			"bank account of another company": {"bank_account": self.foreign_bank_account()},
		}
		for case, values in cases.items():
			with self.subTest(case=case):
				self.assert_invalid(rules=[fee_rule(b1, "Normal", **values)])

	def test_limit_and_default_bank_errors(self):
		b1 = bank_account_name("EGP")
		limit = {"bank_account": b1, "method": "Fawran", "per_transaction_limit": 0, "daily_limit": 0}
		with self.assertRaisesRegex(frappe.ValidationError, "already has a limit"):
			self.settings(limits=[limit, dict(limit)]).validate()
		self.assert_invalid(limits=[{**limit, "daily_limit": -5}])
		with self.assertRaisesRegex(frappe.ValidationError, "must be a company bank account"):
			self.settings(default_swift_bank_account=self.foreign_bank_account()).validate()

	def foreign_bank_account(self) -> str:
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		gl = ensure_account(
			"Treasury Bank EGP Co2 Set", "Bank Accounts", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR
		)
		name = frappe.db.get_value("Bank Account", {"account": gl})
		if not name:
			name = (
				frappe.get_doc(
					{
						"doctype": "Bank Account",
						"account_name": "Treasury Co2 Settings",
						"bank": frappe.db.get_value("Bank Account", bank_account_name("EGP"), "bank"),
						"account": gl,
						"company": OTHER_COMPANY,
						"is_company_account": 1,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		return name

	def test_patch_sets_the_defaults(self):
		fields = list(set_bank_transfer_defaults.DEFAULTS)
		previous = frappe.db.get_value(SETTINGS, COMPANY, fields, as_dict=True)
		self.addCleanup(frappe.db.set_value, SETTINGS, COMPANY, dict(previous))
		frappe.db.set_value(SETTINGS, COMPANY, {f: "" for f in fields})
		set_bank_transfer_defaults.execute()
		self.assertEqual(
			frappe.db.get_value(SETTINGS, COMPANY, fields),
			("Normal", "SHA", "On Bank Confirmation", "Warn"),
		)
		# an explicit choice is kept, and a second run changes nothing
		frappe.db.set_value(SETTINGS, COMPANY, "transfer_limit_action", "Block")
		set_bank_transfer_defaults.execute()
		self.assertEqual(frappe.db.get_value(SETTINGS, COMPANY, "transfer_limit_action"), "Block")

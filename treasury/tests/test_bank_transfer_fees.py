"""Phase 7b: Bank Transfer fees, scope detection, limits and the fee preview (spec 7b §3.1, §3.5, §7 A). No GL."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from treasury.bank_transfers import fees
from treasury.bank_transfers.api import get_fee_preview
from treasury.tests.utils import (
	BANK,
	BANK_SWIFT,
	COMPANY,
	OTHER_ABBR,
	OTHER_BANK,
	OTHER_COMPANY,
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	acc,
	bank_account_name,
	ensure_account,
	ensure_company,
	ensure_other_bank,
	ensure_other_company_bank,
	ensure_second_egp_bank,
	ensure_user,
	fee_rule,
	make_bank_transfer,
	make_iban,
	set_fee_rules,
	set_transfer_limits,
	standard_fee_rules,
)

NO_ACCESS_USER = "treasury-transfer-fees-noaccess@example.com"


class TestBankTransferFees(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_other_bank()
		cls.b1 = bank_account_name("EGP")
		cls.bu = bank_account_name("USD")
		ensure_user(NO_ACCESS_USER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())

	def fee(self, method, amount, scope=fees.OTHER, direction="Outgoing", bank_account=None, bearer=None):
		return fees.compute_fee(COMPANY, bank_account or self.b1, method, direction, scope, amount, bearer)

	# ------------------------------------------------------------------ compute_fee

	def test_compute_fee(self):
		cases = [
			("Fawran", 1000, fees.OTHER, "Outgoing", None, None, 0.5),
			("Fawran", 1000.01, fees.OTHER, "Outgoing", None, None, 2),
			("Fawran", 50000, fees.OTHER, "Outgoing", None, None, 2),
			("Fawran", 60000, fees.OTHER, "Outgoing", None, None, 0),
			("Fawran", 500, fees.SAME, "Outgoing", None, None, 0),
			("Normal", 5000, fees.OTHER, "Outgoing", None, None, 15),
			("Normal", 20000, fees.OTHER, "Outgoing", None, None, 30),
			("Normal", 100000, fees.OTHER, "Outgoing", None, None, 50),
			("Normal", 20000, fees.SAME, "Outgoing", None, None, 0),
			("Normal", 7000, fees.OTHER, "Incoming", None, None, 5),
			("SWIFT", 1000, fees.INTERNATIONAL, "Outgoing", self.bu, "OUR", 40),
			("SWIFT", 1000, fees.INTERNATIONAL, "Outgoing", self.bu, "SHA", 25),
			("SWIFT", 1000, fees.INTERNATIONAL, "Outgoing", self.bu, "BEN", 0),
		]
		for method, amount, scope, direction, bank, bearer, expected in cases:
			with self.subTest(method=method, amount=amount, scope=scope, direction=direction, bearer=bearer):
				result = self.fee(method, amount, scope, direction, bank, bearer)
				self.assertEqual(result.fee, expected)
				if expected:
					self.assertEqual(result.account, acc("Treasury Bank Charges"))
		self.assertEqual(self.fee("Fawran", 60000).basis, "No fee rule matched.")
		our = self.fee("SWIFT", 1000, fees.INTERNATIONAL, bank_account=self.bu, bearer="OUR")
		self.assertEqual(our.correspondent_fee, 15)
		self.assertEqual(our.rule_idx, 5)
		self.assertIn("Rule #5", our.basis)
		ben = self.fee("SWIFT", 1000, fees.INTERNATIONAL, bank_account=self.bu, bearer="BEN")
		self.assertEqual(ben.basis, "Charges borne by the beneficiary (BEN).")

	def test_rule_preference(self):
		b1, bu = self.b1, self.bu
		set_fee_rules(
			self,
			[
				fee_rule(b1, "Normal", scope="Any", fixed_fee=1),
				fee_rule(b1, "Normal", scope="Other Bank", fixed_fee=2),
				fee_rule(bu, "SWIFT", scope="International", fixed_fee=3),
				fee_rule(bu, "SWIFT", scope="International", charge_bearer="SHA", fixed_fee=4),
				fee_rule(b1, "Fawran", scope="Any", fixed_fee=5),
				fee_rule(b1, "Fawran", scope="Any", fixed_fee=6),
			],
		)
		# the exact scope wins over Any, and Any applies to another scope
		self.assertEqual(self.fee("Normal", 100, fees.OTHER).fee, 2)
		self.assertEqual(self.fee("Normal", 100, fees.SAME).fee, 1)
		# an exact bearer wins over an empty one, the empty one applies to another bearer
		self.assertEqual(self.fee("SWIFT", 100, fees.INTERNATIONAL, bank_account=self.bu, bearer="SHA").fee, 4)
		self.assertEqual(self.fee("SWIFT", 100, fees.INTERNATIONAL, bank_account=self.bu, bearer="OUR").fee, 3)
		# lowest row on a tie
		result = self.fee("Fawran", 100, fees.SAME)
		self.assertEqual((result.fee, result.rule_idx), (5, 5))

	# ------------------------------------------------------------------ scope

	def test_iban_bank_code(self):
		self.assertEqual(fees.iban_bank_code("qa58 dohb 0000 1234"), "DOHB")
		self.assertEqual(fees.iban_bank_code("QA58DOHB00001234567890ABCDEFG"), "DOHB")
		self.assertIsNone(fees.iban_bank_code("QA58DOH"))
		self.assertIsNone(fees.iban_bank_code("1258DOHB00001234"))
		self.assertIsNone(fees.iban_bank_code(None))

	def test_detect_scope(self):
		b1 = self.b1
		base = {"transfer_type": "Party Payment", "method": "Normal", "bank_account": b1}
		cases = {
			"same Bank link": ({"beneficiary_bank": BANK}, fees.SAME),
			"other Bank link": ({"beneficiary_bank": OTHER_BANK}, fees.OTHER),
			"IBAN of our bank code vs our SWIFT": ({"beneficiary_iban": make_iban(BANK_SWIFT[:4])}, fees.SAME),
			"IBAN of another bank code": ({"beneficiary_iban": make_iban("DOHB")}, fees.OTHER),
			"IBAN of another country": (
				{"beneficiary_iban": make_iban(BANK_SWIFT[:4], country="DE")},
				fees.OTHER,
			),
			"nothing known": ({}, fees.OTHER),
			"SWIFT": ({"method": "SWIFT", "beneficiary_bank": BANK}, fees.INTERNATIONAL),
		}
		for case, (values, expected) in cases.items():
			with self.subTest(case=case):
				self.assertEqual(fees.detect_scope(frappe._dict({**base, **values})), expected)

		internal = {"transfer_type": "Internal Transfer", "method": "Normal", "bank_account": b1}
		second = ensure_second_egp_bank().bank_account
		other = ensure_other_company_bank().bank_account
		self.assertEqual(fees.detect_scope(frappe._dict(internal, to_bank_account=second)), fees.SAME)
		self.assertEqual(fees.detect_scope(frappe._dict(internal, to_bank_account=other)), fees.OTHER)

	def test_transfer_scope_and_fee_on_the_document(self):
		doc = make_bank_transfer(submit=False, amount=20000, beneficiary_bank=OTHER_BANK)
		self.assertEqual((doc.scope, doc.computed_fee, doc.fee_amount), (fees.OTHER, 30, 30))
		self.assertEqual(doc.fee_account, acc("Treasury Bank Charges"))
		self.assertEqual(doc.fee_rule, "Row #3")
		self.assertEqual(doc.bank_amount, 20030)
		same = make_bank_transfer(submit=False, amount=20000, beneficiary_bank=BANK, beneficiary_iban=None)
		self.assertEqual((same.scope, same.fee_amount, same.bank_amount), (fees.SAME, 0, 20000))

	# ------------------------------------------------------------------ limits

	def test_check_limits(self):
		b1 = self.b1
		set_transfer_limits(
			self, [{"bank_account": b1, "method": "Fawran", "per_transaction_limit": 50000, "daily_limit": 100000}]
		)
		day = getdate(add_days(today(), -1))
		messages = fees.check_limits(frappe._dict(self.transient(60000, day)))
		self.assertEqual(len(messages), 1)
		self.assertIn("per-transaction", messages[0])

		for _i in range(2):
			make_bank_transfer(method="Fawran", amount=45000, execution_date=day)
		messages = fees.check_limits(frappe._dict(self.transient(20000, day)))
		self.assertEqual(len(messages), 1)
		self.assertIn("daily", messages[0])
		self.assertEqual(fees.used_on(frappe._dict(self.transient(0, day)), day), 90000)

	def test_daily_limit_counts_only_the_same_bank_method_and_day(self):
		b1 = self.b1
		set_transfer_limits(
			self, [{"bank_account": b1, "method": "Fawran", "per_transaction_limit": 0, "daily_limit": 100000}]
		)
		day = getdate(add_days(today(), -2))
		counted = make_bank_transfer(method="Fawran", amount=45000, execution_date=day)
		cancelled = make_bank_transfer(method="Fawran", amount=45000, execution_date=day)
		cancelled.cancel()
		make_bank_transfer(method="Fawran", amount=45000, execution_date=add_days(day, -1))
		make_bank_transfer(method="Normal", amount=45000, execution_date=day)
		make_bank_transfer(
			method="Fawran",
			amount=45000,
			execution_date=day,
			bank_account=bank_account_name("USD"),
			beneficiary_iban=make_iban("OTHB"),
		)
		make_bank_transfer(submit=False, method="Fawran", amount=45000, execution_date=day)  # draft
		transient = frappe._dict(self.transient(0, day))
		self.assertEqual(fees.used_on(transient, day), 45000)
		self.assertEqual(fees.check_limits(frappe._dict(self.transient(55000, day))), [])

		# a Scheduled transfer on the day counts too
		future = getdate(add_days(today(), 5))
		make_bank_transfer(method="Fawran", amount=45000, execution_date=future)
		self.assertEqual(fees.used_on(transient, future), 45000)
		self.assertTrue(counted.name)

	def transient(self, amount, date) -> dict:
		return {
			"name": None,
			"company": COMPANY,
			"bank_account": self.b1,
			"method": "Fawran",
			"amount": amount,
			"currency": "EGP",
			"execution_date": date,
		}

	# ------------------------------------------------------------------ fee preview

	def test_fee_preview(self):
		preview = get_fee_preview(
			company=COMPANY,
			bank_account=self.b1,
			method="Normal",
			transfer_type="Party Payment",
			amount=20000,
			beneficiary_bank=OTHER_BANK,
		)
		self.assertEqual(preview["scope"], fees.OTHER)
		self.assertEqual(preview["computed_fee"], 30)
		self.assertEqual(preview["fee_account"], acc("Treasury Bank Charges"))
		self.assertEqual(preview["fee_rule"], "Row #3")
		self.assertEqual(preview["limit_messages"], [])

		swift = get_fee_preview(
			company=COMPANY,
			bank_account=self.bu,
			method="SWIFT",
			transfer_type="Party Payment",
			amount=1000,
			charge_bearer="OUR",
		)
		self.assertEqual(
			(swift["scope"], swift["computed_fee"], swift["correspondent_fee"]), (fees.INTERNATIONAL, 40, 15)
		)

	def test_fee_preview_permissions_and_company(self):
		with self.set_user(NO_ACCESS_USER):
			self.assertRaises(
				frappe.PermissionError,
				get_fee_preview,
				company=COMPANY,
				bank_account=self.b1,
				method="Normal",
				transfer_type="Party Payment",
				amount=100,
			)
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		other_gl = ensure_account(
			"Treasury Bank EGP Co2", "Bank Accounts", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR
		)
		other = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": "Treasury Co2 Bank",
				"bank": BANK,
				"account": other_gl,
				"company": OTHER_COMPANY,
				"is_company_account": 1,
			}
		).insert(ignore_permissions=True)
		self.assertRaises(
			frappe.ValidationError,
			get_fee_preview,
			company=COMPANY,
			bank_account=other.name,
			method="Normal",
			transfer_type="Party Payment",
			amount=100,
		)

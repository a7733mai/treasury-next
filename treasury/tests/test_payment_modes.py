"""Phase 7a: Modes of Payment map of Treasury Company Settings — generation, validation, permissions — and
the mode given to every Payment Entry Treasury builds (spec 7a §2.2, §3.2, §7 B)."""

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase

from treasury.accounting import payment_modes
from treasury.tests.utils import (
	BANK,
	COMPANY,
	PE_SETTINGS,
	TreasuryTestMixin,
	day,
	ensure_facility_accounts,
	ensure_instrument_accounts,
	ensure_mode,
	ensure_user,
	get_pe,
	make_bank_facility,
	make_letter_of_credit,
	make_own_bank,
	make_pdc_cheque,
	set_mode_map,
)

SETTINGS = "Treasury Company Settings"
TREASURY_USER = "treasury-modes-user@example.com"


class TestPaymentModes(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_facility_accounts()
		ensure_instrument_accounts()

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)
		set_mode_map(self, [])
		# a template of this test only: the modes it generates never collide with another test's
		self.tag = frappe.generate_hash(length=5)
		self.use_company_settings(mode_name_template="{method} - {bank} " + self.tag)

	def settings(self):
		return frappe.get_doc(SETTINGS, COMPANY)

	def banks(self) -> list[str]:
		return [self.data.egp.bank_account, self.data.usd.bank_account]

	def name(self, method: str, suffix: str = "") -> str:
		return f"{method} - {BANK} {self.tag}{suffix}"

	def map_rows(self) -> list[tuple]:
		return sorted(
			(row.bank_account, row.method, row.mode_of_payment) for row in self.settings().payment_modes
		)

	# ------------------------------------------------------------------ generate

	def test_generate_creates_modes_and_map_rows(self):
		egp, usd = self.data.egp, self.data.usd
		methods = ["Cheque Out", "Cheque In"]
		result = self.settings().generate_modes_of_payment(methods=methods, bank_accounts=self.banks())

		# both banks are at the same Bank: the second account's modes get its account name appended
		usd_suffix = " - Treasury USD Current"
		expected = {
			(egp.bank_account, "Cheque Out"): self.name("Cheque Out"),
			(egp.bank_account, "Cheque In"): self.name("Cheque In"),
			(usd.bank_account, "Cheque Out"): self.name("Cheque Out", usd_suffix),
			(usd.bank_account, "Cheque In"): self.name("Cheque In", usd_suffix),
		}
		self.assertEqual(sorted(result["created"]), sorted(expected.values()))
		self.assertEqual((result["linked"], result["skipped"]), ([], []))
		self.assertEqual(self.map_rows(), sorted((*key, mode) for key, mode in expected.items()))
		for (bank_account, _method), name in expected.items():
			mode = frappe.get_doc("Mode of Payment", name)
			self.assertEqual((mode.type, mode.enabled), ("Bank", 1))
			gl = egp.bank_gl if bank_account == egp.bank_account else usd.bank_gl
			self.assertEqual([(d.company, d.default_account) for d in mode.accounts], [(COMPANY, gl)])

		# a second run creates nothing and changes no row
		again = self.settings().generate_modes_of_payment(methods=methods, bank_accounts=self.banks())
		self.assertEqual(again, {"created": [], "linked": [], "skipped": []})
		self.assertEqual(len(self.settings().payment_modes), 4)

	def test_generate_defaults_and_linking(self):
		egp = self.data.egp
		# a mode of that name without a row for the company is linked to it
		ensure_mode(self.name("Bank Transfer"))
		result = self.settings().generate_modes_of_payment(bank_accounts=[egp.bank_account])
		self.assertEqual(sorted(result["created"]), sorted([self.name("Cheque Out"), self.name("Cheque In")]))
		self.assertEqual(result["linked"], [self.name("Bank Transfer")])
		accounts = frappe.get_all(
			"Mode of Payment Account",
			filters={"parent": self.name("Bank Transfer")},
			fields=["company", "default_account"],
		)
		self.assertEqual([(d.company, d.default_account) for d in accounts], [(COMPANY, egp.bank_gl)])
		methods = {row.method for row in self.settings().payment_modes}
		self.assertEqual(methods, set(payment_modes.DEFAULT_GENERATE_METHODS))

	def test_existing_mode_with_another_account_is_skipped(self):
		egp, usd = self.data.egp, self.data.usd
		name = ensure_mode(self.name("Cheque Out"), account=usd.bank_gl)
		result = self.settings().generate_modes_of_payment(
			methods=["Cheque Out"], bank_accounts=[egp.bank_account]
		)
		self.assertEqual(result["created"], [])
		self.assertEqual(
			result["skipped"], [{"mode": name, "reason": _("uses account {0}").format(usd.bank_gl)}]
		)
		self.assertEqual(self.map_rows(), [])

	def test_generate_rejects_unknown_input(self):
		self.assertRaises(
			frappe.ValidationError, self.settings().generate_modes_of_payment, methods=["Carrier Pigeon"]
		)
		party_bank = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": f"Treasury Modes Party {self.tag}",
				"bank": BANK,
				"is_company_account": 0,
			}
		).insert(ignore_permissions=True)
		self.assertRaises(
			frappe.ValidationError, self.settings().generate_modes_of_payment, bank_accounts=[party_bank.name]
		)
		self.assertEqual(self.map_rows(), [])

	# ------------------------------------------------------------------ vouchers get the mode

	def test_party_pe_gets_mapped_mode_and_override_wins(self):
		egp = self.data.egp
		mapped = ensure_mode(f"Mapped {self.tag}", account=egp.bank_gl)
		override = ensure_mode(f"Override {self.tag}")
		row = {"bank_account": egp.bank_account, "method": "Cheque Out", "mode_of_payment": mapped}
		set_mode_map(self, [row])

		cheque = make_pdc_cheque(issue_date=day(-10), due_date=day(20))
		self.assertEqual(cheque.mode_of_payment, mapped)  # defaulted on save
		cheque.deliver(handover_date=day(-9))
		cheque.reload()
		self.assertEqual(get_pe(cheque.party_payment_entry).mode_of_payment, mapped)

		other = make_pdc_cheque(issue_date=day(-10), due_date=day(20), mode_of_payment=override)
		other.deliver(handover_date=day(-9))
		other.reload()
		self.assertEqual(get_pe(other.party_payment_entry).mode_of_payment, override)

	def test_internal_transfer_pes_get_the_mode(self):
		egp = self.data.egp
		mode = ensure_mode(f"Cheque In {self.tag}", account=egp.bank_gl)
		set_mode_map(
			self, [{"bank_account": egp.bank_account, "method": "Cheque In", "mode_of_payment": mode}]
		)
		cheque = make_pdc_cheque("Incoming", amount=3000, issue_date=day(-30), due_date=day(-20))
		cheque.receive(receipt_date=day(-29))
		cheque.deposit(deposit_date=day(-20))
		cheque.clear(clearance_date=day(-18))
		cheque.reload()
		vouchers = (cheque.party_payment_entry, cheque.under_collection_payment_entry, cheque.bank_payment_entry)
		for name in vouchers:
			pe = get_pe(name)
			self.assertEqual((pe.mode_of_payment, pe.bank_account), (mode, egp.bank_account), pe.payment_type)

	def test_lc_settlement_gets_bank_transfer_mode(self):
		bank = make_own_bank()
		facility = make_bank_facility(
			limits=[
				{
					"limit_type": "Letter of Credit",
					"limit_amount": 500_000,
					"cash_margin_percent": 20,
					"commission_rate": 0.5,
				}
			],
			bank_account=bank.bank_account,
		)
		mode = ensure_mode(f"Transfer {self.tag}", account=bank.bank_gl)
		set_mode_map(
			self, [{"bank_account": bank.bank_account, "method": "Bank Transfer", "mode_of_payment": mode}]
		)
		credit = make_letter_of_credit(facility, amount=10_000)
		credit.receive_documents(documents_date="2025-04-01")
		credit.settle(settlement_date="2025-04-10")
		credit.reload()
		pe = get_pe(credit.payment_entry)
		self.assertEqual((pe.mode_of_payment, pe.bank_account), (mode, bank.bank_account))
		self.assertEqual(pe.cost_center, frappe.get_cached_value("Company", COMPANY, "cost_center"))

	# ------------------------------------------------------------------ settings validation

	def test_settings_validation(self):
		egp, usd = self.data.egp, self.data.usd
		good = ensure_mode(f"Good {self.tag}", account=egp.bank_gl)
		foreign = ensure_mode(f"Foreign {self.tag}", account=usd.bank_gl)
		disabled = ensure_mode(f"Disabled {self.tag}", enabled=0)
		row = {"bank_account": egp.bank_account, "method": "Cheque Out", "mode_of_payment": good}

		doc = self.settings()
		doc.set("payment_modes", [row])
		doc.run_method("validate")  # valid

		cases = {
			"duplicate (bank, method)": [row, {**row, "mode_of_payment": ensure_mode(f"Free {self.tag}")}],
			"account mismatch": [{**row, "mode_of_payment": foreign}],
			"disabled mode": [{**row, "mode_of_payment": disabled}],
			"invalid series": [{**row, "pe_naming_series": "PE-TTC#"}],
		}
		for case, rows in cases.items():
			with self.subTest(case=case):
				doc = self.settings()
				doc.set("payment_modes", rows)
				self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

		doc = self.settings()
		doc.mode_name_template = "{bank} only"
		self.assertRaises(frappe.ValidationError, doc.run_method, "validate")

		# a valid series that is not a Payment Entry series yet only warns
		frappe.clear_messages()
		doc = self.settings()
		doc.set("payment_modes", [{**row, "pe_naming_series": f" PE-{self.tag.upper()}-.YYYY.- "}])
		doc.run_method("validate")
		self.assertEqual(doc.payment_modes[0].pe_naming_series, f"PE-{self.tag.upper()}-.YYYY.-")
		self.assertEqual(payment_modes.unregistered_series(doc), [f"PE-{self.tag.upper()}-.YYYY.-"])
		messages = " ".join(
			str(m.get("message") if isinstance(m, dict) else m) for m in frappe.local.message_log
		)
		self.assertIn(f"PE-{self.tag.upper()}-.YYYY.-", messages)

	# ------------------------------------------------------------------ permissions

	def test_permissions(self):
		user = ensure_user(TREASURY_USER, ["Treasury User"])
		doc = self.settings()
		frappe.set_user(user)
		try:
			self.assertRaises(frappe.PermissionError, doc.generate_modes_of_payment)
			self.assertRaises(frappe.PermissionError, doc.add_series_to_payment_entry, confirm=1)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.map_rows(), [])

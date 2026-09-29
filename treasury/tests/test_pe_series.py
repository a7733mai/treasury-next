"""Phase 7a: Payment Entry naming series of the Modes of Payment map — posting on an unregistered series is
refused, "Add Series to Payment Entry" registers them through Document Naming Settings (spec 7a §3.2, §7 C).

Registering a series writes Property Setters on Payment Entry (no DDL): they are rolled back with the class,
and the cached Payment Entry meta is cleared so that no later test class sees the series.
"""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.accounting import payment_modes
from treasury.tests.utils import (
	COMPANY,
	PE_SETTINGS,
	TreasuryTestMixin,
	day,
	ensure_mode,
	ensure_user,
	get_pe,
	make_pdc_cheque,
	set_mode_map,
)

SETTINGS = "Treasury Company Settings"
MANAGER = "treasury-series-manager@example.com"


def pe_series() -> list[str]:
	return frappe.get_meta("Payment Entry").get_naming_series_options()


class TestPESeries(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.addClassCleanup(frappe.clear_cache, doctype="Payment Entry")

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)
		self.tag = frappe.generate_hash(length=4).upper()
		self.prefix = f"PE-TTC-{self.tag}-"
		self.series = f"{self.prefix}.YYYY.-"

	def map_series(self, method: str = "Cheque Out", series: str | None = None):
		egp = self.data.egp
		mode = ensure_mode(f"Series Mode {method} {self.tag}", account=egp.bank_gl)
		set_mode_map(
			self,
			[
				{
					"bank_account": egp.bank_account,
					"method": method,
					"mode_of_payment": mode,
					"pe_naming_series": series or self.series,
				}
			],
		)

	def settings(self):
		return frappe.get_doc(SETTINGS, COMPANY)

	def test_unregistered_series_blocks_posting(self):
		self.map_series()
		self.assertNotIn(self.series, pe_series())
		self.assertEqual(payment_modes.unregistered_series(self.settings()), [self.series])
		cheque = make_pdc_cheque(issue_date=day(-10), due_date=day(20))
		with self.assertRaisesRegex(frappe.ValidationError, "is not a Payment Entry series"):
			cheque.deliver(handover_date=day(-9))
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Created", "Not Posted"))

	def test_add_series_to_payment_entry(self):
		self.map_series()
		before = pe_series()
		added = self.settings().add_series_to_payment_entry(confirm=1)
		self.assertEqual(added, [self.series])

		after = pe_series()
		self.assertIn(self.series, after)
		self.assertEqual(after[0], before[0])  # the default series stays first
		self.assertEqual([s for s in after if s and s != self.series], [s for s in before if s])
		self.assertIn(self.series, payment_modes.get_added_series())
		self.assertEqual(payment_modes.unregistered_series(self.settings()), [])

		cheque = make_pdc_cheque(issue_date=day(-10), due_date=day(20))
		cheque.deliver(handover_date=day(-9))
		cheque.reload()
		self.assertTrue(cheque.party_payment_entry.startswith(self.prefix), cheque.party_payment_entry)
		self.assertEqual(get_pe(cheque.party_payment_entry).naming_series, self.series)

		# nothing left to add
		self.assertEqual(self.settings().add_series_to_payment_entry(confirm=1), [])

	def test_add_series_needs_confirmation_and_system_manager(self):
		self.map_series()
		self.assertRaises(frappe.ValidationError, self.settings().add_series_to_payment_entry)
		self.assertRaises(frappe.ValidationError, self.settings().add_series_to_payment_entry, confirm=0)

		user = ensure_user(MANAGER, ["Treasury Manager"])
		doc = self.settings()
		frappe.set_user(user)
		try:
			self.assertRaises(frappe.PermissionError, doc.add_series_to_payment_entry, confirm=1)
		finally:
			frappe.set_user("Administrator")
		self.assertNotIn(self.series, pe_series())

	def test_add_series_uses_the_saved_settings(self):
		# a map row only in the document the client sent (unsaved, unvalidated) registers nothing
		egp = self.data.egp
		doc = self.settings()
		doc.append(
			"payment_modes",
			{
				"bank_account": egp.bank_account,
				"method": "Cheque Out",
				"mode_of_payment": ensure_mode(f"Series Mode Cheque Out {self.tag}", account=egp.bank_gl),
				"pe_naming_series": self.series,
			},
		)
		self.assertNotIn(self.series, doc.add_series_to_payment_entry(confirm=1))
		self.assertNotIn(self.series, pe_series())

	def test_series_of_another_doctype_is_refused(self):
		# (a series ending with "#" would hit a KeyError in DocumentNamingSettings.check_duplicate)
		options = frappe.get_meta("Journal Entry").get_naming_series_options()
		taken = next((s for s in options if s and not s.endswith("#")), None)
		if not taken:
			self.skipTest("Journal Entry has no plain naming series on this site")
		self.map_series(series=taken)
		before = pe_series()
		self.assertRaises(frappe.ValidationError, self.settings().add_series_to_payment_entry, confirm=1)
		self.assertEqual(pe_series(), before)

	def test_reverse_pe_uses_the_original_series(self):
		self.map_series(method="Cheque In")
		self.settings().add_series_to_payment_entry(confirm=1)
		cheque = make_pdc_cheque("Incoming", amount=4000, issue_date=day(-30), due_date=day(-20))
		cheque.receive(receipt_date=day(-29))
		cheque.deposit(deposit_date=day(-20))
		cheque.bounce(bounce_date=day(-15), reason="Account closed")
		cheque.reload()

		for name in (cheque.party_payment_entry, cheque.under_collection_payment_entry):
			self.assertEqual(get_pe(name).naming_series, self.series)
		reverse = get_pe(cheque.reversal_payment_entry)
		self.assertEqual(reverse.naming_series, self.series)
		self.assertTrue(reverse.name.startswith(self.prefix), reverse.name)

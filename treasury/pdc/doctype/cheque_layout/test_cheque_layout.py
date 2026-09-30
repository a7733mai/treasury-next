# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from treasury.pdc.cheque_printing import get_layout
from treasury.tests.utils import (
	COMPANY,
	SUPPLIER,
	TreasuryTestMixin,
	ensure_alert_bank,
	ensure_party_bank_account,
	make_cheque_layout,
)

IGNORE_TEST_RECORD_DEPENDENCIES = ["Company", "Account", "Bank Account", "Print Format"]


class TestChequeLayout(TreasuryTestMixin, IntegrationTestCase):
	def assert_error(self, message: str, **values):
		with self.assertRaises(frappe.ValidationError) as ctx:
			make_cheque_layout(**values)
		self.assertIn(message, str(ctx.exception))

	def test_validations(self):
		outside = "is outside the cheque leaf"
		self.assert_error(outside, date_left_mm=150, date_width_mm=40)  # 190 mm on a 175 mm leaf
		self.assert_error(outside, payee_top_mm=-1)
		self.assert_error(outside, figures_width_mm=0)
		self.assert_error(outside, words2_top_mm=81)
		self.assert_error(outside, signature_top_mm=70, signature_height_mm=15)
		self.assert_error("must be 50–300 mm wide", leaf_width_mm=40)
		self.assert_error("must be 50–300 mm wide", leaf_height_mm=160)
		self.assert_error("Font sizes must be between 5 and 30", date_font_size_pt=3)
		self.assert_error("Font sizes must be between 5 and 30", base_font_size_pt=40)
		self.assert_error("between -90 and 90", crossing_rotation_deg=120)
		self.assert_error("Line 1 Max Characters", words1_max_chars=5)
		self.assert_error("does not fit on an A4 sheet", paper="A4 Carrier", offset_left_mm=50)
		self.assert_error("does not fit on an A4 sheet", paper="A4 Carrier", offset_top_mm=250)
		party_account = ensure_party_bank_account("Supplier", SUPPLIER)
		self.assert_error("must be a company bank account", bank_account=party_account)

		# the defaults are a valid layout; a box exactly on the edge is inside
		layout = make_cheque_layout(bank_account=ensure_alert_bank().bank_account, date_left_mm=135)
		self.assertEqual(layout.date_left_mm + layout.date_width_mm, layout.leaf_width_mm)

	def test_single_default(self):
		bank = ensure_alert_bank().bank_account
		first = make_cheque_layout(bank)
		second = make_cheque_layout(bank)
		first.reload()
		self.assertEqual((first.is_default, second.is_default), (0, 1))
		self.assertEqual(get_layout(bank).name, second.name)

		second.disabled = 1
		second.save()
		second.reload()
		self.assertEqual(second.is_default, 0)  # a disabled layout is never the default
		self.assertEqual(get_layout(bank).name, first.name)  # the newest enabled one

		first.disabled = 1
		first.save()
		self.assertIsNone(get_layout(bank))

	def test_company_fetched(self):
		bank = ensure_alert_bank().bank_account
		layout = make_cheque_layout(bank)
		self.assertEqual(layout.company, COMPANY)
		# read only in the form only: a client value is replaced by the bank account's company
		layout.company = None
		layout.save()
		self.assertEqual(frappe.db.get_value("Cheque Layout", layout.name, "company"), COMPANY)
		self.assertTrue(layout.name.startswith("CHL-"))

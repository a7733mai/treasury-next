# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class TreasurySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.treasury_setup.doctype.treasury_bank_supplier.treasury_bank_supplier import (
			TreasuryBankSupplier,
		)

		allow_future_clearance_date: DF.Check
		bank_suppliers: DF.Table[TreasuryBankSupplier]
		default_alert_days: DF.Int
		facility_alert_days: DF.Int
		payee_supplier_group: DF.Link
		security_alert_days: DF.Int
	# end: auto-generated types

	def validate(self):
		self.validate_bank_suppliers()

	def validate_bank_suppliers(self):
		"""One Supplier per Bank, and an enabled one (spec 7d §2.4)."""
		seen = set()
		for row in self.get("bank_suppliers"):
			if row.bank in seen:
				frappe.throw(
					_("Row #{0}: Bank {1} already has a Supplier.").format(row.idx, frappe.bold(row.bank))
				)
			seen.add(row.bank)
			if row.supplier and frappe.db.get_value("Supplier", row.supplier, "disabled"):
				frappe.throw(
					_("Row #{0}: Supplier {1} is disabled.").format(row.idx, frappe.bold(row.supplier))
				)

# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from treasury.balance_alerts.positions import MAX_HORIZON


class TreasurySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.treasury_setup.doctype.treasury_alert_recipient.treasury_alert_recipient import (
			TreasuryAlertRecipient,
		)
		from treasury.treasury_setup.doctype.treasury_bank_supplier.treasury_bank_supplier import (
			TreasuryBankSupplier,
		)

		allow_future_clearance_date: DF.Check
		balance_alert_email: DF.Check
		balance_alert_horizon: DF.Int
		balance_alert_notification: DF.Check
		balance_alert_recipients: DF.Table[TreasuryAlertRecipient]
		balance_digest_hour: DF.Int
		bank_suppliers: DF.Table[TreasuryBankSupplier]
		bank_suppliers_hint: DF.HTML | None
		default_alert_days: DF.Int
		digest_zero_days: DF.Check
		facility_alert_days: DF.Int
		payee_supplier_group: DF.Link
		security_alert_days: DF.Int
		send_balance_alerts: DF.Check
		send_balance_digest: DF.Check
	# end: auto-generated types

	def validate(self):
		self.validate_bank_suppliers()
		self.validate_balance_alerts()

	def validate_balance_alerts(self):
		"""Horizon 1–7 days, digest hour 0–23, one row per role / user, enabled users only (spec 7e §2.1)."""
		if not 1 <= cint(self.balance_alert_horizon) <= MAX_HORIZON:
			frappe.throw(_("Horizon must be between 1 and 7 days."))
		if not 0 <= cint(self.balance_digest_hour) <= 23:
			frappe.throw(_("Digest Hour must be between 0 and 23."))
		seen = set()
		for row in self.get("balance_alert_recipients"):
			fieldname = "user" if row.recipient_type == "User" else "role"
			other = "role" if fieldname == "user" else "user"
			value = row.get(fieldname)
			if not value:
				label = _(row.meta.get_label(fieldname), context=row.doctype)
				frappe.throw(_("Row #{0}: select a {1}.").format(row.idx, label))
			row.set(other, None)
			if fieldname == "user" and not frappe.db.get_value("User", value, "enabled"):
				frappe.throw(_("Row #{0}: User {1} is disabled.").format(row.idx, frappe.bold(value)))
			if (fieldname, value) in seen:
				frappe.throw(_("Row #{0}: {1} is listed twice.").format(row.idx, frappe.bold(value)))
			seen.add((fieldname, value))

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

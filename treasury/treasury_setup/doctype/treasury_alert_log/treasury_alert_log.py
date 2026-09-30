# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now

OVERRIDE = "Override"


class TreasuryAlertLog(Document):
	"""Written by treasury.balance_alerts only (digests, balance alerts, low balance overrides): the
	idempotency keys of the alerts and the audit trail of the overrides (spec 7e §2.5, §4)."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		alert_date: DF.Date
		alert_type: DF.Literal["Digest", "Balance Alert", "Override"]
		balance: DF.Currency
		bank_account: DF.Link | None
		company: DF.Link
		currency: DF.Link | None
		emailed: DF.Check
		is_escalation: DF.Check
		level: DF.Literal["", "Warning", "Critical"]
		message: DF.Text | None
		notified: DF.Check
		projected_date: DF.Date | None
		recipients: DF.SmallText | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		subject: DF.Data | None
		threshold: DF.Currency
		user: DF.Link | None
	# end: auto-generated types

	def on_trash(self):
		# the overrides are the audit trail of the low balance blocks: kept permanently
		if self.alert_type == OVERRIDE:
			frappe.throw(
				_("Low balance override logs are kept permanently and cannot be deleted."),
				frappe.PermissionError,
			)

	@staticmethod
	def clear_old_logs(days: int = 180):
		"""Log Settings (hooks.py `default_log_clearing_doctypes`): delete the digest and alert logs older than
		`days`. The Override logs (audit trail) are never deleted."""
		table = frappe.qb.DocType("Treasury Alert Log")
		frappe.db.delete(
			table,
			filters=((table.creation < (Now() - Interval(days=days))) & (table.alert_type != OVERRIDE)),
		)


def on_doctype_update():
	frappe.db.add_index("Treasury Alert Log", ["alert_type", "company", "alert_date"])
	frappe.db.add_index("Treasury Alert Log", ["bank_account", "projected_date"])

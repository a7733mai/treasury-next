# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TreasurySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allow_future_clearance_date: DF.Check
		default_alert_days: DF.Int
		facility_alert_days: DF.Int
		payee_supplier_group: DF.Link
		security_alert_days: DF.Int
	# end: auto-generated types

	pass

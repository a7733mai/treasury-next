# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PDCStatusLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		datetime: DF.Datetime | None
		event: DF.Literal[
			"Created",
			"Delivered",
			"Received",
			"Party Posted",
			"Deposited",
			"Under Collection Posted",
			"Cleared",
			"Bank Posted",
			"Bank Charges Posted",
			"Postponed",
			"Bounced",
			"Bounce Charges Posted",
			"Voucher Cancelled",
			"Voucher Reversed",
			"Reversed",
			"Re-presented",
			"Replaced",
			"Cancelled",
			"Returned",
		]
		from_accounting: DF.Data | None
		from_operational: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		remarks: DF.SmallText | None
		to_accounting: DF.Data | None
		to_operational: DF.Data | None
		user: DF.Link | None
		voucher_no: DF.DynamicLink | None
		voucher_type: DF.Link | None
	# end: auto-generated types

	pass

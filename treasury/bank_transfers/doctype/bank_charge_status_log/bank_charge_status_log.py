# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BankChargeStatusLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		datetime: DF.Datetime | None
		event: DF.Data | None
		event_date: DF.Date | None
		from_accounting: DF.Data | None
		from_status: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		remarks: DF.SmallText | None
		to_accounting: DF.Data | None
		to_status: DF.Data | None
		user: DF.Link | None
		voucher_no: DF.DynamicLink | None
		voucher_type: DF.Link | None
	# end: auto-generated types

	pass

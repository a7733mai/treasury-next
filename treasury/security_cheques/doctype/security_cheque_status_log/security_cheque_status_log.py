# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SecurityChequeStatusLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		datetime: DF.Datetime | None
		event: DF.Literal[
			"Created",
			"Received",
			"Stored",
			"Custody Transferred",
			"Issued",
			"Returned",
			"Lost",
			"Cancelled",
			"Encashed",
		]
		event_date: DF.Date | None
		from_status: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		pdc_cheque: DF.Link | None
		remarks: DF.SmallText | None
		to_status: DF.Data | None
		user: DF.Link | None
	# end: auto-generated types

	pass

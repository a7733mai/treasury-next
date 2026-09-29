# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SecurityChequeCustodyLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		datetime: DF.Datetime | None
		event_date: DF.Date | None
		from_custodian: DF.Link | None
		from_location: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		remarks: DF.SmallText | None
		to_custodian: DF.Link | None
		to_location: DF.Link | None
		user: DF.Link | None
	# end: auto-generated types

	pass

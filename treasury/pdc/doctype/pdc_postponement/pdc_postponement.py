# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PDCPostponement(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		approved_by: DF.Link | None
		contact_person: DF.Data | None
		datetime: DF.Datetime | None
		new_due_date: DF.Date | None
		old_due_date: DF.Date | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		reason: DF.SmallText | None
		user: DF.Link | None
	# end: auto-generated types

	pass

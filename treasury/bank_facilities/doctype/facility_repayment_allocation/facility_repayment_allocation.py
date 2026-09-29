# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FacilityRepaymentAllocation(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		due_date: DF.Date | None
		installment_no: DF.Int
		interest: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		principal: DF.Currency
		schedule_row: DF.Data | None
	# end: auto-generated types

	pass

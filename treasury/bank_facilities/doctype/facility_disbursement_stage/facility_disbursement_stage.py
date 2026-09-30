# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FacilityDisbursementStage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		description: DF.Data | None
		disbursed_on: DF.Date | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		stage_date: DF.Date
		status: DF.Literal["Pending", "Disbursed"]
		voucher_no: DF.DynamicLink | None
		voucher_type: DF.Link | None
	# end: auto-generated types

	pass

# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ChequeBatchItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allocated_amount: DF.Currency
		amount: DF.Currency
		cheque_no: DF.Data | None
		drawer_bank: DF.Data | None
		due_date: DF.Date
		facility_schedule_row: DF.Data | None
		installment_no: DF.Int
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		pdc_cheque: DF.Link | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		remarks: DF.SmallText | None
	# end: auto-generated types

	pass

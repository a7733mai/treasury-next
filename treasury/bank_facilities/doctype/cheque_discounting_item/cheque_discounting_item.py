# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ChequeDiscountingItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		cheque_no: DF.Data | None
		closed_date: DF.Date | None
		closing_journal: DF.Link | None
		days: DF.Int
		discount_charges: DF.Currency
		due_date: DF.Date | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		party: DF.DynamicLink | None
		party_name: DF.Data | None
		party_type: DF.Link | None
		pdc_cheque: DF.Link
		status: DF.Literal["Discounted", "Settled", "Bounced"]
	# end: auto-generated types

	pass

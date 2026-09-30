# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PDCClearingMatch(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		bank_charge: DF.Link | None
		bank_charge_category: DF.Link | None
		bank_transaction: DF.Link | None
		bank_transfer: DF.Link | None
		cheque: DF.Link | None
		cheque_no: DF.Data | None
		currency: DF.Link | None
		description: DF.SmallText | None
		direction: DF.Data | None
		due_date: DF.Date | None
		facility_drawdown: DF.Link | None
		facility_repayment: DF.Link | None
		installment_no: DF.Int
		match_quality: DF.Literal["Strong", "Weak", "Keyword"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		party: DF.Data | None
		reference_doctype: DF.Literal[
			"PDC Cheque", "Bank Transfer", "Bank Charge", "Facility Repayment", "Facility Drawdown"
		]
		result: DF.SmallText | None
		selected: DF.Check
		transaction_date: DF.Date | None
	# end: auto-generated types

	pass

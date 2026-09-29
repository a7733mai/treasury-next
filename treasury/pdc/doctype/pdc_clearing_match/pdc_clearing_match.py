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
		bank_transaction: DF.Link | None
		bank_transfer: DF.Link | None
		cheque: DF.Link | None
		cheque_no: DF.Data | None
		currency: DF.Link | None
		direction: DF.Data | None
		due_date: DF.Date | None
		match_quality: DF.Literal["Strong", "Weak"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		party: DF.Data | None
		reference_doctype: DF.Literal["PDC Cheque", "Bank Transfer"]
		result: DF.SmallText | None
		selected: DF.Check
		transaction_date: DF.Date | None
	# end: auto-generated types

	pass

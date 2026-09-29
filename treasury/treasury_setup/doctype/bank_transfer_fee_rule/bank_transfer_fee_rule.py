# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BankTransferFeeRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount_from: DF.Currency
		amount_to: DF.Currency
		bank_account: DF.Link
		charge_bearer: DF.Literal["", "OUR", "SHA", "BEN"]
		correspondent_fee: DF.Currency
		direction: DF.Literal["Outgoing", "Incoming"]
		expense_account: DF.Link
		fee_percent: DF.Percent
		fixed_fee: DF.Currency
		max_fee: DF.Currency
		method: DF.Literal["Normal", "Fawran", "SWIFT"]
		min_fee: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		scope: DF.Literal["Any", "Same Bank", "Other Bank", "International"]
	# end: auto-generated types

	pass

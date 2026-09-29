# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TreasuryPaymentModeMap(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bank_account: DF.Link
		method: DF.Literal[
			"Cheque Out",
			"Cheque In",
			"Bank Transfer",
			"Fawran",
			"SWIFT",
			"Internal Transfer",
			"Auto Debit",
			"Bank Charge",
		]
		mode_of_payment: DF.Link
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		pe_naming_series: DF.Data | None
	# end: auto-generated types

	pass

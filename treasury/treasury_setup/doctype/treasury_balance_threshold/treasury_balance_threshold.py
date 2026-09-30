# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TreasuryBalanceThreshold(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bank_account: DF.Link
		currency: DF.Link | None
		minimum_balance: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		warning_balance: DF.Currency
	# end: auto-generated types

	pass

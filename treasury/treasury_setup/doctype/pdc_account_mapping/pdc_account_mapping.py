# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PDCAccountMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bank_account: DF.Link
		bank_charges_account: DF.Link | None
		cheque_print_format: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		pdc_payable_account: DF.Link
		pdc_receivable_account: DF.Link
		under_collection_account: DF.Link | None
	# end: auto-generated types

	pass

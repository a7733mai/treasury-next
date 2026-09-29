# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class FacilityRepaymentSchedule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		due_date: DF.Date
		installment_no: DF.Int
		interest: DF.Currency
		is_prepayment: DF.Check
		outstanding_after: DF.Currency
		paid_interest: DF.Currency
		paid_principal: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		payment_journal: DF.Link | None
		principal: DF.Currency
		repayment: DF.Link | None
		status: DF.Literal["Pending", "Partially Paid", "Paid", "Overdue"]
		total: DF.Currency
	# end: auto-generated types

	pass

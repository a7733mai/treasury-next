# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BankFacilityLimit(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		available: DF.Currency
		cash_margin_percent: DF.Percent
		commission_rate: DF.Percent
		day_count: DF.Literal["Actual/365", "Actual/360"]
		default_tenor_months: DF.Int
		interest_rate: DF.Percent
		liability_account: DF.Link | None
		limit_amount: DF.Currency
		limit_type: DF.Literal[
			"Overdraft",
			"Short Term Loan",
			"Long Term Loan",
			"Letter of Guarantee",
			"Letter of Credit",
			"Trust Receipt",
			"Cheque Discounting",
			"Invoice Discounting",
			"Murabaha",
			"Tawarruq",
			"Ijarah",
			"Istisna'a",
			"Other",
		]
		margin_rate: DF.Percent
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		rate_type: DF.Literal["Fixed", "Variable (reference + margin)"]
		reference_rate_label: DF.Data | None
		utilised: DF.Currency
	# end: auto-generated types

	pass

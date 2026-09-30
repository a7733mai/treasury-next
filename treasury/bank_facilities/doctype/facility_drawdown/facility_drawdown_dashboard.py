from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Facility Drawdown",
		"drawdown",
		[
			("Facility", ["Bank Facility", "Facility Repayment", "Letter of Credit"]),
			# installment cheques to the lender (spec 7g §3.4)
			("Cheques", ["PDC Cheque", "Cheque Batch"]),
		],
		internal_links={"Bank Facility": "facility", "Letter of Credit": "letter_of_credit"},
		non_standard_fieldnames={"PDC Cheque": "facility_drawdown", "Cheque Batch": "facility_drawdown"},
		vouchers=True,
	)

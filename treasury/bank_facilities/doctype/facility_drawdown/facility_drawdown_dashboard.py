from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Facility Drawdown",
		"drawdown",
		[
			("Facility", ["Bank Facility"]),
			("Repayments", ["Facility Repayment"]),
			# installment cheques to the lender (spec 7g §3.4)
			("Cheques", ["PDC Cheque", "Cheque Batch"]),
			# the Letter of Credit whose settlement created this Trust Receipt
			("Letters of Credit", ["Letter of Credit"]),
		],
		internal_links={"Bank Facility": "facility", "Letter of Credit": "letter_of_credit"},
		non_standard_fieldnames={"PDC Cheque": "facility_drawdown", "Cheque Batch": "facility_drawdown"},
		vouchers=True,
	)

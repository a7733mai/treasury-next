from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Bank Facility",
		"facility",
		[
			("Drawdowns", ["Facility Drawdown", "Facility Repayment"]),
			("Instruments", ["Letter of Credit", "Letter of Guarantee", "Cheque Discounting"]),
			("Security", ["Security Cheque"]),
		],
		internal_links={"Security Cheque": ["security_cheques", "security_cheque"]},
	)

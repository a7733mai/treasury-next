from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Facility Drawdown",
		"drawdown",
		[("Facility", ["Bank Facility", "Facility Repayment"])],
		internal_links={"Bank Facility": "facility"},
		vouchers=True,
	)

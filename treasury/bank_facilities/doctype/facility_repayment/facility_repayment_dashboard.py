from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Facility Repayment",
		"facility_repayment",
		[("Facility", ["Facility Drawdown", "Bank Facility"])],
		internal_links={"Facility Drawdown": "drawdown", "Bank Facility": "facility"},
		vouchers=True,
	)

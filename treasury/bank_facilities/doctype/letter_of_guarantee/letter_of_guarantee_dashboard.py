from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Letter of Guarantee",
		"letter_of_guarantee",
		[("Facility", ["Bank Facility", "Bank Guarantee"])],
		internal_links={"Bank Facility": "facility", "Bank Guarantee": "bank_guarantee"},
		vouchers=True,
	)

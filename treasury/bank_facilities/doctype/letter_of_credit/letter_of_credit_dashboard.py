from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Letter of Credit",
		"letter_of_credit",
		[("Facility", ["Bank Facility", "Supplier"])],
		internal_links={"Bank Facility": "facility", "Supplier": "supplier"},
		vouchers=True,
	)

from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Bank Charge",
		"bank_charge",
		[("Refunds", ["Bank Charge"])],
		non_standard_fieldnames={"Bank Charge": "against_bank_charge"},
		vouchers=True,
	)

from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Letter of Credit",
		"letter_of_credit",
		[("Facility", ["Bank Facility", "Supplier", "Facility Drawdown"])],
		internal_links={
			"Bank Facility": "facility",
			"Supplier": "supplier",
			# the Trust Receipt financing the settlement (spec 7g §3.9)
			"Facility Drawdown": "trust_receipt_drawdown",
		},
		vouchers=True,
	)

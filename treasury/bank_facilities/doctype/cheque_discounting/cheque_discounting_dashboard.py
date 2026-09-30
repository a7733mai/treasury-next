from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Cheque Discounting",
		"cheque_discounting",
		[("Facility", ["Bank Facility"]), ("Treasury", ["PDC Cheque"])],
		internal_links={"Bank Facility": "facility", "PDC Cheque": ["items", "pdc_cheque"]},
		vouchers=True,
	)

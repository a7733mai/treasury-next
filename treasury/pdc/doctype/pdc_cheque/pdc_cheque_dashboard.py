from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"PDC Cheque",
		"pdc_cheque",
		[("Treasury", ["Cheque Batch", "Security Cheque", "Cheque Discounting"])],
		internal_links={"Cheque Batch": "cheque_batch", "Security Cheque": "security_cheque"},
		vouchers=True,
	)

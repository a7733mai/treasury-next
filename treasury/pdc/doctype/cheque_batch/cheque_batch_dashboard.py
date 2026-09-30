from treasury.utils.connections import dashboard


def get_data():
	return dashboard("Cheque Batch", "cheque_batch", [("Treasury", ["PDC Cheque"])])

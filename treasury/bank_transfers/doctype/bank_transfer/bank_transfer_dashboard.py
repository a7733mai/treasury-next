from treasury.utils.connections import dashboard


def get_data():
	return dashboard("Bank Transfer", "bank_transfer", [], vouchers=True)

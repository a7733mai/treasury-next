from treasury.utils.connections import dashboard


def get_data():
	# PDC Cheque.security_cheque (its encashment); Bank Facility Security Cheque.security_cheque (collateral)
	return dashboard("Security Cheque", "security_cheque", [("Treasury", ["PDC Cheque", "Bank Facility"])])

from treasury.utils.connections import dashboard


def get_data():
	return dashboard(
		"Bank Facility",
		"facility",
		[
			("Drawdowns", ["Facility Drawdown", "Facility Repayment"]),
			(
				"Instruments",
				["Letter of Credit", "Letter of Guarantee", "Cheque Discounting", "Invoice Discounting"],
			),
			("Security", ["Security Cheque"]),
		],
		internal_links={"Security Cheque": ["security_cheques", "security_cheque"]},
		# ERPNext's Invoice Discounting, through the Treasury custom field (spec 7g §2.8, design §9.3)
		non_standard_fieldnames={"Invoice Discounting": "treasury_facility"},
	)

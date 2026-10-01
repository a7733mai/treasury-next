"""Where bank charges are booked (spec 7d §3.7): the Treasury documents and posting events whose vouchers carry
a bank charge, for the Bank Charges Analysis report. The label is also the report's Source option; adding a
source is one entry here.

Cheque discounting charges and facility interest / penalties are financing costs (Cost of Financing report),
not bank charges.
"""

from treasury.utils.voucher_summary import REVERSAL_SUFFIX

SOURCES = {
	"Bank Charges": ("Bank Charge", ("bank_charge",)),
	# bt_fee: the fee on its own voucher (a party that refuses deductions, e.g. a custodian)
	"Transfer Fees": ("Bank Transfer", ("bt_transfer", "bt_fee")),
	"Cheque Clearance Charges": ("PDC Cheque", ("pdc_bank_posting", "pdc_bank_charges")),
	"Bounce Charges Recovered": ("PDC Cheque", ("pdc_bounce_charges",)),
	"LG Commissions": ("Letter of Guarantee", ("lg_commission", "lg_extension_commission")),
	"LC Commissions and Charges": (
		"Letter of Credit",
		("lc_commission", "lc_amendment_charges", "lc_document_charges"),
	),
	# Payment Entry mode (spec 7g §4.2): the fees are a deduction row of the drawdown's own Payment Entry (only its
	# Expense rows count: the liability row is not a charge)
	"Facility Fees": ("Facility Drawdown", ("facility_upfront_fees", "facility_drawdown")),
}


def event_sources() -> dict[str, str]:
	"""{event: source label} for every event of SOURCES and its reversal (`<event>_reversal`)."""
	result = {}
	for label, (_doctype, events) in SOURCES.items():
		for event in events:
			result[event] = label
			result[event + REVERSAL_SUFFIX] = label
	return result

"""Bridge to the `custody` app (spec 9): cheques (and, from 9b, bank transfers) that pay or return the custody
of a Custody Request.

Treasury runs unchanged without custody: nothing here imports custody at load time (`compat` is the only module
that does, inside its functions), and every entry point starts with `is_installed()` or with the party-type
check (the Party Type "Custodian" only exists with custody).
"""

import frappe

APP = "custody"
PARTY_TYPE = "Custodian"
REQUEST = "Custody Request"


def is_installed() -> bool:
	return APP in frappe.get_installed_apps()

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.ui.form.on("Treasury Company Settings", {
	setup(frm) {
		const account_query = (extra) => () => ({
			filters: Object.assign({ company: frm.doc.company, is_group: 0 }, extra || {}),
		});

		frm.set_query("bounce_charges_account", account_query());
		frm.set_query("bank_account", "pdc_accounts", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query(
			"pdc_payable_account",
			"pdc_accounts",
			account_query({ account_type: "Bank", root_type: "Liability" })
		);
		frm.set_query(
			"pdc_receivable_account",
			"pdc_accounts",
			account_query({ account_type: "Bank", root_type: "Asset" })
		);
		frm.set_query(
			"under_collection_account",
			"pdc_accounts",
			account_query({ account_type: "Bank", root_type: "Asset" })
		);
		frm.set_query("bank_charges_account", "pdc_accounts", account_query());
	},

	outgoing_party_posting(frm) {
		if (frm.doc.outgoing_party_posting === "On Creation") {
			frappe.show_alert({
				message: __(
					"Outgoing cheques will be posted to the party as soon as they are created, even if they are never handed over."
				),
				indicator: "orange",
			});
		}
	},
});

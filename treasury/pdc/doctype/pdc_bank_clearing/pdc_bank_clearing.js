// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Matching and clearing rules live in pdc_bank_clearing.py and treasury/pdc/lifecycle.py.
frappe.ui.form.on("PDC Bank Clearing", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
	},

	onload(frm) {
		if (!frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_user_default("Company"));
		}
	},

	refresh(frm) {
		frm.disable_save();
		frm.set_df_property("matches", "cannot_add_rows", 1);
	},

	company(frm) {
		frm.set_value("bank_account", "");
	},

	get_suggestions_button(frm) {
		frm.call({ doc: frm.doc, method: "get_suggestions", freeze: true }).then((r) => {
			frm.refresh_field("matches");
			if (!r.message) {
				frappe.show_alert({ message: __("No matching bank transactions found."), indicator: "orange" });
			}
		});
	},

	clear_selected_button(frm) {
		const selected = (frm.doc.matches || []).filter((row) => row.selected);
		if (!selected.length) {
			frappe.msgprint(__("Select the matches to clear."));
			return;
		}
		frappe.confirm(__("Clear {0} cheque(s) and reconcile their bank transactions?", [selected.length]), () => {
			frm.call({ doc: frm.doc, method: "clear_selected", freeze: true }).then(() => {
				frm.refresh_field("matches");
			});
		});
	},
});

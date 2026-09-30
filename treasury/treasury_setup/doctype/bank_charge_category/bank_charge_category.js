// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Validation lives in bank_charge_category.py.
frappe.ui.form.on("Bank Charge Category", {
	setup(frm) {
		frm.set_query("expense_account", "accounts", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { company: row.company, root_type: "Expense", is_group: 0 } };
		});
		frm.set_query("cost_center", "accounts", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { company: row.company, is_group: 0 } };
		});
	},
});

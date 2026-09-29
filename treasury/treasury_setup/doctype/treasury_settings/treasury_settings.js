// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.ui.form.on("Treasury Settings", {
	setup(frm) {
		// New Payee creates Suppliers in this group: a leaf group only
		frm.set_query("payee_supplier_group", () => ({ filters: { is_group: 0 } }));
	},
});

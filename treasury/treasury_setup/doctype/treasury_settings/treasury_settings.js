// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.ui.form.on("Treasury Settings", {
	setup(frm) {
		// New Payee creates Suppliers in this group: a leaf group only
		frm.set_query("payee_supplier_group", () => ({ filters: { is_group: 0 } }));
		frm.set_query("supplier", "bank_suppliers", () => ({ filters: { disabled: 0 } }));
	},

	refresh(frm) {
		// design 7d P12: an empty Bank Suppliers table is the normal case (shown only while it is empty)
		const field = frm.get_field("bank_suppliers_hint");
		field &&
			field.$wrapper.html(
				`<p class="text-muted small">${frappe.utils.escape_html(
					__(
						"Empty is fine: the first charge posted on a bank creates a Supplier named after the Bank (group: Payee Supplier Group) and adds the row here. Add a row only to reuse an existing Supplier."
					)
				)}</p>`
			);
	},
});

// the hint's depends_on follows the rows as they are added or removed
const TS_REFRESH_HINT = (frm) => frm.layout && frm.layout.refresh_dependency && frm.layout.refresh_dependency();
frappe.ui.form.on("Treasury Bank Supplier", {
	bank_suppliers_add: TS_REFRESH_HINT,
	bank_suppliers_remove: TS_REFRESH_HINT,
});

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Loaded on Payment Entry and Journal Entry through hooks.py `doctype_js` (plain file, no bundle).
// Display only: vouchers created by Treasury link back to their source document, where they are cancelled.
// The same file is loaded for both doctypes: register the handlers once.
frappe.treasury_voucher_link_doctypes = frappe.treasury_voucher_link_doctypes || new Set();
["Payment Entry", "Journal Entry"].forEach((doctype) => {
	if (frappe.treasury_voucher_link_doctypes.has(doctype)) {
		return;
	}
	frappe.treasury_voucher_link_doctypes.add(doctype);
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			const source_doctype = frm.doc.treasury_source_doctype;
			const source_name = frm.doc.treasury_source_name;
			if (!source_doctype || !source_name) {
				return;
			}
			frm.set_intro(
				__("Managed by Treasury — cancel from the source document {0}.", [
					`<a href="/app/${frappe.router.slug(source_doctype)}/${encodeURIComponent(source_name)}">${frappe.utils.escape_html(
						source_name
					)}</a>`,
				]),
				"blue"
			);
			frm.add_custom_button(__("Open {0}", [__(source_doctype)]), () => {
				frappe.set_route("Form", source_doctype, source_name);
			});
		},
	});
});

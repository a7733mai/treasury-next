// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.ui.form.on("Cheque Book", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
	},

	refresh(frm) {
		if (frm.is_new() || frm.doc.status === "Closed" || !frm.has_perm("write")) {
			return;
		}

		frm.add_custom_button(
			__("Cancel Leaf"),
			() => {
				frappe.prompt(
					[
						{
							fieldname: "cheque_no",
							fieldtype: "Data",
							label: __("Cheque No"),
							reqd: 1,
						},
						{
							fieldname: "reason",
							fieldtype: "Small Text",
							label: __("Reason"),
							reqd: 1,
						},
					],
					(values) => {
						frappe
							.call({
								method: "treasury.pdc.doctype.cheque_book.cheque_book.cancel_leaf",
								args: {
									cheque_book: frm.doc.name,
									cheque_no: values.cheque_no,
									reason: values.reason,
								},
								freeze: true,
							})
							.then(() => frm.reload_doc());
					},
					__("Cancel Leaf"),
					__("Cancel Leaf")
				);
			},
			__("Actions")
		);

		frm.add_custom_button(
			__("Close Book"),
			() => {
				frappe.confirm(__("Close this cheque book? No more cheque numbers will be allocated from it."), () =>
					frm.call("close").then(() => frm.reload_doc())
				);
			},
			__("Actions")
		);
	},
});

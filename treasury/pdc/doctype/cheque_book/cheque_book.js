// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

const CHEQUE_BOOK_METHOD = "treasury.pdc.doctype.cheque_book.cheque_book.";
// cheque_book.VOID_REASON_TYPES (stored in English, translated with the child DocType as context)
const VOID_REASON_TYPES = ["Damaged", "Misprinted", "Lost", "Stolen", "Other"];

frappe.ui.form.on("Cheque Book", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
	},

	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frm.events.show_leaf_summary(frm);
		frm.add_custom_button(
			__("Leaf Register"),
			() =>
				frappe.set_route("query-report", "Cheque Leaf Register", {
					company: frm.doc.company,
					cheque_book: frm.doc.name,
				}),
			__("View")
		);
		frm.add_custom_button(
			__("Print Leaves"),
			() => {
				const params = new URLSearchParams({
					doctype: frm.doctype,
					name: frm.doc.name,
					format: "Cheque Book Leaves",
					_lang: frappe.boot.lang || "",
				});
				window.open(`/printview?${params.toString()}`);
			},
			__("Print", null, "PDC Cheque")
		);
		if (!frm.has_perm("write")) {
			return;
		}
		// record keeping: a leaf of a closed book may still be voided
		frm.add_custom_button(__("Void Leaf"), () => frm.events.void_leaf(frm), __("Actions"));
		if (frm.doc.status === "Closed") {
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
								method: CHEQUE_BOOK_METHOD + "cancel_leaf",
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

	show_leaf_summary(frm) {
		// cheque_leaves.book_summary: {total, used, cancelled, void, remaining}
		const summary = frm.doc.__onload && frm.doc.__onload.leaf_summary;
		if (!summary) {
			return;
		}
		frm.dashboard.add_indicator(__("Used: {0}", [summary.used]), "blue");
		frm.dashboard.add_indicator(__("Cancelled: {0}", [summary.cancelled]), "gray");
		frm.dashboard.add_indicator(__("Void: {0}", [summary.void]), "gray");
		frm.dashboard.add_indicator(
			__("Remaining: {0} of {1}", [summary.remaining, summary.total]),
			summary.remaining > 5 ? "green" : "orange"
		);
	},

	void_leaf(frm) {
		frappe.prompt(
			[
				{ fieldname: "from_cheque_no", fieldtype: "Data", label: __("From Cheque No"), reqd: 1 },
				{
					fieldname: "to_cheque_no",
					fieldtype: "Data",
					label: __("To Cheque No"),
					description: __("Empty: only the From leaf."),
				},
				{
					fieldname: "reason_type",
					fieldtype: "Select",
					label: __("Reason Type", null, "Cheque Book Cancelled Leaf"),
					options: VOID_REASON_TYPES.map((value) => ({
						value,
						label: __(value, null, "Cheque Book Cancelled Leaf"),
					})),
					default: "Damaged",
					reqd: 1,
				},
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
			],
			(values) => {
				frappe
					.call({
						method: CHEQUE_BOOK_METHOD + "void_leaf",
						args: { cheque_book: frm.doc.name, ...values },
						freeze: true,
					})
					.then((r) => {
						frappe.show_alert({
							message: __("Leaves voided: {0}", [frappe.utils.escape_html((r.message || []).join(", "))]),
							indicator: "green",
						});
						frm.reload_doc();
					});
			},
			__("Void Leaf"),
			__("Void Leaf")
		);
	},
});

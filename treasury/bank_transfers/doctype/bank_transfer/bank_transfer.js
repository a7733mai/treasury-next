// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Minimal form script (Phase 7b-1): the voucher summary and the lifecycle actions. Every action is a
// whitelisted method of the document (treasury.bank_transfers.lifecycle); the server checks the permission,
// the role, the status and the dates.

const BT_CTX = "Bank Transfer";

frappe.ui.form.on("Bank Transfer", {
	refresh(frm) {
		const summary = frm.doc.__onload && frm.doc.__onload.voucher_summary;
		frm.get_field("vouchers_html").$wrapper.html(summary || "");
		if (frm.doc.docstatus !== 1) {
			return;
		}
		const status = frm.doc.status;
		const is_manager = frm.doc.__onload && frm.doc.__onload.is_manager;
		const remarks = { fieldname: "remarks", fieldtype: "Small Text", label: __("Remarks", null, "Treasury") };
		if (status === "Scheduled") {
			add_action(frm, "send_now", __("Send Now", null, BT_CTX), [
				{
					fieldname: "execution_date",
					fieldtype: "Date",
					label: __("Execution Date", null, BT_CTX),
					default: frappe.datetime.get_today(),
				},
				remarks,
			]);
			add_action(frm, "reschedule", __("Reschedule", null, BT_CTX), [
				{ fieldname: "execution_date", fieldtype: "Date", label: __("Execution Date", null, BT_CTX), reqd: 1 },
				remarks,
			]);
		}
		if (status === "Sent") {
			add_action(frm, "mark_completed", __("Mark Completed", null, BT_CTX), [
				{
					fieldname: "completion_date",
					fieldtype: "Date",
					label: __("Completion Date", null, BT_CTX),
					default: frappe.datetime.get_today(),
				},
				{ fieldname: "bank_transaction_date", fieldtype: "Date", label: __("Bank Transaction Date", null, BT_CTX) },
				remarks,
			]);
		}
		if (["Sent", "Completed"].includes(status) && is_manager && frm.doc.transfer_type !== "Party Receipt") {
			add_action(frm, "mark_returned", __("Mark Returned", null, BT_CTX), [
				{
					fieldname: "return_date",
					fieldtype: "Date",
					label: __("Return Date", null, BT_CTX),
					default: frappe.datetime.get_today(),
				},
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Return Reason", null, BT_CTX), reqd: 1 },
			]);
		}
	},
});

function add_action(frm, method, label, fields) {
	frm.add_custom_button(
		label,
		() => {
			frappe.prompt(
				fields,
				(values) => {
					frm.call({ doc: frm.doc, method: method, args: values, freeze: true }).then(() =>
						frm.reload_doc()
					);
				},
				label,
				label
			);
		},
		__("Actions", null, BT_CTX)
	);
}

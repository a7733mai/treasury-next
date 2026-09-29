// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// List scripts share the global scope: the names below are prefixed `BT_LIST_` / `bt_list_`.
const BT_LIST_CTX = "Bank Transfer";
// company -> Transfer Approval Threshold (company currency), read once when the list opens
const BT_LIST_THRESHOLDS = {};
const BT_LIST_COLORS = {
	Draft: "gray",
	Scheduled: "purple",
	Sent: "blue",
	Completed: "green",
	Returned: "red",
	Cancelled: "darkgrey",
};
// vertical arrows read the same in both directions
const BT_LIST_ARROWS = { "Party Payment": "↑", "Party Receipt": "↓", "Internal Transfer": "⇅" };

frappe.listview_settings["Bank Transfer"] = {
	add_fields: [
		"status",
		"transfer_type",
		"method",
		"execution_date",
		"docstatus",
		"company",
		"base_amount",
		"payment_entry",
	],

	onload(listview) {
		// "Awaiting Approval": a draft above its company's threshold (the server enforces it on submit)
		if (!frappe.model.can_read("Treasury Company Settings")) {
			return;
		}
		frappe.db
			.get_list("Treasury Company Settings", {
				fields: ["name", "transfer_approval_threshold"],
				limit: 0,
			})
			.then((rows) => {
				(rows || []).forEach((row) => {
					BT_LIST_THRESHOLDS[row.name] = flt(row.transfer_approval_threshold);
				});
				listview.refresh();
			});
	},

	get_indicator(doc) {
		if (doc.docstatus === 0) {
			const threshold = BT_LIST_THRESHOLDS[doc.company] || 0;
			if (threshold > 0 && flt(doc.base_amount) > threshold) {
				return [__("Awaiting Approval", null, BT_LIST_CTX), "orange", "docstatus,=,0"];
			}
			return [__("Draft", null, BT_LIST_CTX), "gray", "status,=,Draft"];
		}
		const today = frappe.datetime.get_today();
		if (doc.status === "Scheduled" && doc.execution_date && doc.execution_date <= today) {
			// "|" separates filters
			return [__("Overdue", null, "Treasury"), "red", `status,=,Scheduled|execution_date,<=,${today}`];
		}
		const label =
			doc.status === "Sent" && doc.transfer_type === "Party Receipt"
				? __("Received", null, BT_LIST_CTX)
				: __(doc.status, null, BT_LIST_CTX);
		return [label, BT_LIST_COLORS[doc.status] || "gray", `status,=,${doc.status}`];
	},

	formatters: {
		method(value, df, doc) {
			if (!value) {
				return "";
			}
			const esc = frappe.utils.escape_html;
			const type = doc.transfer_type || "";
			return `<span title="${esc(__(type, null, BT_LIST_CTX))}">${BT_LIST_ARROWS[type] || ""}</span> ${esc(
				__(value, null, BT_LIST_CTX)
			)}`;
		},
		payment_entry(value) {
			return value ? `<bdi>${frappe.utils.escape_html(value)}</bdi>` : "";
		},
	},
};

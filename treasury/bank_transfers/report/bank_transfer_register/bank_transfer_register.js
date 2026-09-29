// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

const BT_REGISTER_CTX = "Bank Transfer";
const BT_REGISTER_COLORS = {
	Draft: "gray",
	Scheduled: "purple",
	Sent: "blue",
	Completed: "green",
	Returned: "red",
	Cancelled: "darkgrey",
};

function bt_register_options(values) {
	return ["", ...values].map((value) => ({ value, label: value ? __(value, null, BT_REGISTER_CTX) : "" }));
}

frappe.query_reports["Bank Transfer Register"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "bank_account",
			label: __("Bank Account", null, "Treasury"),
			fieldtype: "Link",
			options: "Bank Account",
			get_query: () => ({
				filters: { company: frappe.query_report.get_filter_value("company"), is_company_account: 1 },
			}),
		},
		{
			fieldname: "method",
			label: __("Method", null, BT_REGISTER_CTX),
			fieldtype: "Select",
			options: bt_register_options(["Normal", "Fawran", "SWIFT"]),
		},
		{
			fieldname: "transfer_type",
			label: __("Transfer Type", null, BT_REGISTER_CTX),
			fieldtype: "Select",
			options: bt_register_options(["Party Payment", "Party Receipt", "Internal Transfer"]),
		},
		{
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "Select",
			options: bt_register_options(["Draft", "Scheduled", "Sent", "Completed", "Returned", "Cancelled"]),
		},
		{
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Link",
			options: "Party Type",
		},
		{
			fieldname: "party",
			label: __("Party", null, "Treasury"),
			fieldtype: "Dynamic Link",
			options: "party_type",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		const esc = frappe.utils.escape_html;
		// the status as a coloured pill (the server sends the English status too, for the colour)
		if (column.fieldname === "status_label" && data && data.status) {
			const label =
				data.status === "Sent" && data.transfer_type === "Party Receipt"
					? __("Received", null, BT_REGISTER_CTX)
					: __(data.status, null, BT_REGISTER_CTX);
			return `<span class="indicator-pill ${BT_REGISTER_COLORS[data.status] || "gray"}">${esc(label)}</span>`;
		}
		// the paper-filing key, as a link
		if (column.fieldname === "payment_entry" && data && value) {
			return `<a href="/app/payment-entry/${encodeURIComponent(value)}"><bdi>${esc(value)}</bdi></a>`;
		}
		value = default_formatter(value, row, column, data);
		// cancelled transfers (listed only when that status is chosen) are muted
		if (data && data.status === "Cancelled") {
			return `<span class="text-muted">${value}</span>`;
		}
		return value;
	},
};

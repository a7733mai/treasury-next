// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

const BT_REGISTER_CTX = "Bank Transfer";

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
};

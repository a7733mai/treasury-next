// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

const BT_FEES_CTX = "Bank Transfer";

frappe.query_reports["Transfer Fees Analysis"] = {
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
			default: frappe.datetime.add_months(frappe.datetime.month_start(), -11),
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
			label: __("Method", null, BT_FEES_CTX),
			fieldtype: "Select",
			options: ["", "Normal", "Fawran", "SWIFT"].map((value) => ({
				value,
				label: value ? __(value, null, BT_FEES_CTX) : "",
			})),
		},
		{
			fieldname: "group_by",
			label: __("Group By", null, "Treasury Report"),
			fieldtype: "Select",
			default: "Month",
			options: ["Month", "Bank", "Bank Account", "Method"].map((value) => ({
				value,
				label: __(value, null, "Treasury Report"),
			})),
		},
	],
};

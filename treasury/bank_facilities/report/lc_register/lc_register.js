// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["LC Register"] = {
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
			fieldname: "facility",
			label: __("Bank Facility"),
			fieldtype: "Link",
			options: "Bank Facility",
			get_query: () => ({ filters: { company: frappe.query_report.get_filter_value("company") } }),
		},
		{
			fieldname: "bank",
			label: __("Bank"),
			fieldtype: "Link",
			options: "Bank",
		},
		{
			fieldname: "supplier",
			label: __("Supplier"),
			fieldtype: "Link",
			options: "Supplier",
		},
		{
			fieldname: "lc_type",
			label: __("LC Type"),
			fieldtype: "Select",
			options: ["", "Sight", "Usance"].map((value) => ({
				value,
				label: value ? __(value, null, "Letter of Credit") : "",
			})),
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: ["", "Opened", "Amended", "Documents Received", "Accepted", "Settled", "Expired"].map(
				(value) => ({ value, label: value ? __(value, null, "Letter of Credit") : "" })
			),
		},
		{
			fieldname: "open_only",
			label: __("Open Only"),
			fieldtype: "Check",
			default: 1,
		},
	],
};

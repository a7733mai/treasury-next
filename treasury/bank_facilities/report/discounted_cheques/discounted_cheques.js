// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Discounted Cheques"] = {
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
			label: __("Bank", null, "Treasury"),
			fieldtype: "Link",
			options: "Bank",
		},
		{
			fieldname: "cheque_discounting",
			label: __("Cheque Discounting"),
			fieldtype: "Link",
			options: "Cheque Discounting",
			get_query: () => ({
				filters: { company: frappe.query_report.get_filter_value("company"), docstatus: 1 },
			}),
		},
		{
			fieldname: "status",
			label: __("Cheque Status"),
			fieldtype: "Select",
			options: ["", "Discounted", "Settled", "Bounced"].map((value) => ({
				value,
				label: value ? __(value, null, "Cheque Discounting Item") : "",
			})),
		},
		{
			fieldname: "from_date",
			label: __("Due From"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("Due To"),
			fieldtype: "Date",
		},
	],

	tree: true,
	name_field: "row_id",
	parent_field: "parent_row_id",
	initial_depth: 1,

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (data && data.indent === 0 && column.fieldname === "label") {
			formatted = `<b>${formatted}</b>`;
		}
		return formatted;
	},
};

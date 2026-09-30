// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Cost of Financing"] = {
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
			label: __("From Date", null, "Treasury Report"),
			fieldtype: "Date",
			default: frappe.datetime.year_start(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date", null, "Treasury Report"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "periodicity",
			label: __("Periodicity", null, "Treasury Report"),
			fieldtype: "Select",
			options: [
				{ value: "Monthly", label: __("Monthly", null, "Treasury Report") },
				{ value: "Quarterly", label: __("Quarterly", null, "Treasury Report") },
				{ value: "Yearly", label: __("Yearly", null, "Treasury Report") },
			],
			default: "Monthly",
			reqd: 1,
		},
		{
			fieldname: "bank",
			label: __("Bank", null, "Treasury"),
			fieldtype: "Link",
			options: "Bank",
		},
		{
			fieldname: "facility",
			label: __("Bank Facility"),
			fieldtype: "Link",
			options: "Bank Facility",
			get_query: () => ({ filters: { company: frappe.query_report.get_filter_value("company") } }),
		},
		{
			fieldname: "lender_type",
			label: __("Lender Type"),
			fieldtype: "Select",
			options: ["", "Bank", "Finance Company"].map((value) => ({
				value,
				label: value ? __(value, null, "Bank Facility") : "",
			})),
		},
	],

	tree: true,
	name_field: "row_id",
	parent_field: "parent_row_id",
	initial_depth: 1,

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (data && !data.indent && ["label", "total"].includes(column.fieldname)) {
			formatted = `<b>${formatted}</b>`;
		}
		return formatted;
	},
};

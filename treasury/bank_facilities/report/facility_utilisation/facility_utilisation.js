// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Facility Utilisation"] = {
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
			fieldname: "bank",
			label: __("Bank"),
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
			fieldname: "limit_type",
			label: __("Limit Type"),
			fieldtype: "Select",
			options: [
				"",
				"Overdraft",
				"Short Term Loan",
				"Long Term Loan",
				"Letter of Guarantee",
				"Letter of Credit",
				"Cheque Discounting",
				"Murabaha",
				"Tawarruq",
				"Other",
			].map((value) => ({ value, label: value ? __(value, null, "Bank Facility") : "" })),
		},
		{
			fieldname: "as_on",
			label: __("As On"),
			fieldtype: "Date",
		},
		{
			fieldname: "include_closed",
			label: __("Include Closed"),
			fieldtype: "Check",
			default: 0,
		},
	],

	tree: true,
	name_field: "row_id",
	parent_field: "parent_row_id",
	initial_depth: 2,

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;
		if (data.indent < 2 && column.fieldname === "label") {
			formatted = `<b>${formatted}</b>`;
		}
		if (column.fieldname === "percent_used" && flt(value) >= 90) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		if (column.fieldname === "days_to_expiry" && data.indent === 1 && cint(value) <= 30) {
			formatted = `<span style="color: var(--orange-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

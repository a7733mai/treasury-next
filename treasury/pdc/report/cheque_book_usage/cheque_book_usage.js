// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Cheque Book Usage"] = {
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
			fieldname: "bank_account",
			label: __("Bank Account", null, "Treasury"),
			fieldtype: "Link",
			options: "Bank Account",
			get_query: function () {
				return {
					filters: {
						company: frappe.query_report.get_filter_value("company"),
						is_company_account: 1,
					},
				};
			},
		},
		{
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Active", label: __("Active") },
				{ value: "Exhausted", label: __("Exhausted") },
				{ value: "Closed", label: __("Closed") },
			],
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		if (data && column.fieldname === "status") {
			value = value ? __(value) : value;
		}
		let formatted = default_formatter(value, row, column, data);
		if (data && column.fieldname === "remaining_leaves" && data.status === "Active" && value <= 5) {
			formatted = `<span style="color: var(--orange-600); font-weight: bold">${formatted}</span>`;
		}
		return formatted;
	},
};

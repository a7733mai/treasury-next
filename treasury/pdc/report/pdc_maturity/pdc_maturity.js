// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["PDC Maturity"] = {
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
			fieldname: "direction",
			label: __("Direction"),
			fieldtype: "Select",
			options: [
				{ value: "Outgoing", label: __("Outgoing") },
				{ value: "Incoming", label: __("Incoming") },
			],
			default: "Outgoing",
			reqd: 1,
		},
		{
			fieldname: "bank_account",
			label: __("Bank Account"),
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
			fieldname: "as_on",
			label: __("As On"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "group_by",
			label: __("Group By", null, "Treasury Report"),
			fieldtype: "Select",
			options: [
				{ value: "Bank Account", label: __("Bank Account") },
				{ value: "Party", label: __("Party Name") },
			],
			default: "Bank Account",
			reqd: 1,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;
		if (data.is_total) {
			formatted = `<b>${formatted}</b>`;
		}
		if (column.fieldname === "overdue" && flt(value) > 0) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

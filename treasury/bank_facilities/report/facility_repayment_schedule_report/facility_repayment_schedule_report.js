// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Facility Repayment Schedule Report"] = {
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
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date", null, "Treasury Report"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), 90),
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
			fieldname: "drawdown",
			label: __("Facility Drawdown"),
			fieldtype: "Link",
			options: "Facility Drawdown",
			get_query: () => ({
				filters: { company: frappe.query_report.get_filter_value("company"), docstatus: 1 },
			}),
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: ["", "Pending", "Partially Paid", "Overdue", "Paid"].map((value) => ({
				value,
				label: value ? __(value, null, "Treasury Report") : "",
			})),
		},
		{
			fieldname: "include_overdue",
			label: __("Include Overdue"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "include_paid",
			label: __("Include Paid"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (data && data.status === "Overdue" && ["due_date", "status_label", "unpaid"].includes(column.fieldname)) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

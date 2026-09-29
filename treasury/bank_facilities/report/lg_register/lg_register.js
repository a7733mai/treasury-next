// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["LG Register"] = {
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
			fieldname: "lg_type",
			label: __("LG Type"),
			fieldtype: "Select",
			options: ["", "Bid Bond", "Performance", "Advance Payment", "Retention", "Financial", "Customs", "Other"].map(
				(value) => ({ value, label: value ? __(value, null, "Letter of Guarantee") : "" })
			),
		},
		{
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "Select",
			options: ["", "Issued", "Extended", "Reduced", "Released", "Claimed", "Expired"].map((value) => ({
				value,
				label: value ? __(value, null, "Letter of Guarantee") : "",
			})),
		},
		{
			fieldname: "open_only",
			label: __("Open Only"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "expiring_within",
			label: __("Expiring Within (Days)"),
			fieldtype: "Int",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (data && column.fieldname === "days_to_expiry" && value !== null && value !== undefined && cint(value) <= 30) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

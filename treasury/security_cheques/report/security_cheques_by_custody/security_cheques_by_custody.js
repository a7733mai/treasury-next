// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Security Cheques by Custody"] = {
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
			fieldname: "custody_location",
			label: __("Custody Location"),
			fieldtype: "Link",
			options: "Custody Location",
		},
		{
			fieldname: "custodian",
			label: __("Custodian", null, "Treasury"),
			fieldtype: "Link",
			options: "User",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		if (data && ["status", "direction", "purpose", "amount_type"].includes(column.fieldname)) {
			value = value ? __(value, null, column.fieldname === "direction" ? null : "Security Cheque") : value;
		}
		let formatted = default_formatter(value, row, column, data);
		if (data && (data.is_group || data.is_total)) {
			formatted = `<b>${formatted}</b>`;
		}
		return formatted;
	},
};

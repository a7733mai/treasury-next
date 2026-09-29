// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Expired Security Cheques Not Returned"] = {
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
			fieldname: "as_on",
			label: __("As On"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "direction",
			label: __("Direction"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Incoming", label: __("Incoming") },
				{ value: "Outgoing", label: __("Outgoing") },
			],
		},
		{
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Customer", label: __("Customer") },
				{ value: "Supplier", label: __("Supplier") },
				{ value: "Employee", label: __("Employee", null, "Security Cheque") },
			],
			on_change: function () {
				frappe.query_report.set_filter_value("party", "");
			},
		},
		{
			fieldname: "party",
			label: __("Party", null, "Treasury Report"),
			fieldtype: "Dynamic Link",
			get_options: function () {
				const party_type = frappe.query_report.get_filter_value("party_type");
				if (!party_type) {
					frappe.throw(__("Please select Party Type first"));
				}
				return party_type;
			},
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		if (data && ["status", "direction", "purpose", "amount_type"].includes(column.fieldname)) {
			value = value ? __(value, null, column.fieldname === "direction" ? null : "Security Cheque") : value;
		}
		let formatted = default_formatter(value, row, column, data);
		if (data && data.is_total) {
			formatted = `<b>${formatted}</b>`;
		}
		if (data && column.fieldname === "days_expired" && value > 0) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Security Exposure"] = {
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
			fieldname: "group_by",
			label: __("Group By", null, "Treasury Report"),
			fieldtype: "Select",
			options: [
				{ value: "Party", label: __("Party", null, "Treasury Report") },
				{ value: "Reference Document", label: __("Reference Document", null, "Treasury Report") },
			],
			default: "Party",
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
		{
			fieldname: "reference_doctype",
			label: __("Reference Type"),
			fieldtype: "Link",
			options: "DocType",
			on_change: function () {
				frappe.query_report.set_filter_value("reference_name", "");
			},
		},
		{
			fieldname: "reference_name",
			label: __("Reference Name", null, "Treasury"),
			fieldtype: "Dynamic Link",
			get_options: function () {
				const doctype = frappe.query_report.get_filter_value("reference_doctype");
				if (!doctype) {
					frappe.throw(__("Please select the Reference Type first"));
				}
				return doctype;
			},
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (data && column.fieldname === "shortfall" && value > 0) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

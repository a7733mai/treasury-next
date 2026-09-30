// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Events of the Security Cheque Status Log, plus the report-only "Custody Moved" (a custody row on its own).
const SECURITY_MOVEMENT_EVENTS = [
	"Created",
	"Received",
	"Stored",
	"Custody Transferred",
	"Issued",
	"Returned",
	"Lost",
	"Cancelled",
	"Encashed",
	"Expiry Extended",
	"Custody Moved",
];

frappe.query_reports["Security Cheque Movements"] = {
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
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
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
				{ value: "Other", label: __("Other", null, "Security Cheque") },
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
				if (!party_type || party_type === "Other") {
					frappe.throw(__("Please select Party Type first"));
				}
				return party_type;
			},
		},
		{
			fieldname: "event",
			label: __("Event", null, "Security Cheque"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				const search = (txt || "").toLowerCase();
				return SECURITY_MOVEMENT_EVENTS.filter(
					(e) => !search || e.toLowerCase().includes(search) || __(e, null, "Security Cheque").includes(txt)
				).map((e) => ({ value: e, description: __(e, null, "Security Cheque") }));
			},
		},
		{
			fieldname: "movement",
			label: __("Movement", null, "Security Cheque"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Status", label: __("Status", null, "Treasury") },
				{ value: "Custody", label: __("Custody", null, "Treasury") },
			],
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
		{
			fieldname: "user",
			label: __("User", null, "Treasury Report"),
			fieldtype: "Link",
			options: "User",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		if (data && ["event", "from_status", "to_status", "purpose"].includes(column.fieldname) && value) {
			value = __(value, null, "Security Cheque");
		}
		if (data && column.fieldname === "direction" && value) {
			value = __(value);
		}
		return default_formatter(value, row, column, data);
	},
};

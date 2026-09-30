// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Design 7f §10. Events of the Security Cheque Status Log, plus the report-only "Custody Moved" (a custody row on
// its own), each with its pill colour. Stored in English, translated with the context "Security Cheque".
const SECURITY_MOVEMENT_EVENT_COLORS = {
	Created: "gray",
	Received: "blue",
	Stored: "cyan",
	"Custody Transferred": "light-blue",
	"Custody Moved": "light-blue",
	Issued: "orange",
	Returned: "green",
	Encashed: "purple",
	Lost: "red",
	Cancelled: "darkgrey",
	"Expiry Extended": "yellow",
};
const SECURITY_MOVEMENT_EVENTS = Object.keys(SECURITY_MOVEMENT_EVENT_COLORS);
const SECURITY_MOVEMENT_USERS = ["from_custodian", "to_custodian", "user"];

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
				{ value: "Status", label: __("Status Changes") },
				{ value: "Custody", label: __("Custody Moves") },
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
		if (!data) {
			return default_formatter(value, row, column, data);
		}
		const esc = frappe.utils.escape_html;
		switch (column.fieldname) {
			case "cheque_no":
				return `<bdi>${esc(value || "")}</bdi>`;
			case "direction":
				return value
					? `<span style="color: var(${value === "Incoming" ? "--blue-600" : "--orange-600"})">${esc(__(value))}</span>`
					: "";
			case "event":
				return value
					? `<span class="indicator-pill ${SECURITY_MOVEMENT_EVENT_COLORS[value] || "gray"}">${esc(
							__(value, null, "Security Cheque")
					  )}</span>`
					: "";
			case "from_status":
				return value ? `<span class="text-muted">${esc(__(value, null, "Security Cheque"))}</span>` : "";
			case "to_status":
				return value ? `<b>${esc(__(value, null, "Security Cheque"))}</b>` : "";
			case "from_location":
				return value ? default_formatter(value, row, column, data) : `<span class="text-muted">—</span>`;
			case "to_location": {
				if (!value) {
					return `<span class="text-muted">—</span>`;
				}
				const formatted = default_formatter(value, row, column, data);
				return value !== data.from_location ? `<b>${formatted}</b>` : formatted;
			}
			case "amount":
				if (["Open", "Blank"].includes(data.amount_type)) {
					return `<span class="text-muted">${esc(__("Open", null, "Cheque Leaf"))}</span>`;
				}
				break;
			case "purpose":
				return value ? esc(__(value, null, "Security Cheque")) : "";
			case "datetime":
				return `<span class="text-muted">${default_formatter(value, row, column, data)}</span>`;
		}
		if (SECURITY_MOVEMENT_USERS.includes(column.fieldname) && value) {
			// the full name, linked to the user
			return `<a href="/app/user/${encodeURIComponent(value)}">${esc(frappe.user.full_name(value) || value)}</a>`;
		}
		return default_formatter(value, row, column, data);
	},
};

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Bounced and Postponed Cheques"] = {
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
			fieldname: "view",
			label: __("View", null, "Treasury Report"),
			fieldtype: "Select",
			options: [
				{ value: "Bounced", label: __("Bounced") },
				{ value: "Postponed", label: __("Postponed") },
			],
			default: "Bounced",
			reqd: 1,
		},
		{
			fieldname: "direction",
			label: __("Direction"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Outgoing", label: __("Outgoing") },
				{ value: "Incoming", label: __("Incoming") },
			],
		},
		{
			// any Party Type: a cheque may be of an Employee, a Shareholder ...
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Link",
			options: "Party Type",
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
			fieldname: "from_date",
			label: __("From Date", null, "Treasury Report"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date", null, "Treasury Report"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		// every Payment Entry of the comma list is a link (export and print keep the plain list)
		if (column.fieldname === "payment_entry" && data && value) {
			return value.split(",").map((n) => n.trim()).filter(Boolean)
				.map((n) => `<a href="/app/payment-entry/${encodeURIComponent(n)}"><bdi>${frappe.utils.escape_html(n)}</bdi></a>`)
				.join(", ");
		}
		if (data && ["operational_status", "direction"].includes(column.fieldname)) {
			value = value ? __(value) : value;
		}
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;
		// frequent postponers stand out
		if (column.fieldname === "party_postponements" && value > 1) {
			const color = value > 2 ? "var(--red-600)" : "var(--orange-600)";
			formatted = `<span style="color: ${color}; font-weight: bold">${formatted}</span>`;
		}
		return formatted;
	},
};

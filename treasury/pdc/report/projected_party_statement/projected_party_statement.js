// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Projected Party Statement"] = {
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
			// any Party Type: a cheque may be of an Employee, a Shareholder ...
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Link",
			options: "Party Type",
			default: "Supplier",
			reqd: 1,
			on_change: function () {
				frappe.query_report.set_filter_value("party", "");
			},
		},
		{
			fieldname: "party",
			label: __("Party", null, "Treasury Report"),
			fieldtype: "Dynamic Link",
			reqd: 1,
			get_options: function () {
				return frappe.query_report.get_filter_value("party_type");
			},
		},
		{
			fieldname: "from_date",
			label: __("From Date", null, "Treasury Report"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
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
			fieldname: "include_created_outgoing",
			label: __("Include Created Outgoing Cheques"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		// every Payment Entry of the comma list is a link (export and print keep the plain list)
		if (column.fieldname === "payment_entry" && data && value) {
			return value.split(",").map((n) => n.trim()).filter(Boolean)
				.map((n) => `<a href="/app/payment-entry/${encodeURIComponent(n)}"><bdi>${frappe.utils.escape_html(n)}</bdi></a>`)
				.join(", ");
		}
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;
		if (column.fieldname === "voucher_type" && value) {
			formatted = __(value);
		}
		if (data.bold) {
			formatted = `<b>${formatted}</b>`;
		}
		if (data.row_type === "projected" && column.fieldname !== "projected_balance") {
			formatted = `<span class="text-muted">${formatted}</span>`;
		}
		return formatted;
	},
};

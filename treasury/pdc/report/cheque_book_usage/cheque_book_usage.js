// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Cheque Book Usage"] = {
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
			fieldname: "bank_account",
			label: __("Bank Account", null, "Treasury"),
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
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Active", label: __("Active") },
				{ value: "Exhausted", label: __("Exhausted") },
				{ value: "Closed", label: __("Closed") },
			],
		},
	],

	onload: function (report) {
		// design 7f P19: from the book summary to its leaves in one click
		report.page.add_inner_button(__("Leaf Register"), function () {
			const options = { company: report.get_filter_value("company") };
			if (report.get_filter_value("bank_account")) {
				options.bank_account = report.get_filter_value("bank_account");
			}
			if (report.get_filter_value("status")) {
				options.book_status = report.get_filter_value("status");
			}
			frappe.set_route("query-report", "Cheque Leaf Register", options);
		});
	},

	formatter: function (value, row, column, data, default_formatter) {
		// every Payment Entry of the comma list is a link (export and print keep the plain list)
		if (column.fieldname === "payment_entry" && data && value) {
			return value.split(",").map((n) => n.trim()).filter(Boolean)
				.map((n) => `<a href="/app/payment-entry/${encodeURIComponent(n)}"><bdi>${frappe.utils.escape_html(n)}</bdi></a>`)
				.join(", ");
		}
		if (data && column.fieldname === "status") {
			value = value ? __(value) : value;
		}
		if (data && column.fieldname === "void_leaves") {
			return `<span class="text-muted">${default_formatter(value, row, column, data)}</span>`;
		}
		let formatted = default_formatter(value, row, column, data);
		if (data && column.fieldname === "remaining_leaves" && data.status === "Active" && value <= 5) {
			formatted = `<span style="color: var(--orange-600); font-weight: bold">${formatted}</span>`;
		}
		return formatted;
	},
};

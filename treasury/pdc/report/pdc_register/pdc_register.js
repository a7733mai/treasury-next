// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["PDC Register"] = {
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
			fieldname: "operational_status",
			label: __("Operational Status"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				const statuses = [
					"Created",
					"Delivered",
					"Received",
					"Deposited",
					"Cleared",
					"Bounced",
					"Returned",
					"Endorsed",
					"Discounted",
					"Replaced",
					"Cancelled",
				];
				const search = (txt || "").toLowerCase();
				return statuses
					.filter((s) => !search || s.toLowerCase().includes(search) || __(s).includes(txt))
					.map((s) => ({ value: s, description: __(s) }));
			},
		},
		{
			fieldname: "accounting_status",
			label: __("Accounting Status"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Not Posted", label: __("Not Posted") },
				{ value: "Party Posted", label: __("Party Posted") },
				{ value: "Under Collection Posted", label: __("Under Collection Posted") },
				{ value: "Bank Posted", label: __("Bank Posted") },
				{ value: "Reversed", label: __("Reversed") },
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
			],
			on_change: function () {
				frappe.query_report.set_filter_value("party", "");
			},
		},
		{
			fieldname: "party",
			label: __("Party"),
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
			fieldname: "bank_account",
			label: __("Bank Account"),
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
			fieldname: "date_based_on",
			label: __("Date Based On"),
			fieldtype: "Select",
			options: [
				{ value: "Issue Date", label: __("Issue Date") },
				{ value: "Handover / Receipt Date", label: __("Handover / Receipt Date") },
				{ value: "Original Due Date", label: __("Original Due Date") },
				{ value: "Effective Due Date", label: __("Effective Due Date") },
				{ value: "Clearance Date", label: __("Actual Clearance Date") },
			],
			default: "Effective Due Date",
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Party", label: __("Party Name") },
				{ value: "Bank Account", label: __("Bank Account") },
				{ value: "Operational Status", label: __("Operational Status") },
			],
		},
		{
			fieldname: "postponed_only",
			label: __("Postponed Only"),
			fieldtype: "Check",
		},
		{
			fieldname: "expected_only",
			label: __("Expected in Bank Position Only"),
			fieldtype: "Check",
		},
		{
			fieldname: "include_created",
			label: __("Include Created Outgoing Cheques"),
			fieldtype: "Check",
			depends_on: "eval:doc.expected_only",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		if (data && ["operational_status", "accounting_status", "direction"].includes(column.fieldname)) {
			value = value ? __(value) : value;
		}
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;
		if (data.is_group || data.is_total) {
			formatted = `<b>${formatted}</b>`;
		}
		if (column.fieldname === "days_to_due" && value !== null && value !== undefined && value < 0) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

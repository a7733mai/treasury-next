// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Security Cheques Summary"] = {
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
			fieldname: "purpose",
			label: __("Purpose", null, "Security Cheque"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Tender", label: __("Tender", null, "Security Cheque") },
				{ value: "Contract Performance", label: __("Contract Performance", null, "Security Cheque") },
				{ value: "Advance Payment", label: __("Advance Payment", null, "Security Cheque") },
				{ value: "Rent", label: __("Rent", null, "Security Cheque") },
				{ value: "Employee Custody", label: __("Employee Custody", null, "Security Cheque") },
				{ value: "Facility Collateral", label: __("Facility Collateral", null, "Security Cheque") },
				{ value: "Other", label: __("Other", null, "Security Cheque") },
			],
		},
		{
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "MultiSelectList",
			description: __("None selected: the cheques held / given now."),
			get_data: function (txt) {
				const statuses = ["Created", "Received", "In Custody", "Issued", "Returned", "Encashed", "Lost", "Cancelled"];
				const search = (txt || "").toLowerCase();
				return statuses
					.filter(
						(s) => !search || s.toLowerCase().includes(search) || __(s, null, "Security Cheque").includes(txt)
					)
					.map((s) => ({ value: s, description: __(s, null, "Security Cheque") }));
			},
		},
		{
			fieldname: "group_by",
			label: __("Group By", null, "Security Cheque"),
			fieldtype: "Select",
			options: [
				{ value: "Party", label: __("Party", null, "Treasury Report") },
				{ value: "Purpose", label: __("Purpose", null, "Security Cheque") },
				{ value: "Status", label: __("Status", null, "Treasury") },
			],
			default: "Party",
		},
		{
			fieldname: "as_on",
			label: __("As On"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "ageing_ranges",
			label: __("Ageing Ranges (Days)"),
			fieldtype: "Data",
			default: "30, 60, 90",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		if (data && ["status", "purpose"].includes(column.fieldname) && value) {
			value = __(value, null, "Security Cheque");
		}
		let formatted = default_formatter(value, row, column, data);
		if (data && data.is_group) {
			formatted = `<b>${formatted}</b>`;
		}
		if (data && column.fieldname === "expired" && value > 0) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Security Cheque Register"] = {
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
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				const statuses = [
					"Created",
					"Received",
					"In Custody",
					"Issued",
					"Returned",
					"Encashed",
					"Lost",
					"Cancelled",
				];
				const search = (txt || "").toLowerCase();
				return statuses
					.filter(
						(s) =>
							!search ||
							s.toLowerCase().includes(search) ||
							__(s, null, "Security Cheque").includes(txt)
					)
					.map((s) => ({ value: s, description: __(s, null, "Security Cheque") }));
			},
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
				{
					value: "Contract Performance",
					label: __("Contract Performance", null, "Security Cheque"),
				},
				{ value: "Advance Payment", label: __("Advance Payment", null, "Security Cheque") },
				{ value: "Rent", label: __("Rent", null, "Security Cheque") },
				{ value: "Employee Custody", label: __("Employee Custody", null, "Security Cheque") },
				{
					value: "Facility Collateral",
					label: __("Facility Collateral", null, "Security Cheque"),
				},
				{ value: "Other", label: __("Other", null, "Security Cheque") },
			],
		},
		{
			fieldname: "amount_type",
			label: __("Amount Type"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Fixed", label: __("Fixed", null, "Security Cheque") },
				{ value: "Open / Blank", label: __("Open / Blank", null, "Security Cheque") },
			],
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
		{
			fieldname: "custody_location",
			label: __("Custody Location"),
			fieldtype: "Link",
			options: "Custody Location",
		},
		{
			fieldname: "expiry_from",
			label: __("Expiry From"),
			fieldtype: "Date",
		},
		{
			fieldname: "expiry_to",
			label: __("Expiry To"),
			fieldtype: "Date",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		// every Payment Entry of the comma list is a link (export and print keep the plain list)
		if (column.fieldname === "payment_entry" && data && value) {
			return value.split(",").map((n) => n.trim()).filter(Boolean)
				.map((n) => `<a href="/app/payment-entry/${encodeURIComponent(n)}"><bdi>${frappe.utils.escape_html(n)}</bdi></a>`)
				.join(", ");
		}
		if (data && ["status", "direction", "purpose", "amount_type"].includes(column.fieldname)) {
			const context = column.fieldname === "direction" ? null : "Security Cheque";
			value = value ? __(value, null, context) : value;
		}
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;
		if (data.is_total) {
			formatted = `<b>${formatted}</b>`;
		}
		const expired = value !== null && value !== undefined && value < 0;
		if (column.fieldname === "days_to_expiry" && expired) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.query_reports["Bank Position Forecast"] = {
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
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				return frappe.db.get_link_options("Bank Account", txt, {
					company: frappe.query_report.get_filter_value("company"),
					is_company_account: 1,
				});
			},
		},
		{
			fieldname: "from_date",
			label: __("From Date", null, "Treasury Report"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
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
			fieldname: "periodicity",
			label: __("Periodicity", null, "Treasury Report"),
			fieldtype: "Select",
			options: [
				{ value: "Daily", label: __("Daily", null, "Treasury Report") },
				{ value: "Weekly", label: __("Weekly", null, "Treasury Report") },
				{ value: "Monthly", label: __("Monthly", null, "Treasury Report") },
			],
			default: "Weekly",
			reqd: 1,
		},
		{
			fieldname: "currency_view",
			label: __("Currency View"),
			fieldtype: "Select",
			options: [
				{ value: "Company Currency", label: __("Company Currency", null, "Treasury Report") },
				{ value: "Bank Currency", label: __("Bank Currency") },
			],
			default: "Company Currency",
			reqd: 1,
		},
		{
			fieldname: "include_created",
			label: __("Include Created Outgoing Cheques"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "include_overdue",
			label: __("Include Overdue"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "show_details",
			label: __("Show Cheques"),
			fieldtype: "Check",
			default: 0,
		},
	],

	tree: true,
	name_field: "row_id",
	parent_field: "parent_row_id",
	initial_depth: 1,

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;

		if (!data.indent && column.fieldname === "period") {
			formatted = `<b>${formatted}</b>`;
		}
		if (data.is_overdue && ["period", "expected_in", "expected_out"].includes(column.fieldname)) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		if (column.fieldname === "closing" && flt(value) < 0) {
			formatted = `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		// drill-down: the cheques of a period in the PDC Register, dated like the forecast dates them
		if (!data.indent && ["expected_in", "expected_out"].includes(column.fieldname) && flt(value)) {
			const filters = frappe.query_report.get_filter_values();
			const banks = filters.bank_account || [];
			const today = frappe.datetime.get_today();
			const params = {
				company: filters.company,
				direction: column.fieldname === "expected_in" ? "Incoming" : "Outgoing",
				date_based_on: "Forecast Date",
				expected_only: 1,
				include_created: filters.include_created ? 1 : 0,
				to_date: data.period_end,
			};
			// cheques dated before today are in the Overdue row (or left out), never in a period
			if (data.period_start) {
				params.from_date = data.period_start > today ? data.period_start : today;
			}
			if (banks.length === 1) params.bank_account = banks[0];
			if (filters.currency_view === "Bank Currency" && data.currency) params.currency = data.currency;
			const url = `/app/query-report/${encodeURIComponent("PDC Register")}?${new URLSearchParams(
				params
			).toString()}`;
			// the register filters one bank account only: with several selected it lists every bank
			const title =
				banks.length > 1
					? __("Open in PDC Register (all bank accounts: approximate)")
					: __("Open in PDC Register");
			formatted = `<a href="${url}" title="${frappe.utils.escape_html(title)}">${formatted}</a>`;
		}
		return formatted;
	},
};

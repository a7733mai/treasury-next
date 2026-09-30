// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Levels, thresholds and positions are computed on the server (treasury/balance_alerts/positions.py).
const BBO_CTX = "Treasury Alert Log";
const BBO_LEVEL_COLORS = { Critical: "var(--red-600)", Warning: "var(--orange-600)", OK: "var(--green-600)" };

frappe.query_reports["Bank Balance Outlook"] = {
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
			fieldname: "horizon_days",
			label: __("Horizon (Days)", null, "Treasury Settings"),
			fieldtype: "Int",
			default: 7,
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
			fieldname: "level",
			label: __("Level", null, BBO_CTX),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Critical", label: __("Critical", null, BBO_CTX) },
				{ value: "Warning", label: __("Warning", null, BBO_CTX) },
				{ value: "OK", label: __("OK", null, BBO_CTX) },
				{ value: "No Threshold", label: __("No Threshold", null, BBO_CTX) },
			],
		},
	],

	onload: function (report) {
		// the horizon of the alerts (Treasury Settings) unless the route gave one
		if ((frappe.route_options && frappe.route_options.horizon_days) || !frappe.model.can_read("Treasury Settings")) {
			return;
		}
		frappe.db.get_single_value("Treasury Settings", "balance_alert_horizon").then((value) => {
			if (cint(value) && cint(value) !== cint(report.get_filter_value("horizon_days"))) {
				report.set_filter_value("horizon_days", cint(value));
			}
		});
	},

	formatter: function (value, row, column, data, default_formatter) {
		let formatted = default_formatter(value, row, column, data);
		if (!data) return formatted;
		if (column.fieldname === "level_label" && BBO_LEVEL_COLORS[data.level]) {
			formatted = `<span style="color: ${BBO_LEVEL_COLORS[data.level]}; font-weight: 600">${formatted}</span>`;
		}
		if (column.fieldname === "lowest_balance" && data.bank_account) {
			// the bank's daily forecast over the same horizon (treasury.balance_alerts.notify.forecast_route)
			const filters = frappe.query_report.get_filter_values();
			const today = frappe.datetime.get_today();
			const horizon = Math.max(cint(filters.horizon_days) || 7, 1);
			const params = new URLSearchParams({
				company: filters.company,
				from_date: today,
				to_date: frappe.datetime.add_days(today, horizon - 1),
				periodicity: "Daily",
				include_overdue: 1,
				show_details: 1,
				currency_view: "Bank Currency",
				bank_account: data.bank_account,
			});
			const url = `/app/query-report/${encodeURIComponent("Bank Position Forecast")}?${params.toString()}`;
			formatted = `<a href="${url}" title="${frappe.utils.escape_html(
				__("Open in Bank Position Forecast")
			)}">${formatted}</a>`;
		}
		return formatted;
	},
};

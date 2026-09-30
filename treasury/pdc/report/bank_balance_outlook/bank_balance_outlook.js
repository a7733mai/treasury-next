// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Levels, thresholds and positions are computed on the server (treasury/balance_alerts/positions.py).
// Report scripts share the global scope: the names below are prefixed `BBO_` / `bbo_`.
const BBO_CTX = "Treasury Alert Log";
const BBO_LEVEL_CTX = "Bank Balance Outlook";
const BBO_PILLS = { Critical: "red", Warning: "orange", OK: "green", "No Threshold": "gray" };
const BBO_TEXT = { Critical: "text-danger", Warning: "text-warning" };
const BBO_ALERT_ROLES = ["Treasury Manager", "System Manager"];
const BBO_ALERTS_METHOD = "treasury.balance_alerts.notify.";

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
				{ value: "Critical", label: __("Critical", null, BBO_LEVEL_CTX) },
				{ value: "Warning", label: __("Warning", null, BBO_LEVEL_CTX) },
				{ value: "OK", label: __("OK", null, BBO_LEVEL_CTX) },
				{ value: "No Threshold", label: __("No Threshold", null, BBO_LEVEL_CTX) },
			],
		},
	],

	onload: function (report) {
		bbo_add_menu(report);
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
		if (!data) {
			return formatted;
		}
		const level = data.level;
		switch (column.fieldname) {
			case "bank_account":
				return level === "Critical" ? `<b>${formatted}</b>` : formatted;
			case "level_label":
				// a colour and a word
				return `<span class="indicator-pill ${BBO_PILLS[level] || "gray"}">${frappe.utils.escape_html(
					__(level || "", null, BBO_LEVEL_CTX)
				)}</span>`;
			case "lowest_balance":
				return bbo_lowest_link(formatted, data);
			case "first_breach":
				return value && BBO_TEXT[level] ? `<span class="${BBO_TEXT[level]}">${formatted}</span>` : formatted;
			case "shortfall":
				return flt(value) > 0 ? `<span class="text-danger"><b>${formatted}</b></span>` : "";
			case "warning_balance":
			case "minimum_balance":
				return value === null || value === undefined ? "" : `<span class="text-muted">${formatted}</span>`;
			default:
				return formatted;
		}
	},
};

function bbo_lowest_link(formatted, data) {
	// the bank's daily forecast over the same horizon (treasury.balance_alerts.notify.forecast_route); red
	// below the minimum, orange below the warning balance
	if (!data.bank_account) {
		return formatted;
	}
	const lowest = flt(data.lowest_balance);
	let css = "";
	if (data.minimum_balance !== null && data.minimum_balance !== undefined && lowest < flt(data.minimum_balance)) {
		css = "text-danger";
	} else if (data.warning_balance !== null && data.warning_balance !== undefined && lowest < flt(data.warning_balance)) {
		css = "text-warning";
	}
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
	return `<a href="${url}" class="${css}" title="${frappe.utils.escape_html(
		__("Open in Bank Position Forecast")
	)}">${formatted}</a>`;
}

function bbo_add_menu(report) {
	// design 7e P1 (managers): preview, test send and alert check for the filter company; the thresholds
	const group = __("Bank Position Alerts", null, "Treasury Settings");
	const company = () => report.get_filter_value("company");
	const esc = frappe.utils.escape_html;
	if (frappe.model.can_read("Treasury Company Settings")) {
		report.page.add_inner_button(
			__("Thresholds"),
			() => company() && frappe.set_route("Form", "Treasury Company Settings", company()),
			group
		);
	}
	if (!frappe.user.has_role(BBO_ALERT_ROLES)) {
		return;
	}
	report.page.add_inner_button(
		__("Preview Digest"),
		() => {
			const dialog = new frappe.ui.Dialog({
				title: __("Preview Digest"),
				fields: [
					{
						fieldname: "user",
						label: __("User"),
						fieldtype: "Link",
						options: "User",
						reqd: 1,
						default: frappe.session.user,
						get_query: () => ({ filters: { enabled: 1, user_type: "System User" } }),
						description: __(
							"Shows exactly what this user would receive: only the banks they may see, in their language."
						),
					},
				],
				primary_action_label: __("Preview Digest"),
				primary_action(values) {
					dialog.hide();
					bbo_show_digest(company(), values.user);
				},
			});
			dialog.show();
		},
		group
	);
	report.page.add_inner_button(
		__("Send Test to Me"),
		() =>
			frappe.xcall(BBO_ALERTS_METHOD + "send_test_digest", { company: company() }).then((r) => {
				if (r.sent) {
					frappe.show_alert({ message: esc(r.message), indicator: "green" });
				} else {
					frappe.msgprint({ message: esc(r.message || ""), indicator: "orange" });
				}
			}),
		group
	);
	report.page.add_inner_button(
		__("Run Alerts Now"),
		() =>
			frappe.confirm(
				esc(
					__(
						"Evaluates the thresholds now and sends any new critical or warning alert. Alerts already sent today are not repeated."
					)
				),
				() =>
					frappe.xcall(BBO_ALERTS_METHOD + "run_alerts_now", { company: company() }).then((r) => {
						frappe.show_alert({ message: esc(r.message), indicator: "blue" });
					})
			),
		group
	);
}

function bbo_show_digest(company, user) {
	const esc = frappe.utils.escape_html;
	frappe.xcall(BBO_ALERTS_METHOD + "preview_digest", { company, user }).then((r) => {
		if (!r.html) {
			frappe.msgprint({ message: esc(r.message || ""), indicator: "orange" });
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Digest preview for {0} ({1})", [esc(r.user), esc(company)]),
			size: "extra-large",
			fields: [{ fieldname: "preview", fieldtype: "HTML" }],
		});
		// a sandboxed frame: the e-mail's inline styles stay out of the desk, its links open a new tab
		const $frame = $(
			'<iframe sandbox="allow-popups allow-popups-to-escape-sandbox" style="width: 100%; height: 70vh; border: 0;"></iframe>'
		);
		$frame.attr("srcdoc", `<base target="_blank">${r.html}`);
		dialog
			.get_field("preview")
			.$wrapper.empty()
			.append(`<p class="text-muted small"><b>${esc(r.subject || "")}</b></p>`)
			.append($frame);
		dialog.show();
	});
}

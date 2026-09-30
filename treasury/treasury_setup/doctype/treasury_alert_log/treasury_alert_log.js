// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Written by treasury/balance_alerts only (read only, no buttons). Form scripts share the global scope: the
// names below are prefixed `TAL_` / `tal_`.
const TAL_COLORS = { Critical: "red", Warning: "orange" };

frappe.ui.form.on("Treasury Alert Log", {
	refresh(frm) {
		const doc = frm.doc;
		const esc = frappe.utils.escape_html;
		const amount = (value) => `<bdi>${esc(format_currency(value, doc.currency))}</bdi>`;
		if (doc.alert_type === "Balance Alert" && doc.bank_account) {
			const sentence =
				doc.level === "Critical"
					? __("{0}: projected {1} on {2}, minimum {3}.", [
							`<b><bdi>${esc(doc.bank_account)}</bdi></b>`,
							amount(doc.balance),
							esc(frappe.datetime.str_to_user(doc.projected_date)),
							amount(doc.threshold),
					  ])
					: __("{0}: projected {1} on {2}, warning {3}.", [
							`<b><bdi>${esc(doc.bank_account)}</bdi></b>`,
							amount(doc.balance),
							esc(frappe.datetime.str_to_user(doc.projected_date)),
							amount(doc.threshold),
					  ]);
			// the values are escaped above; the sentence is our own translation
			frm.dashboard.set_headline(
				`<div>${sentence} &nbsp; <a href="${esc(tal_forecast_route(doc))}">${esc(
					__("Open in Bank Position Forecast")
				)}</a></div>`,
				TAL_COLORS[doc.level] || "blue"
			);
		} else if (doc.alert_type === "Override" && doc.reference_doctype && doc.reference_name) {
			const route = `/app/${frappe.router.slug(doc.reference_doctype)}/${encodeURIComponent(doc.reference_name)}`;
			frm.dashboard.set_headline(
				__("Override by {0} on {1}.", [
					`<b>${esc(frappe.user.full_name(doc.user) || doc.user || "")}</b>`,
					`<a href="${route}">${esc(__(doc.reference_doctype))} <b><bdi>${esc(doc.reference_name)}</bdi></b></a>`,
				]),
				"purple"
			);
		}
	},
});

function tal_forecast_route(doc) {
	// the bank's daily forecast from the alert day (treasury.balance_alerts.notify.forecast_route)
	const from_date = doc.alert_date || frappe.datetime.get_today();
	const params = new URLSearchParams({
		company: doc.company,
		from_date,
		to_date: frappe.datetime.add_days(from_date, 6),
		periodicity: "Daily",
		include_overdue: 1,
		show_details: 1,
		currency_view: "Bank Currency",
		bank_account: doc.bank_account,
	});
	return `/app/query-report/${encodeURIComponent("Bank Position Forecast")}?${params.toString()}`;
}

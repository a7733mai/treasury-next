// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Design 7g §8.3: the installment states of the drawdown form (`display_status`, P24), the repayment mode, the
// installment cheque and the payment voucher (the filing key, in bold).
const FRS_STATE_COLORS = {
	Paid: "green",
	"Partially Paid": "orange",
	"Cheque Issued": "blue",
	Pending: "gray",
	"Awaiting Statement": "yellow",
	"Posting Today": "light-blue",
	"Cheque Due": "orange",
	Overdue: "red",
};
const FRS_CHEQUE_COLORS = {
	Created: "gray",
	Delivered: "blue",
	Cleared: "green",
	Bounced: "red",
	Replaced: "darkgrey",
	Cancelled: "darkgrey",
};
const FRS_MODE_COLORS = { "Auto Debit": "var(--cyan-600)", "PDC to Lender": "var(--blue-600)" };

frappe.query_reports["Facility Repayment Schedule Report"] = {
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
			fieldname: "lender_type",
			label: __("Lender Type"),
			fieldtype: "Select",
			options: ["", "Bank", "Finance Company"].map((value) => ({
				value,
				label: value ? __(value, null, "Bank Facility") : "",
			})),
		},
		{
			fieldname: "bank",
			label: __("Bank", null, "Treasury"),
			fieldtype: "Link",
			options: "Bank",
		},
		{
			fieldname: "facility",
			label: __("Bank Facility"),
			fieldtype: "Link",
			options: "Bank Facility",
			get_query: () => ({ filters: { company: frappe.query_report.get_filter_value("company") } }),
		},
		{
			fieldname: "drawdown",
			label: __("Facility Drawdown"),
			fieldtype: "Link",
			options: "Facility Drawdown",
			get_query: () => ({
				filters: { company: frappe.query_report.get_filter_value("company"), docstatus: 1 },
			}),
		},
		{
			fieldname: "repayment_mode",
			label: __("Repayment Mode"),
			fieldtype: "Select",
			options: ["", "Manual", "Auto Debit", "PDC to Lender"].map((value) => ({
				value,
				label: value ? __(value, null, "Facility Drawdown") : "",
			})),
		},
		{
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "Select",
			options: ["", "Pending", "Partially Paid", "Overdue", "Paid"].map((value) => ({
				value,
				label: value ? __(value, null, "Treasury Report") : "",
			})),
		},
		{
			fieldname: "include_overdue",
			label: __("Include Overdue"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "include_paid",
			label: __("Include Paid"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		const esc = frappe.utils.escape_html;
		const field = column.fieldname;
		if (!data) {
			return default_formatter(value, row, column, data);
		}
		if (field === "status_label") {
			const color = FRS_STATE_COLORS[data.display_status || data.status] || "gray";
			return value ? `<span class="indicator-pill ${color}">${esc(value)}</span>` : "";
		}
		if (field === "cheque_status_label") {
			const color = FRS_CHEQUE_COLORS[data.cheque_status] || "orange";
			return value ? `<span class="indicator-pill ${color}">${esc(value)}</span>` : "";
		}
		if (field === "payment_journal" && value) {
			const doctype = data.payment_voucher_type || "Journal Entry";
			return `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(value)}"><b><bdi>${esc(
				value
			)}</bdi></b></a>`;
		}
		let formatted = default_formatter(value, row, column, data);
		if (field === "repayment_mode_label" && FRS_MODE_COLORS[data.repayment_mode]) {
			return `<span style="color: ${FRS_MODE_COLORS[data.repayment_mode]}">${formatted}</span>`;
		}
		if (["due_date", "unpaid"].includes(field)) {
			if (data.display_status === "Awaiting Statement") {
				return `<span style="color: var(--yellow-600)">${formatted}</span>`;
			}
			if (data.status === "Overdue" && data.display_status !== "Cheque Due") {
				return `<span style="color: var(--red-600)">${formatted}</span>`;
			}
		}
		return formatted;
	},
};

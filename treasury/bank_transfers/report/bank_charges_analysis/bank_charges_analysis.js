// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// The data, totals and chart are built by bank_charges_analysis.py; the Source labels mirror
// treasury.bank_transfers.charge_sources.SOURCES.
const BCA_CTX = "Bank Charge";
const BCA_SOURCES = [
	"Bank Charges",
	"Transfer Fees",
	"Cheque Clearance Charges",
	"Bounce Charges Recovered",
	"LG Commissions",
	"LC Commissions and Charges",
	"Facility Fees",
];

frappe.query_reports["Bank Charges Analysis"] = {
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
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "bank",
			label: __("Bank", null, "Treasury"),
			fieldtype: "Link",
			options: "Bank",
		},
		{
			fieldname: "bank_account",
			label: __("Bank Account", null, "Treasury"),
			fieldtype: "Link",
			options: "Bank Account",
			get_query: () => ({
				filters: { company: frappe.query_report.get_filter_value("company"), is_company_account: 1 },
			}),
		},
		{
			fieldname: "source",
			label: __("Source", null, BCA_CTX),
			fieldtype: "Select",
			options: ["", ...BCA_SOURCES].map((value) => ({ value, label: value ? __(value, null, BCA_CTX) : "" })),
		},
		{
			fieldname: "category",
			label: __("Category", null, BCA_CTX),
			fieldtype: "Link",
			options: "Bank Charge Category",
		},
		{
			fieldname: "group_by",
			label: __("Group By", null, "Treasury Report"),
			fieldtype: "Select",
			default: "Month",
			options: ["Month", "Bank", "Bank Account", "Category", "Source"].map((value) => ({
				value,
				label: __(value, null, "Treasury Report"),
			})),
		},
		{
			fieldname: "view",
			label: __("View", null, BCA_CTX),
			fieldtype: "Select",
			default: "Summary",
			options: ["Summary", "Detail"].map((value) => ({ value, label: __(value, null, BCA_CTX) })),
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		// a group opens the Detail view of its vouchers
		if (column.fieldname === "group" && data && !data.is_total && data.group_filter) {
			const esc = frappe.utils.escape_html;
			const text = default_formatter(value, row, column, data);
			return `<a class="bca-drill" data-filter="${esc(data.group_filter)}" data-value="${esc(
				data.group_value || ""
			)}">${text}</a>`;
		}
		value = default_formatter(value, row, column, data);
		return data && data.is_total ? `<b>${value}</b>` : value;
	},

	onload: function (report) {
		$(report.page.wrapper).on("click", ".bca-drill", function (event) {
			event.preventDefault();
			bca_drill_down(report, $(this).attr("data-filter"), $(this).attr("data-value"));
		});
	},
};

function bca_drill_down(report, fieldname, value) {
	const filters = Object.assign({}, report.get_values(), { view: "Detail" });
	if (fieldname === "month") {
		const start = `${value}-01`;
		filters.from_date = start;
		filters.to_date = frappe.datetime.month_end(start);
	} else if (value && value !== "-") {
		filters[fieldname] = value;
	}
	frappe.set_route("query-report", "Bank Charges Analysis", filters);
}

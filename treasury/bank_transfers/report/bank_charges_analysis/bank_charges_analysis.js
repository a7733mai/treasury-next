// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// The data, totals, cards and chart are built by bank_charges_analysis.py; the Source labels and colours
// mirror treasury.bank_transfers.charge_sources.SOURCES and SOURCE_COLORS of the report (design 7d §5).
const BCA_CTX = "Bank Charges Analysis";
const BCA_REPORT = "Bank Charges Analysis";
const BCA_SOURCES = {
	"Bank Charges": "orange",
	"Transfer Fees": "blue",
	"Cheque Clearance Charges": "purple",
	"Bounce Charges Recovered": "green",
	"LG Commissions": "cyan",
	"LC Commissions and Charges": "pink",
	"Facility Fees": "yellow",
};

frappe.query_reports[BCA_REPORT] = {
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
			// design P17: a year of months makes the default Month chart meaningful
			default: bca_year_start(),
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
			get_query: () => {
				const filters = { company: frappe.query_report.get_filter_value("company"), is_company_account: 1 };
				const bank = frappe.query_report.get_filter_value("bank");
				if (bank) {
					filters.bank = bank;
				}
				return { filters };
			},
		},
		{
			fieldname: "source",
			label: __("Source", null, BCA_CTX),
			fieldtype: "Select",
			options: ["", ...Object.keys(BCA_SOURCES)].map((value) => ({
				value,
				label: value ? __(value, null, BCA_CTX) : "",
			})),
		},
		{
			fieldname: "category",
			label: __("Category", null, BCA_CTX),
			fieldtype: "Link",
			options: "Bank Charge Category",
			on_change: () => {
				// a category belongs to the Bank Charges source only
				const report = frappe.query_report;
				if (report.get_filter_value("category") && report.get_filter_value("source")) {
					report.set_filter_value("source", "");
				} else {
					report.refresh();
				}
			},
		},
		{
			fieldname: "group_by",
			label: __("Group By", null, BCA_CTX),
			fieldtype: "Select",
			default: "Month",
			options: ["Month", "Bank", "Bank Account", "Category", "Source"].map((value) => ({
				value,
				label: __(value, null, BCA_CTX),
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
		const esc = frappe.utils.escape_html;
		if (!data) {
			return default_formatter(value, row, column, data);
		}
		const fieldname = column.fieldname;
		if (data.is_total) {
			return `<b>${default_formatter(value, row, column, data)}</b>`;
		}
		if (fieldname === "group") {
			// a group opens the Detail view of its vouchers; a month reads MM-YYYY
			const text =
				data.group_filter === "month" && /^\d{4}-\d{2}$/.test(data.group_value || "")
					? `${data.group_value.slice(5)}-${data.group_value.slice(0, 4)}`
					: value;
			const html = `<bdi>${esc(text == null ? "" : text)}</bdi>`;
			if (!data.group_filter) {
				return html;
			}
			return `<a class="bca-drill" data-filter="${esc(data.group_filter)}" data-value="${esc(
				data.group_value || ""
			)}">${html}</a>`;
		}
		if (fieldname === "share") {
			// the share with a thin bar (a negative share has no bar)
			const share = flt(value);
			const width = Math.max(0, Math.min(100, share));
			return `<div style="display: flex; align-items: center; gap: var(--margin-sm)">
				<span style="min-width: 4.5em; text-align: end">${esc(format_number(share, null, 2))} %</span>
				<span style="flex: 1; height: 4px; background: var(--gray-200); border-radius: 2px">
					<span style="display: block; height: 4px; width: ${width}%; background: var(--orange-500); border-radius: 2px"></span>
				</span>
			</div>`;
		}
		if (fieldname === "source" && data.source_value) {
			const color = BCA_SOURCES[data.source_value] || "gray";
			return `<span class="indicator-pill ${color}">${esc(value || "")}</span>`;
		}
		if (fieldname === "voucher_no" && value) {
			return `<b><bdi>${default_formatter(value, row, column, data)}</bdi></b>`;
		}
		if (fieldname === "document" && value) {
			return `<bdi>${default_formatter(value, row, column, data)}</bdi>`;
		}
		if (fieldname === "category" && value) {
			// the default categories show in the user's language
			return `<a href="/app/bank-charge-category/${encodeURIComponent(value)}">${esc(__(value))}</a>`;
		}
		const formatted = default_formatter(value, row, column, data);
		// money back (refunds, recoveries) in green; net charges in bold
		if ((fieldname === "refunds" && flt(value) > 0) || ((fieldname === "net" || fieldname === "amount") && flt(value) < 0)) {
			return `<span class="text-success">${fieldname === "net" ? `<b>${formatted}</b>` : formatted}</span>`;
		}
		return fieldname === "net" ? `<b>${formatted}</b>` : formatted;
	},

	onload: function (report) {
		$(report.page.wrapper).on("click", ".bca-drill", function (event) {
			event.preventDefault();
			bca_drill_down(report, $(this).attr("data-filter"), $(this).attr("data-value"));
		});
	},
};

function bca_year_start() {
	// the start of the current fiscal year (ERPNext's boot: [name, start, end]), else 1 January
	const year = frappe.boot.current_fiscal_year;
	const today = frappe.datetime.get_today();
	if (year && year[1] && year[1] <= today && today <= year[2]) {
		return year[1];
	}
	return frappe.datetime.year_start();
}

function bca_drill_down(report, fieldname, value) {
	// spec 7d §3.7-11: the Detail view of the group's vouchers
	const filters = Object.assign({}, report.get_values(), { view: "Detail" });
	if (fieldname === "month") {
		const start = `${value}-01`;
		filters.from_date = start;
		filters.to_date = frappe.datetime.month_end(start);
	} else if (fieldname === "category") {
		// a Bank Charge category (the server sends the other sources of this grouping as "source")
		filters.category = value;
		filters.source = "";
	} else if (value && value !== "-") {
		filters[fieldname] = value;
	}
	frappe.set_route("query-report", BCA_REPORT, filters);
}

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Leaf statuses (treasury.pdc.cheque_leaves.LEAF_STATUSES), stored in English, translated with the context
// "Cheque Leaf".
const CHEQUE_LEAF_STATUSES = [
	"Unused",
	"Written",
	"Issued (PDC)",
	"Cleared",
	"Bounced",
	"Replaced",
	"Cancelled Leaf",
	"Void",
	"Security Created",
	"Security Issued",
	"Security Returned",
	"Security Encashed",
	"Security Lost",
	"Security Cancelled",
];

frappe.query_reports["Cheque Leaf Register"] = {
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
			fieldname: "cheque_book",
			label: __("Cheque Book"),
			fieldtype: "Link",
			options: "Cheque Book",
			get_query: function () {
				const filters = { company: frappe.query_report.get_filter_value("company") };
				const bank_account = frappe.query_report.get_filter_value("bank_account");
				if (bank_account) {
					filters.bank_account = bank_account;
				}
				return { filters };
			},
		},
		{
			fieldname: "book_status",
			label: __("Book Status"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Active", label: __("Active", null, "Cheque Book") },
				{ value: "Exhausted", label: __("Exhausted", null, "Cheque Book") },
				{ value: "Closed", label: __("Closed", null, "Cheque Book") },
			],
		},
		{
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				const search = (txt || "").toLowerCase();
				return CHEQUE_LEAF_STATUSES.filter(
					(s) => !search || s.toLowerCase().includes(search) || __(s, null, "Cheque Leaf").includes(txt)
				).map((s) => ({ value: s, description: __(s, null, "Cheque Leaf") }));
			},
		},
		{
			fieldname: "usage",
			label: __("Usage", null, "Cheque Leaf"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Payment", label: __("Payment", null, "Cheque Leaf") },
				{ value: "Security", label: __("Security", null, "Cheque Leaf") },
			],
		},
		{
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Link",
			options: "Party Type",
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
				if (!party_type) {
					frappe.throw(__("Please select Party Type first"));
				}
				return party_type;
			},
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
			fieldname: "view",
			label: __("View", null, "Cheque Leaf"),
			fieldtype: "Select",
			options: [
				{ value: "Leaves", label: __("Leaves", null, "Cheque Leaf") },
				{ value: "Books", label: __("Books", null, "Cheque Leaf") },
			],
			default: "Leaves",
		},
	],

	onload: function (report) {
		report.page.add_inner_button(__("Print Book"), function () {
			const book = frappe.query_report.get_filter_value("cheque_book");
			if (!book) {
				frappe.msgprint(__("Select a Cheque Book first."));
				return;
			}
			const params = new URLSearchParams({
				doctype: "Cheque Book",
				name: book,
				format: "Cheque Book Leaves",
				_lang: frappe.boot.lang || "",
			});
			window.open(`/printview?${params.toString()}`);
		});
	},

	formatter: function (value, row, column, data, default_formatter) {
		// every Payment Entry of the comma list is a link (export and print keep the plain list)
		if (column.fieldname === "payment_entry" && data && value) {
			return value
				.split(",")
				.map((n) => n.trim())
				.filter(Boolean)
				.map((n) => `<a href="/app/payment-entry/${encodeURIComponent(n)}"><bdi>${frappe.utils.escape_html(n)}</bdi></a>`)
				.join(", ");
		}
		if (data && column.fieldname === "status" && value) {
			value = data.is_group ? __(value, null, "Cheque Book") : __(value, null, "Cheque Leaf");
		}
		if (data && column.fieldname === "usage" && value) {
			value = __(value, null, "Cheque Leaf");
		}
		if (data && column.fieldname === "pdc_status" && value) {
			value = __(value);
		}
		if (data && column.fieldname === "cancel_reason_type" && value) {
			value = __(value, null, "Cheque Book Cancelled Leaf");
		}
		let formatted = default_formatter(value, row, column, data);
		if (!data) {
			return formatted;
		}
		if (data.is_group) {
			return `<b>${formatted}</b>`;
		}
		if (["Void", "Cancelled Leaf"].includes(data.status)) {
			return `<span style="color: var(--gray-500)">${formatted}</span>`;
		}
		if (data.status === "Bounced") {
			return `<span style="color: var(--red-600)">${formatted}</span>`;
		}
		return formatted;
	},
};

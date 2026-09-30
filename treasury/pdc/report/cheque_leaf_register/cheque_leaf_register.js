// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Design 7f §2. Leaf statuses (treasury.pdc.cheque_leaves.LEAF_STATUSES) are stored in English and translated
// with the context "Cheque Leaf"; each has a colour and a word (the security colours are those of the Security
// Cheque list, so a leaf reads like its cheque). Unused leaves get no pill: the eye must skip them.
const CHEQUE_LEAF_STATUS_COLORS = {
	Unused: null,
	Written: "yellow",
	"Issued (PDC)": "blue",
	Cleared: "green",
	Bounced: "red",
	Replaced: "gray",
	"Cancelled Leaf": "darkgrey",
	Void: "darkgrey",
	"Security Created": "yellow",
	"Security Issued": "orange",
	"Security Returned": "green",
	"Security Encashed": "purple",
	"Security Lost": "red",
	"Security Cancelled": "darkgrey",
};
const CHEQUE_LEAF_STATUSES = Object.keys(CHEQUE_LEAF_STATUS_COLORS);
const CHEQUE_LEAF_BOOK_COLORS = { Active: "green", Exhausted: "orange", Closed: "gray" };
const CHEQUE_LEAF_UNUSABLE = ["Void", "Cancelled Leaf"];
const CHEQUE_LEAF_LOW_REMAINING = 5;

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
			on_change: function () {
				// a book of another bank no longer applies
				if (frappe.query_report.get_filter_value("cheque_book")) {
					frappe.query_report.set_filter_value("cheque_book", "");
				} else {
					frappe.query_report.refresh();
				}
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
			on_change: function () {
				cheque_leaf_register_book_buttons(frappe.query_report);
				frappe.query_report.refresh();
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
			label: __("Leaf Status"),
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
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Supplier", label: __("Supplier") },
				{ value: "Employee", label: __("Employee") },
				{ value: "Customer", label: __("Customer") },
				{ value: "Shareholder", label: __("Shareholder") },
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
			description: __("Issue date of the cheque, or the date a leaf was cancelled or voided."),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			description: __("Issue date of the cheque, or the date a leaf was cancelled or voided."),
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
		cheque_leaf_register_book_buttons(report);
		// the attribution rule of hand-typed numbers (spec §9.8), under the filters
		if (report.page.page_form && !report.page.main.find(".cheque-leaf-register-help").length) {
			$(
				`<p class="text-muted small cheque-leaf-register-help" style="padding: 0 var(--padding-md, 15px)">${frappe.utils.escape_html(
					__(
						"A leaf without a cheque book on its cheque is attributed to the book of the same bank whose range covers the number: an open book first, then the newest."
					)
				)}</p>`
			).insertAfter(report.page.page_form);
		}
	},

	formatter: function (value, row, column, data, default_formatter) {
		if (!data) {
			return default_formatter(value, row, column, data);
		}
		return cheque_leaf_register_format(value, row, column, data, default_formatter);
	},
};

function cheque_leaf_register_book_buttons(report) {
	// design 7f §2.6: with one book selected, print it or open it (Void Leaf lives on the book)
	const print_label = __("Print Book");
	const open_label = __("Open Cheque Book");
	report.page.remove_inner_button(print_label);
	report.page.remove_inner_button(open_label);
	const book = report.get_filter_value("cheque_book");
	if (!book) {
		return;
	}
	report.page.add_inner_button(print_label, function () {
		const params = new URLSearchParams({
			doctype: "Cheque Book",
			name: report.get_filter_value("cheque_book"),
			format: "Cheque Book Leaves",
			_lang: frappe.boot.lang || "",
		});
		window.open(`/printview?${params.toString()}`);
	});
	report.page.add_inner_button(open_label, function () {
		frappe.set_route("Form", "Cheque Book", report.get_filter_value("cheque_book"));
	});
}

function cheque_leaf_register_pill(label, color) {
	const text = frappe.utils.escape_html(label);
	return color ? `<span class="indicator-pill ${color}">${text}</span>` : `<span class="text-muted">${text}</span>`;
}

function cheque_leaf_register_format(value, row, column, data, default_formatter) {
	const esc = frappe.utils.escape_html;
	const field = column.fieldname;
	const today = frappe.datetime.get_today();

	if (data.is_group) {
		// the book row: identity, status pill and summary sentence
		if (field === "leaf_no") {
			const route = `/app/cheque-book/${encodeURIComponent(data.cheque_book)}`;
			return `<b><a href="${route}"><bdi>${esc(data.cheque_book)}</bdi></a> · <bdi>${esc(value || "")}</bdi></b>`;
		}
		if (field === "status") {
			return value ? cheque_leaf_register_pill(__(value, null, "Cheque Book"), CHEQUE_LEAF_BOOK_COLORS[value]) : "";
		}
		if (field === "party_name") {
			const low = data.status === "Active" && data.remaining_leaves <= CHEQUE_LEAF_LOW_REMAINING;
			const text = esc(
				__("{0} leaves · {1} used · {2} cancelled · {3} void · {4} remaining", [
					data.total_leaves,
					data.used_leaves,
					data.cancelled_leaves,
					data.void_leaves,
					data.remaining_leaves,
				])
			);
			return low ? `<b style="color: var(--orange-600)">${text}</b>` : `<b>${text}</b>`;
		}
		const formatted = default_formatter(value, row, column, data);
		if (field === "remaining_leaves" && data.status === "Active" && value <= CHEQUE_LEAF_LOW_REMAINING) {
			return `<span style="color: var(--orange-600); font-weight: bold">${formatted}</span>`;
		}
		return `<b>${formatted}</b>`;
	}

	const unusable = CHEQUE_LEAF_UNUSABLE.includes(data.status);
	switch (field) {
		case "leaf_no": {
			const number = `<bdi style="font-family: var(--font-family-monospace, monospace)">${esc(value || "")}</bdi>`;
			// [P7] an unusable number is struck through
			return unusable ? `<s class="text-muted">${number}</s>` : number;
		}
		case "status":
			return value ? cheque_leaf_register_pill(__(value, null, "Cheque Leaf"), CHEQUE_LEAF_STATUS_COLORS[value]) : "";
		case "usage":
			return value ? `<span class="text-muted">${esc(__(value, null, "Cheque Leaf"))}</span>` : "";
		case "party_name":
			if (data.restricted) {
				return `<span class="text-muted" title="${esc(__("You do not have access to this document."))}">${esc(
					__("Restricted")
				)}</span>`;
			}
			break;
		case "amount":
			if (["Open", "Blank"].includes(data.amount_type)) {
				return `<span class="text-muted">${esc(__("Open", null, "Cheque Leaf"))}</span>`;
			}
			break;
		case "due_date":
			if (value && value < today && ["Written", "Issued (PDC)"].includes(data.status)) {
				return `<span style="color: var(--red-600)">${default_formatter(value, row, column, data)}</span>`;
			}
			break;
		case "expiry_date":
			if (value && value < today && data.status === "Security Issued") {
				return `<span style="color: var(--red-600)">${default_formatter(value, row, column, data)}</span>`;
			}
			break;
		case "payment_entry":
			// the filing key: every Payment Entry of the comma list is a bold link (export keeps the plain list)
			return (value || "")
				.split(",")
				.map((n) => n.trim())
				.filter(Boolean)
				.map((n) => `<a href="/app/payment-entry/${encodeURIComponent(n)}"><b><bdi>${esc(n)}</bdi></b></a>`)
				.join(", ");
		case "pdc_status":
			return value ? esc(__(value)) : "";
		case "print_count":
			if (!cint(value)) {
				return "";
			}
			// reprinted
			return cint(value) > 1
				? `<span style="color: var(--orange-600); font-weight: bold">${cint(value)}</span>`
				: `${cint(value)}`;
		case "cancel_reason_type":
			return value ? esc(__(value, null, "Cheque Book Cancelled Leaf")) : "";
	}
	return default_formatter(value, row, column, data);
}

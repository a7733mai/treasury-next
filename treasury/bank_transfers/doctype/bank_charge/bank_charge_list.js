// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// List scripts share the global scope: the names below are prefixed `BCH_LIST_` / `bch_list_`.
// The indicator rules mirror bch_indicator of bank_charge.js (design 7d §1.6).
const BCH_LIST_CTX = "Bank Charge";
const BCH_LIST_COLORS = { Posted: "blue", Reconciled: "green" };

frappe.listview_settings["Bank Charge"] = {
	add_fields: [
		"status",
		"entry_type",
		"docstatus",
		"amount",
		"refunded_amount",
		"currency",
		"payment_entry",
		"journal_entry",
	],

	get_indicator(doc) {
		if (doc.docstatus === 0) {
			return [__("Draft", null, BCH_LIST_CTX), "gray", "docstatus,=,0"];
		}
		if (doc.status === "Cancelled" || doc.docstatus === 2) {
			return [__("Cancelled", null, BCH_LIST_CTX), "darkgrey", "status,=,Cancelled"];
		}
		const refunded = flt(doc.refunded_amount);
		if (doc.entry_type === "Charge" && refunded > 0) {
			return refunded >= flt(doc.amount)
				? [__("Refunded", null, BCH_LIST_CTX), "purple", "refunded_amount,>,0"]
				: [__("Partly Refunded", null, BCH_LIST_CTX), "yellow", "refunded_amount,>,0"];
		}
		return [__(doc.status, null, BCH_LIST_CTX), BCH_LIST_COLORS[doc.status] || "blue", `status,=,${doc.status}`];
	},

	formatters: {
		amount(value, df, doc) {
			const esc = frappe.utils.escape_html;
			const text = `<bdi>${esc(format_currency(flt(value), doc.currency))}</bdi>`;
			// a refund is money back: an arrow and green (the arrow reads the same in both directions)
			return doc.entry_type === "Refund"
				? `<span class="text-success"><span title="${esc(__("Refund", null, BCH_LIST_CTX))}">↩</span> ${text}</span>`
				: text;
		},
		payment_entry(value, df, doc) {
			// the voucher column is never empty: the Journal Entry of a Journal Entry-mode company
			const esc = frappe.utils.escape_html;
			if (value) {
				return `<bdi>${esc(value)}</bdi>`;
			}
			if (doc.journal_entry) {
				return `<span title="${esc(__("Journal Entry", null, BCH_LIST_CTX))}"><span class="text-muted">JV</span> <bdi>${esc(
					doc.journal_entry
				)}</bdi></span>`;
			}
			return "";
		},
	},
};

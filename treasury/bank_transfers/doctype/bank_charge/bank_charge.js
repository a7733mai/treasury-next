// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_transfers/charges.py and the controller.
// Form scripts share the global scope: every name below is prefixed `bch_` / `BCH_`.
const BCH_CTX = "Bank Charge";
const BCH_LOG_CTX = "Bank Charge Status Log";
const BCH_METHOD = "treasury.bank_transfers.doctype.bank_charge.bank_charge.";
const BCH_USER_ROLES = ["Treasury User", "Treasury Manager", "System Manager"];
const BCH_INDICATORS = { Draft: "gray", Posted: "blue", Reconciled: "green", Cancelled: "darkgrey" };

frappe.ui.form.on("Bank Charge", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("expense_account", () => ({
			filters: { company: frm.doc.company, root_type: "Expense", is_group: 0 },
		}));
		frm.set_query("category", () => ({ filters: { disabled: 0 } }));
		frm.set_query("against_bank_charge", () => ({
			filters: {
				docstatus: 1,
				entry_type: "Charge",
				company: frm.doc.company,
				bank_account: frm.doc.bank_account,
			},
		}));
		frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("project", () => ({ filters: { company: frm.doc.company } }));
		frm.set_query("mode_of_payment", () => ({ filters: { enabled: 1 } }));
		if (window.erpnext && erpnext.accounts && erpnext.accounts.dimensions) {
			erpnext.accounts.dimensions.setup_dimension_filters(frm, frm.doctype);
		}
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.company) {
			const company = frappe.defaults.get_user_default("Company");
			company && frm.set_value("company", company);
		}
	},

	refresh(frm) {
		bch_translate_log(frm);
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		if (frm.doc.docstatus > 0) {
			frm.page.set_indicator(__(frm.doc.status, null, BCH_CTX), BCH_INDICATORS[frm.doc.status] || "blue");
		}
		if (frm.doc.docstatus === 1 && !frm.is_dirty() && frappe.user.has_role(BCH_USER_ROLES)) {
			bch_add_actions(frm);
		}
	},

	company(frm) {
		bch_load_category_defaults(frm);
	},

	category(frm) {
		bch_load_category_defaults(frm);
	},
});

function bch_load_category_defaults(frm) {
	const doc = frm.doc;
	if (doc.docstatus !== 0 || !doc.category || !doc.company || doc.against_bank_charge) {
		return;
	}
	frappe
		.xcall(BCH_METHOD + "get_category_defaults", { category: doc.category, company: doc.company })
		.then((r) => {
			const values = { expense_account: r.expense_account || "" };
			if (r.cost_center) {
				values.cost_center = r.cost_center;
			}
			if (r.default_description && !frm.doc.description) {
				values.description = r.default_description;
			}
			frm.set_value(values);
		});
}

function bch_add_actions(frm) {
	const doc = frm.doc;
	const group = __("Actions", null, BCH_CTX);
	if (doc.entry_type === "Charge" && flt(doc.refunded_amount) < flt(doc.amount) && frappe.model.can_create(BCH_CTX)) {
		frm.add_custom_button(
			__("Record Refund", null, BCH_CTX),
			() => {
				frappe.xcall(BCH_METHOD + "make_refund", { source_name: doc.name }).then((refund) => {
					const synced = frappe.model.sync(refund)[0];
					frappe.set_route("Form", synced.doctype, synced.name);
				});
			},
			group
		);
	}
	if (doc.status === "Posted" && frappe.model.can_read("PDC Bank Clearing")) {
		frm.add_custom_button(
			__("Match Bank Statement", null, BCH_CTX),
			() => {
				frappe.route_options = { company: doc.company, bank_account: doc.bank_account };
				frappe.set_route("Form", "PDC Bank Clearing");
			},
			group
		);
	}
}

function bch_translate_log(frm) {
	// events and statuses are stored in English in the status log (Data fields): translate them on display
	const contexts = {
		event: BCH_LOG_CTX,
		from_status: BCH_CTX,
		to_status: BCH_CTX,
		from_accounting: BCH_CTX,
		to_accounting: BCH_CTX,
	};
	Object.entries(contexts).forEach(([fieldname, context]) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield("Bank Charge Status Log", fieldname, name);
			if (df) {
				df.formatter = (value) => frappe.utils.escape_html(value ? __(value, null, context) : "");
			}
		}
	});
	frm.refresh_field("status_log");
}

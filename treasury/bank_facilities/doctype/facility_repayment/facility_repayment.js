// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Allocation, the accrued / expensed interest split and the posting are computed on the server
// (treasury/bank_facilities/repayment.py, accounting.py); save the draft to see them.
frappe.ui.form.on("Facility Repayment", {
	setup(frm) {
		frm.set_query("drawdown", () => ({ filters: { docstatus: 1, repayment_status: "Open" } }));
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("penalty_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
	},

	drawdown(frm) {
		frm.set_value("bank_account", null);
		frm.set_value("principal_paid", 0);
		frm.set_value("interest_paid", 0);
		frm.clear_table("allocations");
		frm.refresh_field("allocations");
	},

	refresh(frm) {
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		if (frm.doc.docstatus === 0 && frm.doc.prepayment) {
			frm.set_intro(
				__(
					"The principal beyond the installments due up to the repayment date reduces the later installments; the tenor is kept."
				),
				"blue"
			);
		}
		// who settled it: the bank (a user), the bank's auto debit, or an installment cheque to the lender
		if (!frm.is_new() && frm.doc.settlement && frm.doc.settlement !== "Bank") {
			frm.dashboard.add_indicator(
				__("Settled By: {0}", [__(frm.doc.settlement, null, "Facility Repayment")]),
				frm.doc.settlement === "PDC Cheque" ? "purple" : "blue"
			);
		}
		// "Profit" wording of an Islamic facility (server terms, treasury.bank_facilities.lenders)
		const terms = (frm.doc.__onload || {}).terms;
		if (terms) {
			frm.set_df_property("interest_paid", "label", terms.interest_paid);
		}
	},
});

// ------------------------------------------------------------------------------------ low balance guard
// The override is offered to Treasury Managers only (the server checks the role and logs the override).
frappe.ui.form.on("Facility Repayment", {
	refresh(frm) {
		frm.toggle_display(
			"allow_low_balance",
			frm.doc.docstatus === 0 && frappe.user.has_role(["Treasury Manager", "System Manager"])
		);
		if (frm.doc.docstatus === 0 && frm.doc.allow_low_balance) {
			frp_low_balance_ticked(frm);
		}
		// design 7e P17: the latest override (Treasury Alert Log, user and time only)
		const override = frm.doc.__onload && frm.doc.__onload.low_balance_override;
		if (override) {
			frm.dashboard.set_headline(
				`<div>${frappe.utils.escape_html(
					__("Submitted with a low-balance override by {0} on {1}.", [
						frappe.user.full_name(override.user) || override.user,
						frappe.datetime.str_to_user(override.creation),
					])
				)}</div>`,
				"orange"
			);
		}
	},

	allow_low_balance(frm) {
		frp_low_balance_ticked(frm);
	},
});

function frp_low_balance_ticked(frm) {
	// design 7e §4.4 / P16: a ticked override is never hidden in a collapsed section
	if (!frm.doc.allow_low_balance || frm.doc.docstatus !== 0) {
		return;
	}
	const field = frm.get_field("allow_low_balance");
	const section = field && field.section;
	section && section.is_collapsed && section.is_collapsed() && section.collapse(false);
	if (frm._frp_low_balance_alerted !== frm.doc.name) {
		frm._frp_low_balance_alerted = frm.doc.name;
		frappe.show_alert({
			message: frappe.utils.escape_html(
				__(
					"The block is overridden only if this entry takes a bank below its minimum. Each override is logged."
				)
			),
			indicator: "orange",
		});
	}
}

// Design 7e §4.3: one dialog for the LowBalanceError of every guarded action and submit form, registered once by
// whichever guarded form loads first (the same code is in each guarded form script: there are no bundles).
// Frappe shows no message of its own when a handler exists, so the dialog renders the server message (built
// and escaped by treasury.balance_alerts.guard). A manager gets one red button: the action helpers set
// `treasury.low_balance.retry` just before their call; a draft with `allow_low_balance` is submitted again.
frappe.provide("treasury.low_balance");
if (!treasury.low_balance.show) {
	treasury.low_balance.show = function (r) {
		const retry = treasury.low_balance.retry;
		treasury.low_balance.retry = null;
		let html = "";
		try {
			html = JSON.parse(r._server_messages || "[]")
				.map((m) => JSON.parse(m).message || "")
				.join("");
		} catch (e) {
			html = "";
		}
		const frm = window.cur_frm;
		const can_override = frappe.user.has_role(["Treasury Manager", "System Manager"]);
		const submit_form = !retry && frm && frm.doc && frm.doc.docstatus === 0 && frm.fields_dict.allow_low_balance;
		const dialog = new frappe.ui.Dialog({
			title: __("Low Projected Balance"),
			indicator: "red",
			fields: [{ fieldname: "message", fieldtype: "HTML", options: html }],
		});
		if (can_override && (retry || submit_form)) {
			dialog.set_primary_action(retry ? __("Override and Continue") : __("Override and Submit"), () => {
				dialog.hide();
				// the red button is the confirmation: no second "Permanently Submit?" prompt
				retry ? retry() : frm.set_value("allow_low_balance", 1).then(() => frm.save("Submit"));
			});
			dialog.get_primary_btn().removeClass("btn-primary").addClass("btn-danger");
			dialog.set_secondary_action(() => dialog.hide());
			dialog.set_secondary_action_label(__("Cancel"));
		} else {
			dialog.set_primary_action(__("Close"), () => dialog.hide());
		}
		dialog.show();
	};
	frappe.request.on_error("LowBalanceError", (r) => treasury.low_balance.show(r));
}

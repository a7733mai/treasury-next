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
		frm._frp_context = null;
		frp_load_context(frm, false);
	},

	penalty(frm) {
		frp_penalty_note(frm);
	},

	refresh(frm) {
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		frp_load_context(frm, true);
		if (!frm.is_new()) {
			frp_settlement_indicator(frm);
			frp_set_headline(frm);
		}
	},
});

// ------------------------------------------------------------------------------------ design 7g §4
// Facility Repayment scripts share the global scope with the other forms: names are prefixed `frp_`.

function frp_load_context(frm, on_refresh) {
	// the drawdown's repayment mode and the facility's Islamic wording / late payment mode (a new repayment has
	// no __onload yet): read once per drawdown. `on_refresh`: the dashboard was just reset (the Islamic tag).
	const doc = frm.doc;
	if (!doc.drawdown) {
		frp_apply_context(frm, {}, on_refresh);
		return;
	}
	if (frm._frp_context && frm._frp_context.drawdown === doc.drawdown) {
		frp_apply_context(frm, frm._frp_context, on_refresh);
		return;
	}
	frappe.db
		.get_value("Facility Drawdown", doc.drawdown, ["repayment_mode", "facility", "is_islamic", "limit_type"])
		.then((r) => {
			const drawdown = r.message || {};
			const facility = drawdown.facility
				? frappe.db.get_value("Bank Facility", drawdown.facility, "late_payment_mode")
				: Promise.resolve({});
			return facility.then((f) => {
				frm._frp_context = {
					drawdown: doc.drawdown,
					repayment_mode: drawdown.repayment_mode,
					is_islamic: cint(drawdown.is_islamic),
					limit_type: drawdown.limit_type,
					late_payment_mode: (f && f.message && f.message.late_payment_mode) || "Paid to Lender",
				};
				frp_apply_context(frm, frm._frp_context, on_refresh);
			});
		});
}

function frp_apply_context(frm, context, on_refresh) {
	// design 7g §1: the facility's word on the amounts, the accounting section and the allocation grid (P1 tag)
	const terms = treasury.facility.terms(context.is_islamic, context.limit_type);
	const known = !!context.drawdown;
	frm.set_df_property("interest_paid", "label", known ? terms.interest_paid : "Interest / Profit Paid");
	frm.set_df_property("accrued_interest_cleared", "label", known ? terms.accrued_interest_cleared : "Accrued Interest Cleared");
	frm.set_df_property("interest_expensed", "label", known ? terms.interest_expensed : "Interest Expensed");
	frm.set_df_property("penalty", "label", known ? terms.penalty : "Penalty");
	const grid = frm.fields_dict.allocations && frm.fields_dict.allocations.grid;
	if (grid) {
		grid.update_docfield_property("interest", "label", known ? terms.interest : "Interest / Profit");
		frm.refresh_field("allocations");
	}
	if (on_refresh && !frm.is_new() && context.is_islamic) {
		frm.dashboard.add_indicator(frappe.utils.escape_html(__("Islamic")), "cyan");
	}
	frp_penalty_note(frm);
	frp_draft_intro(frm);
}

function frp_penalty_note(frm) {
	// design 7g §4.2: where an Islamic facility's late payment charge goes (mirrors set_penalty_treatment)
	const context = frm._frp_context || {};
	let note = "";
	if (frm.doc.docstatus === 0 && context.is_islamic && flt(frm.doc.penalty)) {
		note =
			context.late_payment_mode === "Payable to Charity"
				? __("Not paid now: booked as payable to charity")
				: __("Paid to the lender with this installment");
	}
	frm.set_df_property("penalty", "description", frappe.utils.escape_html(note));
}

function frp_draft_intro(frm) {
	if (frm.doc.docstatus !== 0) {
		return;
	}
	const context = frm._frp_context || {};
	if (context.repayment_mode === "PDC to Lender") {
		frm.set_intro(__("Installments with a cheque are paid by the cheque. Oldest First skips them."), "orange");
	} else if (frm.doc.prepayment) {
		frm.set_intro(
			__(
				"The principal beyond the installments due up to the repayment date reduces the later installments; the tenor is kept."
			),
			"blue"
		);
	} else {
		frm.set_intro();
	}
}

function frp_settlement_indicator(frm) {
	// design 7g §4.2: who settled it — the bank (a user), the bank's auto debit or an installment cheque
	const esc = frappe.utils.escape_html;
	const info = (frm.doc.__onload && frm.doc.__onload.settlement_info) || {};
	const indicator = {
		Bank: [__("Bank Payment"), "blue"],
		"Auto Debit": [__("Auto Debit", null, "Facility Repayment"), "cyan"],
		"PDC Cheque": [__("Cheque {0}", [info.cheque_no || frm.doc.pdc_cheque || ""]), "purple"],
	}[frm.doc.settlement || "Bank"];
	if (indicator) {
		frm.dashboard.add_indicator(esc(indicator[0]), indicator[1]);
	}
}

function frp_link(doctype, name, text, bold) {
	const esc = frappe.utils.escape_html;
	const label = `<bdi>${esc(text || name)}</bdi>`;
	return `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}">${bold ? `<b>${label}</b>` : label}</a>`;
}

function frp_set_headline(frm) {
	// design 7g §4.2: how the repayment was settled, with its voucher (the filing key) in bold; the 7e
	// low-balance override line keeps priority
	const doc = frm.doc;
	if (doc.docstatus !== 1) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const info = (doc.__onload && doc.__onload.settlement_info) || {};
	const override = doc.__onload && doc.__onload.low_balance_override;
	const voucher = doc.payment_entry
		? frp_link("Payment Entry", doc.payment_entry, null, true)
		: doc.journal_entry
			? frp_link("Journal Entry", doc.journal_entry, null, true)
			: "";
	if (!override) {
		if (doc.settlement === "PDC Cheque" && doc.pdc_cheque) {
			frm.dashboard.set_headline(
				`<div>${__("Paid by installment cheque {0} ({1}) · {2}.", [
					esc(info.cheque_no || ""),
					frp_link("PDC Cheque", doc.pdc_cheque),
					voucher,
				])} ${esc(__("To undo, bounce, replace or cancel the cheque."))}</div>`,
				"blue"
			);
		} else if (doc.settlement === "Auto Debit" && doc.bank_transaction) {
			frm.dashboard.set_headline(
				`<div>${__("Auto debit matched with bank statement line {0} on {1} · {2}.", [
					frp_link("Bank Transaction", doc.bank_transaction),
					esc(info.transaction_date ? frappe.datetime.str_to_user(info.transaction_date) : ""),
					voucher,
				])}</div>`,
				"blue"
			);
		} else if (doc.settlement === "Auto Debit") {
			frm.dashboard.set_headline(
				`<div>${__("Posted automatically on the due date · {0} · not yet matched with the bank statement.", [
					voucher,
				])}</div>`,
				"yellow"
			);
		}
	}
	if (doc.charity_journal_entry) {
		frm.dashboard.set_headline(
			`<div>${__("Late payment charge {0} booked as payable to charity ({1}).", [
				esc(format_currency(doc.penalty, doc.currency)),
				frp_link("Journal Entry", doc.charity_journal_entry, null, true),
			])}</div>`,
			"orange"
		);
	}
}

// Design 7g §1: the Interest / Profit wording of a facility, shared by the facility forms (the same helper is
// in each of them: there are no bundles; defined once by whichever loads first). The strings are those of
// treasury.bank_facilities.lenders.terms().
frappe.provide("treasury.facility");
if (!treasury.facility.terms) {
	treasury.facility.terms = function (is_islamic, limit_type) {
		const islamic = !!cint(is_islamic);
		const rental = islamic && limit_type === "Ijarah";
		return {
			interest: islamic ? __("Profit") : __("Interest"),
			interest_rate: islamic ? __("Profit Rate (% p.a.)") : __("Interest Rate (% p.a.)"),
			total_interest: islamic ? __("Total Profit") : __("Total Interest"),
			interest_paid: islamic ? __("Profit Paid") : __("Interest Paid"),
			paid_interest: islamic ? __("Paid Profit") : __("Paid Interest"),
			accrued_interest: islamic ? __("Accrued Profit") : __("Accrued Interest"),
			accrued_interest_account: islamic ? __("Accrued Profit Account") : __("Accrued Interest Account"),
			interest_expense_account: islamic ? __("Profit Expense Account") : __("Interest Expense Account"),
			accrued_interest_cleared: islamic ? __("Accrued Profit Cleared") : __("Accrued Interest Cleared"),
			interest_expensed: islamic ? __("Profit Expensed") : __("Interest Expensed"),
			installment: rental ? __("Rental") : __("Installment"),
			total: rental ? __("Rental") : __("Total", null, "Treasury"),
			penalty: islamic ? __("Late Payment Charge") : __("Penalty"),
		};
	};
}

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

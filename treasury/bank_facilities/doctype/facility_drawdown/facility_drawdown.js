// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities (schedule.py, accounting.py, facility_cheques.py, istisna.py,
// auto_debit.py); the form only calls them and mirrors them for display (design 7g §3).
// Form scripts share the global scope: names are prefixed `DD_` / `dd_`.
const DD_SCHEDULE = "Facility Repayment Schedule";
const DD_STAGE = "Facility Disbursement Stage";
const DD_PDC = "PDC to Lender";
const DD_AUTO_DEBIT = "Auto Debit";
const DD_ON_STATEMENT = "On Bank Statement Match";
// mirrors treasury.bank_facilities.utilisation.LOAN_TYPES
const DD_LOAN_TYPES = [
	"Short Term Loan",
	"Long Term Loan",
	"Murabaha",
	"Tawarruq",
	"Ijarah",
	"Istisna'a",
	"Trust Receipt",
	"Other",
];
const DD_REPAYMENT_MODES = ["Manual", DD_AUTO_DEBIT, DD_PDC];
// design 7g §3.3: the installment states (the stored status plus the derived ones) and the cheque dots
const DD_STATE_COLORS = {
	Paid: "green",
	"Partially Paid": "orange",
	"Cheque Issued": "blue",
	Pending: "gray",
	"Awaiting Statement": "yellow",
	"Posting Today": "light-blue",
	"Cheque Due": "orange",
	Overdue: "red",
};
const DD_CHEQUE_COLORS = {
	Created: "gray",
	Delivered: "blue",
	Cleared: "green",
	Bounced: "red",
	Replaced: "darkgrey",
	Cancelled: "darkgrey",
};

frappe.ui.form.on("Facility Drawdown", {
	setup(frm) {
		frm.set_query("facility", () => ({ filters: { status: "Active" } }));
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("fees_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
		// mirrors FacilityDrawdown.validate_disbursement
		frm.set_query("disbursement_account", () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
				account_currency: frm.doc.currency,
				account_type: ["not in", ["Payable", "Receivable"]],
				name: ["!=", frm.doc.liability_account || ""],
			},
		}));
	},

	refresh(frm) {
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		dd_set_formatters(frm);
		dd_set_labels(frm);
		dd_set_options(frm);
		dd_set_stages_grid(frm);
		dd_mode_changed_alert(frm);
		if (frm.doc.docstatus === 0) {
			if (!frm.is_new()) {
				frm.add_custom_button(__("Generate Schedule"), () => frm.events.generate_schedule(frm));
			}
			if (frm.doc.limit_type === "Istisna'a") {
				frm.set_intro(
					__("Submitting books only the upfront fees. Each stage is posted when you use Disburse Stage."),
					"blue"
				);
			}
		}
		if (frm.doc.docstatus === 1) {
			dd_add_indicators(frm);
			dd_set_headline(frm);
			dd_set_progress(frm);
			dd_set_mode_intro(frm);
			if (
				frm.doc.repayment_status === "Open" &&
				frappe.model.can_create("Facility Repayment") &&
				!(frm.doc.repayment_mode === DD_PDC && !dd_rows_without_cheque(frm).length)
			) {
				// hidden when every unpaid installment is paid by its cheque (repayment I1)
				frm.add_custom_button(
					__("Facility Repayment"),
					() => frappe.new_doc("Facility Repayment", { drawdown: frm.doc.name }),
					__("Create")
				);
			}
			frm.add_custom_button(__("Refresh Status"), () => {
				frm.call({ doc: frm.doc, method: "refresh_status", freeze: true }).then(() => frm.reload_doc());
			});
			frm.events.add_facility_actions(frm);
		}
	},

	add_facility_actions(frm) {
		if (frm.is_dirty()) {
			return;
		}
		const info = dd_info(frm);
		if (
			frm.doc.repayment_mode === DD_PDC &&
			frm.doc.repayment_status === "Open" &&
			dd_rows_without_cheque(frm).length &&
			frappe.user.has_role(["Treasury Manager", "Treasury User"]) &&
			frappe.model.can_create("Cheque Batch")
		) {
			frm.add_custom_button(__("Generate Installment Cheques"), () => dd_installment_cheques_dialog(frm))
				.removeClass("btn-default")
				.addClass("btn-primary");
		}
		if (
			frm.doc.limit_type === "Istisna'a" &&
			dd_pending_stages(frm).length &&
			frappe.user.has_role("Treasury Manager")
		) {
			frm.add_custom_button(__("Disburse Stage"), () => dd_disburse_stage_dialog(frm))
				.removeClass("btn-default")
				.addClass("btn-primary");
		}
		if (info.has_cheques && frappe.model.can_read("PDC Cheque")) {
			frm.add_custom_button(
				__("Installment Cheques"),
				() => frappe.set_route("List", "PDC Cheque", { facility_drawdown: frm.doc.name }),
				__("View")
			);
		}
	},

	generate_schedule(frm) {
		const run = () =>
			frm.call({ doc: frm.doc, method: "generate_schedule", doc: frm.doc, freeze: true }).then((r) => {
				if (r.message) {
					frappe.model.sync(r.message);
					frm.refresh();
					frm.dirty();
					frm.save();
				}
			});
		if (frm.is_dirty()) {
			frm.save().then(run);
		} else {
			run();
		}
	},

	facility(frm) {
		frm.set_value("bank_account", null);
		frm.set_value("interest_rate", 0);
		frm.clear_table("repayment_schedule");
		frm.refresh_field("repayment_schedule");
		frm._dd_facility = null;
		dd_set_options(frm);
	},

	limit_type(frm) {
		frm.set_value("interest_rate", 0);
		frm.set_value("rate_type", null);
		frm.set_value("day_count", null);
		dd_set_labels(frm);
	},

	is_islamic(frm) {
		dd_set_labels(frm);
	},

	currency(frm) {
		dd_set_options(frm);
	},

	repayment_mode(frm) {
		// design 7g §3.6: after a change on a submitted drawdown is saved, say what comes next
		if (frm.doc.docstatus === 1 && frm._dd_mode_from === undefined) {
			frm._dd_mode_from = frm._dd_saved_mode;
		}
	},
});

frappe.ui.form.on(DD_STAGE, {
	amount(frm) {
		dd_set_stages_total(frm);
	},
	disbursement_stages_add(frm) {
		dd_set_stages_total(frm);
	},
	disbursement_stages_remove(frm) {
		dd_set_stages_total(frm);
	},
});

// A term change makes the schedule stale: clear it, the server rebuilds an empty schedule on save (and refuses
// a changed term with an unchanged schedule).
const TERM_FIELDS = [
	"amount",
	"drawdown_date",
	"interest_rate",
	"day_count",
	"profit_amount",
	"tenor_months",
	"repayment_frequency",
	"repayment_method",
	"first_repayment_date",
	"grace_months",
];
frappe.ui.form.on(
	"Facility Drawdown",
	Object.fromEntries(
		TERM_FIELDS.map((fieldname) => [
			fieldname,
			(frm) => {
				if (frm.doc.docstatus === 0 && (frm.doc.repayment_schedule || []).length) {
					frm.clear_table("repayment_schedule");
					frm.refresh_field("repayment_schedule");
				}
				if (fieldname === "amount") {
					dd_set_stages_total(frm);
				}
			},
		])
	)
);

// ------------------------------------------------------------------------------------------ installments

function dd_info(frm) {
	// __onload.installment_info (design 7g P11 / P14): the company's Auto Debit Posting and the cheques
	return (frm.doc.__onload && frm.doc.__onload.installment_info) || {};
}

function dd_cheque(frm, row) {
	return (dd_info(frm).cheques || {})[row.name] || null;
}

function dd_live_cheque(frm, row) {
	const cheque = dd_cheque(frm, row);
	const live = dd_info(frm).live_statuses || ["Created", "Delivered"];
	return cheque && live.includes(cheque.operational_status) ? cheque : null;
}

function dd_unpaid(row) {
	return flt(flt(row.principal) + flt(row.interest) - flt(row.paid_principal) - flt(row.paid_interest), 2);
}

function dd_open_rows(frm) {
	return (frm.doc.repayment_schedule || [])
		.filter((row) => !cint(row.is_prepayment) && dd_unpaid(row) > 0)
		.sort((a, b) => (a.due_date < b.due_date ? -1 : a.due_date > b.due_date ? 1 : a.idx - b.idx));
}

function dd_rows_without_cheque(frm) {
	// mirrors facility_cheques._open_rows: the installments Generate Installment Cheques would take
	return dd_open_rows(frm).filter((row) => !dd_live_cheque(frm, row));
}

function dd_state(frm, row) {
	// design 7g §3.3 (mirrors report_utils.display_status): [state, colour]
	const status = row.status || "Pending";
	if (status === "Paid" || dd_unpaid(row) <= 0) {
		return ["Paid", DD_STATE_COLORS.Paid];
	}
	const today = frappe.datetime.get_today();
	const due = row.due_date || "";
	if (frm.doc.docstatus === 1 && dd_live_cheque(frm, row)) {
		return due < today ? ["Cheque Due", DD_STATE_COLORS["Cheque Due"]] : ["Cheque Issued", DD_STATE_COLORS["Cheque Issued"]];
	}
	if (frm.doc.docstatus === 1 && frm.doc.repayment_mode === DD_AUTO_DEBIT && due && due <= today) {
		if (dd_info(frm).auto_debit_posting === DD_ON_STATEMENT) {
			return ["Awaiting Statement", DD_STATE_COLORS["Awaiting Statement"]];
		}
		if (due === today) {
			return ["Posting Today", DD_STATE_COLORS["Posting Today"]];
		}
	}
	return [status, DD_STATE_COLORS[status] || "gray"];
}

function dd_state_label(state) {
	// the derived states are ours; the stored ones are the grid's Select options
	return {
		"Cheque Issued": __("Cheque Issued", null, DD_SCHEDULE),
		"Cheque Due": __("Cheque Due", null, DD_SCHEDULE),
		"Awaiting Statement": __("Awaiting Statement", null, DD_SCHEDULE),
		"Posting Today": __("Posting Today", null, DD_SCHEDULE),
	}[state] || __(state, null, DD_SCHEDULE);
}

function dd_link(doctype, name, text, bold) {
	const esc = frappe.utils.escape_html;
	const label = `<bdi>${esc(text || name)}</bdi>`;
	return `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}">${bold ? `<b>${label}</b>` : label}</a>`;
}

function dd_money(frm, value) {
	return format_currency(value, frm.doc.currency);
}

function dd_date(value) {
	return value ? frappe.datetime.str_to_user(value) : "";
}

// ------------------------------------------------------------------------------------------ display

function dd_set_formatters(frm) {
	// design 7g §3.3 / §3.9: pills, cheque dots and the payment vouchers (the filing key) in bold
	const esc = frappe.utils.escape_html;
	const is_row = (row, doctype) => row && row.doctype === doctype;
	const schedule = {
		due_date: (value, df, options, row) => {
			const text = esc(dd_date(value));
			if (!is_row(row, DD_SCHEDULE)) {
				return text;
			}
			const state = dd_state(frm, row)[0];
			if (state === "Overdue") {
				return `<span style="color: var(--red-600)">${text}</span>`;
			}
			if (state === "Awaiting Statement") {
				return `<span style="color: var(--yellow-600)">${text}</span>`;
			}
			return text;
		},
		total: (value) => `<b>${esc(dd_money(frm, value))}</b>`,
		status: (value, df, options, row) => {
			if (!is_row(row, DD_SCHEDULE)) {
				return esc(value ? __(value, null, DD_SCHEDULE) : "");
			}
			const [state, color] = dd_state(frm, row);
			return `<span class="indicator-pill ${color}">${esc(dd_state_label(state))}</span>`;
		},
		pdc_cheque: (value, df, options, row) => {
			if (!value) {
				return "";
			}
			const cheque = is_row(row, DD_SCHEDULE) ? dd_cheque(frm, row) : null;
			if (!cheque) {
				return dd_link("PDC Cheque", value);
			}
			const color = DD_CHEQUE_COLORS[cheque.operational_status] || "orange";
			const status = esc(__(cheque.operational_status || "", null, "PDC Cheque"));
			return `${dd_link("PDC Cheque", value, cheque.cheque_no || value)}
				<span class="indicator ${color}" title="${status}"></span>`;
		},
		payment_journal: (value, df, options, row) => {
			if (!value) {
				return "";
			}
			const doctype = (row && row.payment_voucher_type) || "Journal Entry";
			return dd_link(doctype, value, value, true);
		},
	};
	const stages = {
		status: (value) => {
			if (!value) {
				return "";
			}
			const color = value === "Disbursed" ? "green" : "gray";
			return `<span class="indicator-pill ${color}">${esc(__(value, null, DD_STAGE))}</span>`;
		},
		voucher_no: (value, df, options, row) =>
			value ? dd_link((row && row.voucher_type) || "Journal Entry", value, value, true) : "",
	};
	for (const [doctype, formatters] of [
		[DD_SCHEDULE, schedule],
		[DD_STAGE, stages],
	]) {
		Object.entries(formatters).forEach(([fieldname, formatter]) => {
			for (const name of [null, frm.doc.name]) {
				const df = frappe.meta.get_docfield(doctype, fieldname, name);
				if (df) {
					df.formatter = formatter;
				}
			}
		});
	}
	frm.refresh_field("repayment_schedule");
	frm.refresh_field("disbursement_stages");
}

function dd_set_labels(frm) {
	// design 7g §1 / §3.2: the facility's word (Interest / Profit, Rental for an Islamic Ijarah); the neutral
	// JSON labels until the facility is chosen
	const known = !!frm.doc.facility;
	const terms = treasury.facility.terms(frm.doc.is_islamic, frm.doc.limit_type);
	const grid = frm.fields_dict.repayment_schedule && frm.fields_dict.repayment_schedule.grid;
	frm.set_df_property("interest_rate", "label", known ? terms.interest_rate : "Interest / Profit Rate (% p.a.)");
	frm.set_df_property("total_interest", "label", known ? terms.total_interest : "Total Interest / Profit");
	if (grid) {
		grid.update_docfield_property("interest", "label", known ? terms.interest : "Interest / Profit");
		grid.update_docfield_property("paid_interest", "label", known ? terms.paid_interest : "Paid Interest / Profit");
		grid.update_docfield_property("total", "label", known ? terms.total : "Total");
	}
	const ijarah = frm.doc.limit_type === "Ijarah";
	const amount = {
		Ijarah: __("Financed Amount (Asset Cost)"),
		"Istisna'a": __("Contract Amount"),
	}[frm.doc.limit_type];
	frm.set_df_property("amount", "label", amount || "Amount");
	frm.set_df_property("repayment_schedule", "label", ijarah ? __("Rental Schedule") : "Repayment Schedule");
	frm.set_df_property("schedule_section", "label", ijarah ? __("Rental Schedule") : "Repayment Schedule");
	frm.refresh_field("repayment_schedule");
}

function dd_set_options(frm) {
	// design 7g §3.1: the facility's own loan sub-limits (P9: the only one is chosen); no cheques to the lender
	// in a foreign currency (mirrors validate_repayment_mode)
	if (frm.doc.docstatus !== 0) {
		return;
	}
	if (frm.doc.company) {
		frappe.db.get_value("Company", frm.doc.company, "default_currency").then((r) => {
			const company_currency = r.message && r.message.default_currency;
			const foreign = frm.doc.currency && company_currency && frm.doc.currency !== company_currency;
			const modes = DD_REPAYMENT_MODES.filter((mode) => !(foreign && mode === DD_PDC));
			frm.set_df_property("repayment_mode", "options", modes.join("\n"));
			if (foreign && frm.doc.repayment_mode === DD_PDC) {
				frm.set_value("repayment_mode", "Manual");
			}
		});
	}
	if (!frm.doc.facility) {
		frm.set_df_property("limit_type", "options", DD_LOAN_TYPES.join("\n"));
		return;
	}
	const apply = (facility) => {
		const types = DD_LOAN_TYPES.filter((type) => (facility.limits || []).some((row) => row.limit_type === type));
		frm.set_df_property("limit_type", "options", (types.length ? types : DD_LOAN_TYPES).join("\n"));
		if (types.length === 1 && frm.doc.limit_type !== types[0]) {
			frm.set_value("limit_type", types[0]);
		}
	};
	if (frm._dd_facility && frm._dd_facility.name === frm.doc.facility) {
		apply(frm._dd_facility);
		return;
	}
	frappe.db.get_doc("Bank Facility", frm.doc.facility).then((facility) => {
		frm._dd_facility = facility;
		apply(facility);
	});
}

function dd_set_stages_grid(frm) {
	// design 7g §3.9: the stages change through Disburse Stage only once submitted (the field is
	// allow_on_submit for the stage vouchers): no editing, no adding
	const field = frm.fields_dict.disbursement_stages;
	if (!field || !field.grid) {
		return;
	}
	const submitted = frm.doc.docstatus !== 0;
	frm.set_df_property("disbursement_stages", "read_only", submitted ? 1 : 0);
	field.grid.cannot_add_rows = submitted;
	dd_set_stages_total(frm);
}

function dd_set_stages_total(frm) {
	// the live total under the stages of a draft: "Stages {0} of {1}", green when they add up
	if (frm.doc.limit_type !== "Istisna'a") {
		return;
	}
	const esc = frappe.utils.escape_html;
	let description = esc(
		__(
			"Add one row per disbursement stage; the amounts must add up to the contract amount. Each stage is posted with Disburse Stage."
		)
	);
	if (frm.doc.docstatus === 0) {
		const total = flt(
			(frm.doc.disbursement_stages || []).reduce((sum, row) => sum + flt(row.amount), 0),
			2
		);
		const color = total === flt(frm.doc.amount, 2) ? "var(--green-600)" : "var(--red-600)";
		description += `<br><span style="color: ${color}">${esc(
			__("Stages {0} of {1}", [dd_money(frm, total), dd_money(frm, frm.doc.amount)])
		)}</span>`;
	}
	frm.set_df_property("disbursement_stages", "description", description);
}

function dd_add_indicators(frm) {
	// design 7g §3.4
	const esc = frappe.utils.escape_html;
	const doc = frm.doc;
	const outstanding = (doc.__onload || {}).outstanding_principal;
	if (outstanding !== undefined) {
		frm.dashboard.add_indicator(
			esc(__("Outstanding Principal: {0}", [format_currency(outstanding, doc.currency)])),
			outstanding > 0 ? "orange" : "green"
		);
	}
	if (doc.repayment_mode === DD_AUTO_DEBIT) {
		const statement = dd_info(frm).auto_debit_posting === DD_ON_STATEMENT;
		frm.dashboard.add_indicator(
			esc(statement ? __("Auto Debit · Statement Match") : __("Auto Debit · On Due Date")),
			"cyan"
		);
	} else if (doc.repayment_mode === DD_PDC) {
		const rows = (doc.repayment_schedule || []).filter((row) => !cint(row.is_prepayment));
		const paid_by_cheque = rows.filter((row) => {
			const cheque = dd_cheque(frm, row);
			return dd_unpaid(row) <= 0 && cheque && !["Cancelled", "Replaced", "Bounced"].includes(cheque.operational_status);
		}).length;
		const open = dd_open_rows(frm);
		const covered = open.filter((row) => dd_live_cheque(frm, row)).length + paid_by_cheque;
		const total = open.length + paid_by_cheque;
		frm.dashboard.add_indicator(
			esc(__("PDC to Lender · {0} of {1} cheques", [covered, total])),
			covered === total ? "blue" : "orange"
		);
	} else {
		frm.dashboard.add_indicator(esc(__("Manual Repayment")), "gray");
	}
	if (cint(doc.is_islamic)) {
		frm.dashboard.add_indicator(esc(__("Islamic")), "cyan");
	}
	if (doc.limit_type === "Trust Receipt") {
		frm.dashboard.add_indicator(esc(__("Trust Receipt")), "purple");
	}
	if (doc.limit_type === "Ijarah" && cint(doc.ownership_transfer)) {
		frm.dashboard.add_indicator(esc(__("Ijarah Muntahia Bittamleek")), "purple");
	}
}

function dd_set_headline(frm) {
	// design 7g §3.4: one line, the first matching condition
	const esc = frappe.utils.escape_html;
	const doc = frm.doc;
	const show = (html, color) => frm.dashboard.set_headline(`<div>${html}</div>`, color);
	if (doc.limit_type === "Trust Receipt" && doc.letter_of_credit) {
		const date = (doc.__onload || {}).letter_of_credit_settlement;
		show(
			__("Trust Receipt financing the settlement of {0} on {1}.", [
				dd_link("Letter of Credit", doc.letter_of_credit),
				esc(dd_date(date)),
			]),
			"blue"
		);
		return;
	}
	const pending = dd_pending_stages(frm);
	if (doc.limit_type === "Istisna'a" && pending.length) {
		show(
			esc(
				__("{0} of {1} disbursed · next stage {2} on {3}.", [
					dd_money(frm, doc.disbursed_amount),
					dd_money(frm, doc.amount),
					pending[0].idx,
					dd_date(pending[0].stage_date),
				])
			),
			"blue"
		);
		return;
	}
	const next = dd_open_rows(frm)[0];
	if (next) {
		const word = doc.limit_type === "Ijarah" && cint(doc.is_islamic) ? __("rental") : __("installment");
		const [state] = dd_state(frm, next);
		let color = state === "Overdue" || state === "Cheque Due" ? "orange" : "blue";
		const parts = [
			esc(__("Next {0} #{1} · {2} due {3}", [word, next.installment_no, dd_money(frm, dd_unpaid(next)), dd_date(next.due_date)])),
		];
		if (doc.repayment_mode === DD_PDC) {
			const cheque = dd_live_cheque(frm, next);
			if (cheque) {
				parts.push(
					__("cheque {0} ({1})", [
						dd_link("PDC Cheque", cheque.name, cheque.cheque_no || cheque.name),
						esc(__(cheque.operational_status, null, "PDC Cheque")),
					])
				);
			} else {
				parts.push(esc(__("no cheque yet")));
				color = "orange";
			}
		} else if (doc.repayment_mode === DD_AUTO_DEBIT) {
			if (dd_info(frm).auto_debit_posting === DD_ON_STATEMENT) {
				if (state === "Awaiting Statement") {
					parts.push(esc(__("awaiting the bank statement")));
					color = "yellow";
				}
			} else {
				parts.push(esc(__("posted automatically on the due date")));
			}
		}
		show(parts.join(" · "), color);
		return;
	}
	if (doc.repayment_status === "Repaid") {
		const last = (doc.repayment_schedule || [])
			.filter((row) => row.payment_journal)
			.sort((a, b) => (a.due_date < b.due_date ? -1 : 1))
			.pop();
		const voucher = last ? dd_link(last.payment_voucher_type || "Journal Entry", last.payment_journal, null, true) : "—";
		show(__("Fully repaid · last voucher {0}.", [voucher]), "green");
	}
}

function dd_set_progress(frm) {
	// design 7g P13: the Istisna'a disbursement progress
	const doc = frm.doc;
	if (doc.limit_type !== "Istisna'a" || !flt(doc.amount)) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const disbursed = flt(doc.disbursed_amount);
	const percent = Math.min(100, Math.max(0, (disbursed * 100) / flt(doc.amount)));
	const title = esc(__("Disbursed", null, DD_STAGE));
	frm.dashboard.add_progress(
		title,
		[{ title, width: `${percent}%`, progress_class: "progress-bar-success" }],
		esc(
			__("{0} disbursed · {1} pending", [
				dd_money(frm, disbursed),
				dd_money(frm, flt(doc.amount) - disbursed),
			])
		)
	);
}

function dd_set_mode_intro(frm) {
	// design 7g §3.5: how the installments of this drawdown are paid
	const esc = frappe.utils.escape_html;
	const doc = frm.doc;
	if (doc.repayment_status !== "Open") {
		return;
	}
	if (doc.repayment_mode === DD_AUTO_DEBIT) {
		if (dd_info(frm).auto_debit_posting === DD_ON_STATEMENT) {
			frm.set_intro(
				`${esc(
					__(
						"The bank debits each installment automatically. Import the bank statement and match it in PDC Bank Clearing to post the repayment."
					)
				)} <a class="dd-open-clearing">${esc(__("Open PDC Bank Clearing"))} →</a>`,
				"blue"
			);
			frm.$intro_message &&
				frm.$intro_message.find(".dd-open-clearing").on("click", () => {
					frappe.route_options = { company: doc.company, bank_account: doc.bank_account };
					frappe.set_route("Form", "PDC Bank Clearing");
				});
		} else {
			frm.set_intro(
				esc(
					__(
						"The bank debits each installment automatically. Treasury posts it on its due date as a repayment from {0}.",
						[doc.bank_account]
					)
				),
				"blue"
			);
		}
		return;
	}
	if (doc.repayment_mode !== DD_PDC) {
		return;
	}
	const missing = dd_rows_without_cheque(frm).length;
	if (!missing) {
		frm.set_intro(
			esc(
				__(
					"Each installment is paid by a post-dated cheque to {0}. The cheque is posted on its date as the repayment; nothing is posted at delivery.",
					[doc.lender_name || ""]
				)
			),
			"blue"
		);
		return;
	}
	const can_generate =
		frappe.user.has_role(["Treasury Manager", "Treasury User"]) && frappe.model.can_create("Cheque Batch");
	frm.set_intro(
		`${esc(__("{0} installment(s) have no cheque yet.", [missing]))}${
			can_generate ? ` <a class="dd-generate-cheques">${esc(__("Generate Installment Cheques"))} →</a>` : ""
		}`,
		"orange"
	);
	frm.$intro_message &&
		frm.$intro_message.find(".dd-generate-cheques").on("click", () => dd_installment_cheques_dialog(frm));
}

function dd_mode_changed_alert(frm) {
	// design 7g §3.6: after the Repayment Mode of a submitted drawdown was updated to PDC to Lender
	if (frm.is_dirty()) {
		return;
	}
	const from = frm._dd_mode_from;
	frm._dd_mode_from = undefined;
	frm._dd_saved_mode = frm.doc.repayment_mode;
	if (from !== undefined && from !== DD_PDC && frm.doc.repayment_mode === DD_PDC) {
		frappe.show_alert({
			message: frappe.utils.escape_html(__("Repayment mode changed. Generate the installment cheques now.")),
			indicator: "green",
		});
	}
}

// ------------------------------------------------------------------------------------------ installment cheques

function dd_installment_cheques_dialog(frm) {
	// design 7g §3.7: a client preview of the rows the server will return (facility_cheques.make_installment_batch)
	const esc = frappe.utils.escape_html;
	const doc = frm.doc;
	const terms = treasury.facility.terms(doc.is_islamic, doc.limit_type);
	const info = dd_info(frm);
	const rows = dd_rows_without_cheque(frm);
	const book = info.cheque_book;
	const fields = [];
	const line = (label, value) =>
		`<div class="row" style="margin-bottom: var(--margin-xs)">
			<div class="col-xs-3 text-muted">${esc(label)}</div><div class="col-xs-9">${value}</div>
		</div>`;
	let html = line(__("To", null, "Installment Cheques"), `${esc(doc.lender_name || "")} <span class="text-muted">(${esc(__("Supplier"))})</span>`);
	let from = esc(doc.bank_account || "");
	if (book) {
		from += ` · ${esc(__("Cheque book {0} (next {1})", [book.name, book.next_no || "—"]))}`;
	}
	html += line(__("From", null, "Installment Cheques"), from);
	if (rows.length) {
		const total = rows.reduce((sum, row) => sum + dd_unpaid(row), 0);
		html += `<p style="margin-top: var(--margin-md)"><b>${esc(
			__("{0} cheques · {1} · {2} → {3}", [
				rows.length,
				dd_money(frm, total),
				dd_date(rows[0].due_date),
				dd_date(rows[rows.length - 1].due_date),
			])
		)}</b></p>`;
		const body = rows
			.map((row) => {
				const principal = flt(row.principal) - flt(row.paid_principal);
				const interest = flt(row.interest) - flt(row.paid_interest);
				return `<tr>
					<td>${cint(row.installment_no)}</td>
					<td style="white-space: nowrap">${esc(dd_date(row.due_date))}</td>
					<td style="text-align: end"><bdi>${esc(dd_money(frm, principal))}</bdi></td>
					<td style="text-align: end"><bdi>${esc(dd_money(frm, interest))}</bdi></td>
					<td style="text-align: end"><b><bdi>${esc(dd_money(frm, dd_unpaid(row)))}</bdi></b></td>
				</tr>`;
			})
			.join("");
		html += `<div style="max-height: 320px; overflow-y: auto"><table class="table table-sm table-bordered">
			<thead><tr>
				<th>#</th>
				<th>${esc(__("Due Date", null, "Treasury"))}</th>
				<th style="text-align: end">${esc(__("Principal"))}</th>
				<th style="text-align: end">${esc(terms.interest)}</th>
				<th style="text-align: end">${esc(__("Cheque", null, "Installment Cheques"))}</th>
			</tr></thead>
			<tbody>${body}</tbody>
		</table></div>`;
	} else {
		html += `<p style="margin-top: var(--margin-md)">${esc(__("Every unpaid installment already has a cheque."))}</p>`;
	}
	const skipped = dd_skipped_rows(frm);
	if (skipped) {
		html += `<p class="text-muted small">${esc(__("Skipped: {0}", [skipped]))}</p>`;
	}
	if (rows.length) {
		html += `<p class="text-muted small">${esc(
			__(
				"The cheques are posted on their dates as repayments of these installments. Amounts and dates follow the schedule and cannot be changed in the batch."
			)
		)}</p>`;
	}
	fields.push({ fieldname: "preview", fieldtype: "HTML", options: html });
	const dialog = new frappe.ui.Dialog({
		title: __("Installment Cheques · {0}", [doc.name]),
		size: "large",
		fields,
	});
	if (rows.length) {
		dialog.set_primary_action(__("Create Cheque Batch"), () => {
			dialog.hide();
			frm.call({ doc: frm.doc, method: "make_installment_cheques", freeze: true }).then((r) => {
				if (r.message) {
					const synced = frappe.model.sync(r.message)[0];
					frappe.set_route("Form", synced.doctype, synced.name);
				}
			});
		});
	} else {
		dialog.set_primary_action(__("Close"), () => dialog.hide());
	}
	dialog.show();
}

function dd_skipped_rows(frm) {
	// "#1–#4 paid · #7 already has cheque 000121 (Delivered)"
	const rows = (frm.doc.repayment_schedule || [])
		.filter((row) => !cint(row.is_prepayment))
		.sort((a, b) => cint(a.installment_no) - cint(b.installment_no));
	const paid = rows.filter((row) => dd_unpaid(row) <= 0).map((row) => cint(row.installment_no));
	const parts = [];
	if (paid.length) {
		parts.push(`${dd_ranges(paid)} ${__("paid", null, "Installment Cheques")}`);
	}
	rows.forEach((row) => {
		const cheque = dd_unpaid(row) > 0 && dd_live_cheque(frm, row);
		if (cheque) {
			parts.push(
				`#${cint(row.installment_no)} ${__("already has cheque {0} ({1})", [
					cheque.cheque_no || cheque.name,
					__(cheque.operational_status, null, "PDC Cheque"),
				])}`
			);
		}
	});
	return parts.join(" · ");
}

function dd_ranges(numbers) {
	// [1, 2, 3, 4, 6] -> "#1–#4, #6"
	const ranges = [];
	numbers.forEach((n) => {
		const last = ranges[ranges.length - 1];
		if (last && n === last[1] + 1) {
			last[1] = n;
		} else {
			ranges.push([n, n]);
		}
	});
	return ranges.map(([a, b]) => (a === b ? `#${a}` : `#${a}–#${b}`)).join(", ");
}

// ------------------------------------------------------------------------------------------ Istisna'a stages

function dd_pending_stages(frm) {
	return (frm.doc.disbursement_stages || []).filter((row) => row.status !== "Disbursed");
}

function dd_disburse_stage_dialog(frm) {
	// design 7g §3.8: pick the stage, confirm the date; the summary is the confirmation
	const esc = frappe.utils.escape_html;
	const doc = frm.doc;
	const pending = dd_pending_stages(frm);
	const today = frappe.datetime.get_today();
	const latest = (doc.disbursement_stages || [])
		.map((row) => row.disbursed_on)
		.filter(Boolean)
		.sort()
		.pop();
	const foreign = flt(doc.exchange_rate) && flt(doc.exchange_rate) !== 1;
	const summary = (dialog) => {
		const row = pending.find((r) => r.name === dialog.get_value("stage"));
		if (!row) {
			return "";
		}
		const target =
			doc.disburse_to === "Other Account"
				? __("Debits {0} (paid by the financier directly)", [doc.disbursement_account || ""])
				: __("Credits {0}", [doc.bank_account || ""]);
		return `<p><b>${esc(dd_money(frm, row.amount))}</b> · ${esc(target)}<br>
			<span class="text-muted">${esc(
				__("Disbursed so far {0} of {1}", [dd_money(frm, doc.disbursed_amount), dd_money(frm, doc.amount)])
			)}</span></p>`;
	};
	let dialog = null;
	const refresh = () => dialog && dialog.fields_dict.summary.$wrapper.html(summary(dialog));
	dialog = new frappe.ui.Dialog({
		title: __("Disburse Stage"),
		fields: [
			{
				fieldname: "stage",
				label: __("Stage"),
				fieldtype: "Select",
				reqd: 1,
				options: pending.map((row) => ({
					value: row.name,
					label: __("Stage {0} · {1} · {2} · {3}", [
						row.idx,
						dd_date(row.stage_date),
						dd_money(frm, row.amount),
						// Select option labels are rendered as HTML
						frappe.utils.escape_html(row.description || ""),
					]),
				})),
				default: pending[0].name,
				onchange: refresh,
			},
			{ fieldname: "summary", fieldtype: "HTML" },
			{
				fieldname: "disbursement_date",
				label: __("Date", null, "Treasury"),
				fieldtype: "Date",
				reqd: 1,
				default: latest && latest > today ? latest : today,
				description: __("The date on the bank advice or the contractor's certificate."),
			},
			{
				fieldname: "exchange_rate",
				label: __("Exchange Rate", null, "Treasury"),
				fieldtype: "Float",
				precision: 9,
				hidden: foreign ? 0 : 1,
				description: __("Foreign currency only. Default: the exchange rate of the date."),
			},
			{ fieldname: "remarks", label: __("Remarks", null, "Treasury"), fieldtype: "Small Text" },
		],
		primary_action_label: __("Disburse"),
		primary_action(values) {
			dialog.hide();
			frm.call({ doc: frm.doc, method: "disburse_stage", args: values, freeze: true }).then((r) => {
				const result = r.message || {};
				frappe.show_alert({
					message: __("Stage {0} disbursed: {1}.", [
						cint(result.stage),
						result.voucher_no ? `<b><bdi>${esc(result.voucher_no)}</bdi></b>` : "",
					]),
					indicator: "green",
				});
				frm.reload_doc();
			});
		},
	});
	dialog.show();
	refresh();
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

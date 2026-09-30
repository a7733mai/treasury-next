// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities/lc.py; the buttons below only mirror them.
const LC_INDICATORS = {
	Draft: "gray",
	Opened: "blue",
	Amended: "blue",
	"Documents Received": "purple",
	Accepted: "orange",
	Settled: "green",
	Expired: "red",
	Cancelled: "darkgrey",
};

frappe.ui.form.on("Letter of Credit", {
	setup(frm) {
		lc_setup_queries(frm);
	},

	facility(frm) {
		if (!frm.doc.facility) {
			return;
		}
		frappe.db.get_doc("Bank Facility", frm.doc.facility).then((facility) => {
			const limit = (facility.limits || []).find((row) => row.limit_type === "Letter of Credit");
			frm.set_value("bank_account", facility.default_bank_account || frm.doc.bank_account);
			frm.set_value("cash_margin_percent", limit ? limit.cash_margin_percent : 0);
			frm.set_value("commission_rate", limit ? limit.commission_rate : 0);
			if (!frm.doc.commission_account && facility.commission_expense_account) {
				frm.set_value("commission_account", facility.commission_expense_account);
			}
		});
	},

	refresh(frm) {
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		lc_translate_log(frm);
		if (frm.is_new() || frm.doc.docstatus === 0) {
			return;
		}
		lc_trust_receipt_headline(frm);
		frm.page.set_indicator(__(frm.doc.status, null, "Letter of Credit"), LC_INDICATORS[frm.doc.status] || "blue");
		if (frm.doc.docstatus !== 1 || frm.is_dirty() || !frappe.user.has_role("Treasury Manager")) {
			return;
		}
		const status = frm.doc.status;
		const today = (fieldname, label) => ({
			fieldname,
			label,
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		});
		const money = (fieldname, label, extra) => ({
			fieldname,
			label,
			fieldtype: "Currency",
			options: frm.doc.currency,
			...extra,
		});
		const rate = {
			fieldname: "exchange_rate",
			label: __("Exchange Rate", null, "Treasury"),
			fieldtype: "Float",
			precision: 9,
			description: __("Foreign currency only. Default: the exchange rate of the date."),
		};
		const remarks = { fieldname: "remarks", label: __("Remarks", null, "Treasury"), fieldtype: "Small Text" };

		if (["Opened", "Amended"].includes(status)) {
			lc_action(frm, "amend", __("Amend", null, "Letter of Credit"), [
				today("amendment_date", __("Amendment Date")),
				money("new_amount", __("New Amount"), { default: frm.doc.current_amount }),
				{
					fieldname: "new_expiry_date",
					label: __("New Expiry Date"),
					fieldtype: "Date",
					default: frm.doc.effective_expiry_date,
				},
				money("charges", __("Amendment Charges")),
				rate,
				remarks,
			]);
			lc_action(frm, "receive_documents", __("Receive Documents"), [
				today("documents_date", __("Documents Date")),
				money("documents_amount", __("Documents Amount"), { reqd: 1, default: frm.doc.current_amount }),
				money("charges", __("Documents Charges")),
				rate,
				remarks,
			]);
		}
		if (status === "Documents Received" && frm.doc.lc_type === "Usance") {
			lc_action(frm, "accept", __("Accept", null, "Letter of Credit"), [
				today("acceptance_date", __("Acceptance Date")),
				{
					fieldname: "maturity_date",
					label: __("Maturity Date"),
					fieldtype: "Date",
					description: __("Default: the acceptance date plus the usance days."),
				},
				remarks,
			]);
		}
		if (
			(status === "Documents Received" && frm.doc.lc_type === "Sight") ||
			(status === "Accepted" && frm.doc.lc_type === "Usance")
		) {
			const settle_fields = [
				today("settlement_date", __("Settlement Date")),
				money("amount", __("Amount", null, "Treasury"), { reqd: 1, default: frm.doc.documents_amount }),
				rate,
				{
					fieldname: "purchase_invoice",
					label: __("Purchase Invoice"),
					fieldtype: "Link",
					options: "Purchase Invoice",
					get_query: () => ({
						filters: {
							supplier: frm.doc.supplier,
							company: frm.doc.company,
							docstatus: 1,
							outstanding_amount: [">", 0],
						},
					}),
				},
				remarks,
			];
			if ((frm.doc.__onload || {}).has_trust_receipt_limit) {
				// design 7g §6.1: one dialog with a live Trust Receipt preview
				frm.add_custom_button(
					__("Settle", null, "Letter of Credit"),
					() => lc_settle_dialog(frm, settle_fields),
					__("Actions")
				);
			} else {
				lc_action(frm, "settle", __("Settle", null, "Letter of Credit"), settle_fields);
			}
		}
		if (status === "Expired" && flt(frm.doc.margin_held) > 0) {
			lc_action(frm, "release_margin", __("Release Margin"), [
				today("release_date", __("Release Date", null, "Treasury")),
				rate,
				remarks,
			]);
		}
	},
});

function lc_trust_receipt_fields(frm, refresh) {
	// spec 7g §3.9 / design §6.1: the bank finances the settlement less the margin held by a Trust Receipt
	// drawdown; tenor and rate default to the sub-limit's (P18)
	const onload = frm.doc.__onload || {};
	const limit = onload.trust_receipt_limit || {};
	const financed = "eval:doc.finance_by_trust_receipt";
	return [
		{
			fieldname: "finance_by_trust_receipt",
			label: __("Finance by Trust Receipt"),
			fieldtype: "Check",
			onchange: refresh,
		},
		{ fieldname: "tr_preview", fieldtype: "HTML", depends_on: financed },
		{
			fieldname: "tr_tenor_months",
			label: __("Tenor (Months)", null, "Letter of Credit"),
			fieldtype: "Int",
			default: limit.default_tenor_months || onload.trust_receipt_tenor || 0,
			depends_on: financed,
		},
		{
			fieldname: "tr_first_repayment_date",
			label: __("First Repayment Date", null, "Letter of Credit"),
			fieldtype: "Date",
			depends_on: financed,
			description: __("Default: settlement date plus the tenor (one bullet repayment)."),
		},
		{
			fieldname: "tr_interest_rate",
			label: treasury.facility.terms(limit.is_islamic).interest_rate,
			fieldtype: "Percent",
			default: limit.interest_rate,
			depends_on: financed,
			description: __("Default: the rate of the Trust Receipt sub-limit."),
		},
	];
}

function lc_settle_dialog(frm, fields) {
	// the settle action with "Finance by Trust Receipt" (design 7g §6.1): a live preview of the amount financed
	const esc = frappe.utils.escape_html;
	const limit = (frm.doc.__onload || {}).trust_receipt_limit || {};
	const label = __("Settle", null, "Letter of Credit");
	let dialog = null;
	const refresh = () => {
		if (!dialog) {
			return;
		}
		const financed = cint(dialog.get_value("finance_by_trust_receipt"));
		dialog.get_primary_btn().text(financed ? __("Settle and Create Trust Receipt") : label);
		const amount = flt(dialog.get_value("amount"));
		const margin = flt(frm.doc.margin_held);
		const tr_amount = flt(amount - margin, precision("amount", frm.doc));
		const money = (value) => format_currency(value, frm.doc.currency);
		const html =
			tr_amount > 0
				? `<p class="text-muted">${esc(
						__("Trust Receipt: {0} (settlement {1} − margin {2}) · available {3}", [
							money(tr_amount),
							money(amount),
							money(margin),
							money(limit.available),
						])
				  )}</p>`
				: `<p style="color: var(--red-600)">${esc(
						__("The cash margin covers the settlement: nothing to finance by Trust Receipt.")
				  )}</p>`;
		dialog.fields_dict.tr_preview.$wrapper.html(html);
	};
	const all = [...fields];
	// before the remarks
	all.splice(all.length - 1, 0, ...lc_trust_receipt_fields(frm, refresh));
	const amount = all.find((df) => df.fieldname === "amount");
	amount && (amount.onchange = refresh);
	dialog = new frappe.ui.Dialog({
		title: label,
		fields: all,
		primary_action_label: label,
		primary_action(values) {
			dialog.hide();
			frm.call({ doc: frm.doc, method: "settle", args: values, freeze: true }).then((r) => {
				const drawdown = r.message && r.message.trust_receipt_drawdown;
				frm.reload_doc().then(() => {
					const tr = (frm.doc.__onload || {}).trust_receipt;
					if (drawdown && tr) {
						frappe.show_alert(
							{
								message: __("Trust Receipt {0} created: {1} due {2}.", [
									lc_link("Facility Drawdown", drawdown),
									esc(format_currency(tr.amount, tr.currency)),
									esc(tr.due_date ? frappe.datetime.str_to_user(tr.due_date) : ""),
								]),
								indicator: "green",
							},
							10
						);
					}
				});
			});
		},
	});
	dialog.show();
	refresh();
}

function lc_trust_receipt_headline(frm) {
	// design 7g §6.2: the Trust Receipt that financed the settlement
	const tr = (frm.doc.__onload || {}).trust_receipt;
	if (!tr) {
		return;
	}
	const esc = frappe.utils.escape_html;
	frm.dashboard.set_headline(
		`<div>${__("Financed by Trust Receipt {0} · {1} · due {2} · {3}.", [
			lc_link("Facility Drawdown", tr.name),
			esc(format_currency(tr.amount, tr.currency)),
			esc(tr.due_date ? frappe.datetime.str_to_user(tr.due_date) : ""),
			esc(__(tr.repayment_status || "Open", null, "Facility Drawdown")),
		])}</div>`,
		"blue"
	);
}

function lc_link(doctype, name) {
	const esc = frappe.utils.escape_html;
	return `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}"><b><bdi>${esc(name)}</bdi></b></a>`;
}

function lc_setup_queries(frm) {
	frm.set_query("facility", () => ({ filters: { status: "Active" } }));
	frm.set_query("bank_account", () => ({
		filters: { company: frm.doc.company, is_company_account: 1, bank: frm.doc.bank },
	}));
	frm.set_query("purchase_order", () => ({
		filters: { supplier: frm.doc.supplier, company: frm.doc.company, docstatus: 1 },
	}));
	frm.set_query("margin_account", () => ({
		filters: {
			company: frm.doc.company,
			is_group: 0,
			root_type: "Asset",
			account_currency: frm.doc.currency,
			account_type: ["not in", ["Payable", "Receivable", "Bank", "Cash"]],
		},
	}));
	["commission_account", "charges_account"].forEach((fieldname) => {
		frm.set_query(fieldname, () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
	});
}

function lc_translate_log(frm) {
	// events and statuses are stored in English in the status log (Data fields): translate them on display
	["event", "from_status", "to_status"].forEach((fieldname) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield("Bank Instrument Status Log", fieldname, name);
			if (df) {
				df.formatter = (value) =>
					frappe.utils.escape_html(value ? __(value, null, "Letter of Credit") : "");
			}
		}
	});
	frm.refresh_field("status_log");
}

function lc_action(frm, method, label, fields) {
	frm.add_custom_button(
		label,
		() => {
			frappe.prompt(
				fields,
				(values) => {
					frm.call({ doc: frm.doc, method, args: values, freeze: true }).then(() => frm.reload_doc());
				},
				label,
				label
			);
		},
		__("Actions")
	);
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

// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities/discounting.py; the buttons below only mirror them.
const CHD_INDICATORS = {
	Draft: "gray",
	Discounted: "blue",
	"Partially Settled": "orange",
	Settled: "green",
	Cancelled: "darkgrey",
};

frappe.ui.form.on("Cheque Discounting", {
	setup(frm) {
		frm.set_query("facility", () => ({ filters: { status: "Active" } }));
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1, bank: frm.doc.bank },
		}));
		frm.set_query("discount_expense_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
		frm.set_query("pdc_cheque", "items", () => ({
			filters: {
				company: frm.doc.company,
				direction: "Incoming",
				operational_status: "Received",
				accounting_status: "Party Posted",
				currency: frm.doc.currency,
				effective_due_date: [">", frm.doc.discount_date || frappe.datetime.get_today()],
			},
		}));
	},

	facility(frm) {
		if (!frm.doc.facility) {
			return;
		}
		frappe.db.get_doc("Bank Facility", frm.doc.facility).then((facility) => {
			const limit = (facility.limits || []).find((row) => row.limit_type === "Cheque Discounting");
			frm.set_value("bank_account", facility.default_bank_account || frm.doc.bank_account);
			frm.set_value("discount_rate", limit ? limit.interest_rate : 0);
			frm.set_value("day_count", (limit && limit.day_count) || "Actual/365");
			if (!frm.doc.discount_expense_account && facility.interest_expense_account) {
				frm.set_value("discount_expense_account", facility.interest_expense_account);
			}
		});
	},

	refresh(frm) {
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		chd_translate_log(frm);
		if (frm.is_new() || frm.doc.docstatus === 0) {
			return;
		}
		frm.page.set_indicator(__(frm.doc.status, null, "Cheque Discounting"), CHD_INDICATORS[frm.doc.status] || "blue");
		const open_rows = (frm.doc.items || []).filter((row) => row.status === "Discounted");
		if (
			frm.doc.docstatus !== 1 ||
			frm.is_dirty() ||
			!open_rows.length ||
			!frappe.user.has_role("Treasury Manager")
		) {
			return;
		}
		frm.add_custom_button(
			__("Settle Cheques"),
			() => {
				const today = frappe.datetime.get_today();
				const fields = [
					{
						fieldname: "settlement_date",
						label: __("Settlement Date"),
						fieldtype: "Date",
						reqd: 1,
						default: today,
					},
					{
						fieldtype: "HTML",
						options: `<p class="text-muted small">${__(
							"Tick the cheques the drawers paid to the bank at maturity. A bounced cheque is bounced from the cheque itself (the bank takes the amount back)."
						)}</p>`,
					},
					...open_rows.map((row) => ({
						fieldname: `cheque_${row.idx}`,
						label: `${row.cheque_no} — ${row.party_name || row.party} — ${format_currency(
							row.amount,
							frm.doc.currency
						)} — ${frappe.datetime.str_to_user(row.due_date)}`,
						fieldtype: "Check",
						default: row.due_date <= today ? 1 : 0,
						cheque: row.pdc_cheque,
					})),
				];
				frappe.prompt(
					fields,
					(values) => {
						const cheques = open_rows
							.filter((row) => values[`cheque_${row.idx}`])
							.map((row) => row.pdc_cheque);
						if (!cheques.length) {
							frappe.msgprint(__("Select at least one cheque."));
							return;
						}
						frm.call({
							doc: frm.doc,
							method: "settle_cheques",
							args: { cheques, settlement_date: values.settlement_date },
							freeze: true,
						}).then(() => frm.reload_doc());
					},
					__("Settle Cheques"),
					__("Settle", null, "Cheque Discounting")
				);
			},
			__("Actions")
		);
	},
});

function chd_translate_log(frm) {
	// events and statuses are stored in English in the status log (Data fields): translate them on display
	["event", "from_status", "to_status"].forEach((fieldname) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield("Bank Instrument Status Log", fieldname, name);
			if (df) {
				df.formatter = (value) =>
					frappe.utils.escape_html(value ? __(value, null, "Cheque Discounting") : "");
			}
		}
	});
	frm.refresh_field("status_log");
}

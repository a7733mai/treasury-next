// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Validation lives in bank_charge_category.py; this script previews the keywords as PDC Bank Clearing reads
// them and defaults the account rows (design 7d §2). Form scripts share the global scope: names are
// prefixed `BCC_` / `bcc_`.
const BCC_ACCOUNT = "Bank Charge Category Account";
const BCC_METHOD = "treasury.treasury_setup.doctype.bank_charge_category.bank_charge_category.";
const BCC_MIN_KEYWORD = 3; // MIN_KEYWORD_LENGTH of the controller

frappe.ui.form.on("Bank Charge Category", {
	setup(frm) {
		frm.set_query("expense_account", "accounts", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { company: row.company, root_type: "Expense", is_group: 0 } };
		});
		frm.set_query("cost_center", "accounts", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { company: row.company, is_group: 0 } };
		});
	},

	refresh(frm) {
		bcc_render_keywords(frm);
		bcc_set_headline(frm);
	},

	keywords(frm) {
		bcc_render_keywords(frm);
	},

	auto_select_proposals(frm) {
		bcc_render_keywords(frm);
	},

	disabled(frm) {
		bcc_set_headline(frm);
	},
});

frappe.ui.form.on(BCC_ACCOUNT, {
	accounts_add(frm, cdt, cdn) {
		// the user's company, when no row has it yet
		const company = frappe.defaults.get_user_default("Company");
		const used = (frm.doc.accounts || []).some((row) => row.company === company && row.name !== cdn);
		if (company && !used) {
			frappe.model.set_value(cdt, cdn, "company", company);
		}
	},

	company(frm, cdt, cdn) {
		// design P10: the company's single Bank Charges Account (PDC Accounts), as for the default categories
		const row = locals[cdt][cdn];
		if (!row.company || row.expense_account) {
			bcc_set_headline(frm);
			return;
		}
		const company = row.company;
		frappe.xcall(BCC_METHOD + "get_company_charge_account", { company }).then((account) => {
			const current = locals[cdt] && locals[cdt][cdn];
			if (account && current && current.company === company && !current.expense_account) {
				frappe.model.set_value(cdt, cdn, "expense_account", account);
			}
		});
		bcc_set_headline(frm);
	},

	accounts_remove(frm) {
		bcc_set_headline(frm);
	},
});

function bcc_parse_keywords(text) {
	// mirrors parse_keywords / clean_keywords: trimmed, single spaces, lower-case; duplicates marked
	const seen = new Set();
	return String(text || "")
		.split("\n")
		.map((line) => line.split(/\s+/).filter(Boolean).join(" "))
		.filter(Boolean)
		.map((keyword) => {
			const key = keyword.toLowerCase();
			const duplicate = seen.has(key);
			seen.add(key);
			return { keyword, duplicate, short: keyword.length < BCC_MIN_KEYWORD };
		});
}

function bcc_render_keywords(frm) {
	// design P9: the keywords as pills, with the mistakes the server would refuse or ignore
	const field = frm.get_field("keywords_preview");
	if (!field) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const keywords = bcc_parse_keywords(frm.doc.keywords);
	const lines = [];
	if (keywords.length) {
		const pills = keywords.map(({ keyword, duplicate, short }) => {
			if (short) {
				return `<span class="indicator-pill red" title="${esc(
					__("Too short (at least 3 characters)")
				)}"><bdi>${esc(keyword)}</bdi></span>`;
			}
			const text = `<bdi>${esc(keyword)}</bdi>`;
			return `<span class="indicator-pill gray">${duplicate ? `<s>${text}</s>` : text}</span>`;
		});
		lines.push(`<div style="display: flex; flex-wrap: wrap; gap: var(--margin-xs)">${pills.join("")}</div>`);
		if (!frm.doc.auto_select_proposals) {
			lines.push(`<p class="text-muted small">${esc(__("Proposals are shown but not pre-selected."))}</p>`);
		}
	} else {
		lines.push(
			`<p class="text-muted small">${esc(
				__("No keywords: this category is never proposed by PDC Bank Clearing.")
			)}</p>`
		);
	}
	lines.push(`<p class="text-muted small">${esc(__("e.g. ACCT MAINT FEE · رسوم إدارة"))}</p>`);
	field.$wrapper.html(`<div style="margin-bottom: var(--margin-md)">${lines.join("")}</div>`);
}

function bcc_set_headline(frm) {
	if (frm._bcc_$headline) {
		frm._bcc_$headline.remove();
		frm._bcc_$headline = null;
	}
	if (frm.is_new()) {
		return;
	}
	const esc = frappe.utils.escape_html;
	if (frm.doc.disabled) {
		frm._bcc_$headline = frm.dashboard.set_headline(
			`<div>${esc(__("Disabled: not offered on new charges or in clearing proposals."))}</div>`,
			"gray"
		);
		return;
	}
	const company = frappe.defaults.get_user_default("Company");
	const has_row = (frm.doc.accounts || []).some((row) => row.company === company && row.expense_account);
	if (company && !has_row) {
		frm._bcc_$headline = frm.dashboard.set_headline(
			`<div>${esc(
				__("No expense account for {0}: charges of this category cannot be created or proposed for it.", [
					company,
				])
			)}</div>`,
			"orange"
		);
	}
}

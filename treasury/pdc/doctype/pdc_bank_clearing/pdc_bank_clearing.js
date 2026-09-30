// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Matching and clearing rules live in pdc_bank_clearing.py, treasury/pdc/lifecycle.py,
// treasury/bank_transfers/lifecycle.py and treasury/bank_transfers/charges.py.
// Form scripts share the global scope: names are prefixed `PBC_` / `pbc_`.
const PBC_MATCH = "PDC Clearing Match";
const PBC_CHARGE = "Bank Charge";
const PBC_KEYWORD = "Keyword";
const PBC_QUALITY_COLORS = { Strong: "green", Weak: "orange", Keyword: "blue" };

frappe.ui.form.on("PDC Bank Clearing", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("bank_charge_category", "matches", () => ({ filters: { disabled: 0 } }));
	},

	onload(frm) {
		if (!pbc_apply_route_options(frm) && !frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_user_default("Company"));
		}
	},

	refresh(frm) {
		frm.disable_save();
		frm.set_df_property("matches", "cannot_add_rows", 1);
		frm.set_intro(
			__(
				"Matches bank statement lines with open cheques, bank transfers and bank charges, and proposes bank charges for unmatched lines."
			),
			"blue"
		);
		pbc_set_formatters(frm);
		pbc_add_grid_buttons(frm);
		pbc_update_counter(frm);
		// "Match Bank Statement" on a Bank Transfer / Bank Charge opens this (single) form with its company and bank
		pbc_apply_route_options(frm);
	},

	company(frm) {
		frm.set_value("bank_account", "");
	},

	get_suggestions_button(frm) {
		frm.call({ doc: frm.doc, method: "get_suggestions", freeze: true }).then((r) => {
			frm._pbc_done = new Set();
			pbc_refresh_grid(frm);
			if (!r.message) {
				frappe.show_alert({ message: __("No matching bank transactions found."), indicator: "orange" });
			}
		});
	},

	clear_selected_button(frm) {
		const selected = (frm.doc.matches || []).filter((row) => row.selected);
		if (!selected.length) {
			frappe.msgprint(__("Select the matches to clear."));
			return;
		}
		const counts = pbc_counts(selected);
		const missing = selected.filter((row) => pbc_kind(row) === "create" && !row.bank_charge_category);
		if (missing.length) {
			frappe.msgprint(
				`${frappe.utils.escape_html(__("Choose a Charge Category."))} (${missing.map((row) => `#${cint(row.idx)}`).join(", ")})`
			);
			return;
		}
		let message;
		if (counts.reconcile || counts.create) {
			message = frappe.utils.escape_html(
				__(
					"Clear {0} cheque(s), {1} bank transfer(s), reconcile {2} bank charge(s) and create {3} bank charge(s)?",
					[counts.cheque, counts.transfer, counts.reconcile, counts.create]
				)
			);
			message += pbc_create_table(selected);
		} else if (counts.transfer) {
			message = __("Clear {0} cheque(s) and {1} bank transfer(s) and reconcile their bank transactions?", [
				counts.cheque,
				counts.transfer,
			]);
		} else {
			message = __("Clear {0} cheque(s) and reconcile their bank transactions?", [counts.cheque]);
		}
		frappe.confirm(message, () => {
			const names = selected.map((row) => row.name);
			frm.call({ doc: frm.doc, method: "clear_selected", freeze: true }).then((r) => {
				// a cleared row comes back unselected; a skipped one keeps its tick and its reason
				frm._pbc_done = frm._pbc_done || new Set();
				(frm.doc.matches || []).forEach((row) => {
					if (names.includes(row.name) && !row.selected) {
						frm._pbc_done.add(row.name);
					}
				});
				pbc_refresh_grid(frm);
				pbc_show_created((r.message && r.message.created) || []);
			});
		});
	},
});

frappe.ui.form.on(PBC_MATCH, {
	selected(frm) {
		pbc_update_counter(frm);
	},

	bank_charge_category(frm, cdt, cdn) {
		// choosing a category on a proposal ticks it (the server still skips a category without an account)
		const row = locals[cdt][cdn];
		if (row.match_quality === PBC_KEYWORD && !row.bank_charge && row.bank_charge_category) {
			row.selected = 1;
			row.result = "";
			pbc_refresh_grid(frm);
		}
	},
});

function pbc_apply_route_options(frm) {
	const options = frappe.route_options;
	if (!options || !(options.company || options.bank_account)) {
		return false;
	}
	frappe.route_options = null;
	// the company trigger clears the bank account: set the bank once it ran
	Promise.resolve(options.company ? frm.set_value("company", options.company) : null)
		.then(() => options.bank_account && frm.set_value("bank_account", options.bank_account))
		.then(() => frm.doc.company && frm.doc.bank_account && frm.trigger("get_suggestions_button"));
	return true;
}

// ------------------------------------------------------------------------------------------ rows

function pbc_kind(row) {
	// mirrors row_kind of pdc_bank_clearing.py; a charge row is reconciled (existing) or created (proposal)
	const doctype = row.reference_doctype;
	if (doctype === "Bank Transfer" || (doctype !== PBC_CHARGE && row.bank_transfer && !row.cheque)) {
		return "transfer";
	}
	if (doctype === PBC_CHARGE || (row.bank_charge && !row.cheque)) {
		return row.bank_charge ? "reconcile" : "create";
	}
	return "cheque";
}

function pbc_counts(rows) {
	const counts = { cheque: 0, transfer: 0, reconcile: 0, create: 0 };
	rows.forEach((row) => (counts[pbc_kind(row)] += 1));
	return counts;
}

function pbc_is_done(frm, row) {
	return !!(frm._pbc_done && frm._pbc_done.has(row.name));
}

function pbc_refresh_grid(frm) {
	frm.refresh_field("matches");
	pbc_add_grid_buttons(frm);
	pbc_update_counter(frm);
}

function pbc_select(frm, predicate) {
	(frm.doc.matches || []).forEach((row) => {
		row.selected = !pbc_is_done(frm, row) && predicate(row) ? 1 : 0;
	});
	pbc_refresh_grid(frm);
}

// ------------------------------------------------------------------------------------------ display

function pbc_link(doctype, name) {
	return `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}"><bdi>${frappe.utils.escape_html(
		name
	)}</bdi></a>`;
}

function pbc_set_formatters(frm) {
	const esc = frappe.utils.escape_html;
	const is_row = (row) => row && row.doctype === PBC_MATCH;
	const formatters = {
		direction: (value) => esc(value ? __(value) : ""),
		match_quality: (value) =>
			value
				? `<span class="indicator-pill ${PBC_QUALITY_COLORS[value] || "gray"}">${esc(
						__(value, null, PBC_MATCH)
				  )}</span>`
				: "",
		// design P13: one "Document" column for the cheque, the bank transfer or the bank charge of the row
		cheque: (value, df, options, row) => {
			if (!is_row(row)) {
				return value ? pbc_link("PDC Cheque", value) : "";
			}
			if (row.cheque) {
				return pbc_link("PDC Cheque", row.cheque);
			}
			if (row.bank_transfer) {
				return pbc_link("Bank Transfer", row.bank_transfer);
			}
			if (row.bank_charge) {
				return pbc_link(PBC_CHARGE, row.bank_charge);
			}
			if (row.match_quality === PBC_KEYWORD) {
				return `<span class="text-primary">+ ${esc(__("New Bank Charge"))}</span>`;
			}
			return "";
		},
		bank_transfer: (value) => (value ? pbc_link("Bank Transfer", value) : ""),
		bank_charge: (value) => (value ? pbc_link(PBC_CHARGE, value) : ""),
		bank_charge_category: (value, df, options, row) => {
			const proposal = is_row(row) && row.match_quality === PBC_KEYWORD && !row.bank_charge;
			if (!value) {
				return proposal ? `<span class="text-warning">${esc(__("Choose a category"))}</span>` : "";
			}
			const name = esc(__(value));
			if (proposal && row.result && !row.selected) {
				// not pre-selected: the reason is in the dot's tooltip (and in the Result of the row)
				return `<span class="indicator orange" title="${esc(row.result)}"></span>${name}`;
			}
			return name;
		},
		reference_doctype: (value) => esc(value ? __(value, null, PBC_MATCH) : ""),
		// plain text from the server (an error message may repeat client-sent values): never markup
		result: (value) => esc(value || ""),
	};
	Object.entries(formatters).forEach(([fieldname, formatter]) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield(PBC_MATCH, fieldname, name);
			if (df) {
				df.formatter = formatter;
			}
		}
	});
	const document = frappe.meta.get_docfield(PBC_MATCH, "cheque", frm.doc.name);
	if (document) {
		document.label = __("Document", null, PBC_MATCH);
	}
	frm.refresh_field("matches");
}

function pbc_add_grid_buttons(frm) {
	// design P15: bulk selection above the grid, shown when it has rows
	const grid = frm.fields_dict.matches && frm.fields_dict.matches.grid;
	if (!grid || !grid.add_custom_button) {
		return;
	}
	const has_rows = !!(frm.doc.matches || []).length;
	// translated here with a context (the grid translates the label again, which keeps it)
	const buttons = [
		[
			__("Select All", null, PBC_MATCH),
			() => pbc_select(frm, (row) => pbc_kind(row) !== "create" || !!row.bank_charge_category),
		],
		[__("Strong Only", null, PBC_MATCH), () => pbc_select(frm, (row) => row.match_quality === "Strong")],
		[
			__("Charge Proposals", null, PBC_MATCH),
			() => pbc_select(frm, (row) => pbc_kind(row) === "create" && !!row.bank_charge_category),
		],
		[__("Select None", null, PBC_MATCH), () => pbc_select(frm, () => false)],
		[__("Set Charge Category", null, PBC_MATCH), () => pbc_set_category_dialog(frm)],
	];
	// prepended one by one: added in reverse so they read in this order
	buttons.reverse().forEach(([label, action]) => {
		const $btn = grid.add_custom_button(label, action, "top");
		$btn && $btn.toggleClass("hidden", !has_rows);
	});
}

function pbc_set_category_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Set Charge Category", null, PBC_MATCH),
		fields: [
			{
				fieldname: "category",
				fieldtype: "Link",
				label: __("Charge Category", null, PBC_MATCH),
				options: "Bank Charge Category",
				reqd: 1,
				get_query: () => ({ filters: { disabled: 0 } }),
			},
		],
		primary_action_label: __("Set Charge Category", null, PBC_MATCH),
		primary_action(values) {
			dialog.hide();
			let count = 0;
			(frm.doc.matches || []).forEach((row) => {
				if (row.selected && pbc_kind(row) === "create" && !pbc_is_done(frm, row)) {
					row.bank_charge_category = values.category;
					row.result = "";
					count += 1;
				}
			});
			pbc_refresh_grid(frm);
			frappe.show_alert({ message: __("Category set on {0} row(s).", [count]), indicator: "green" });
		},
	});
	dialog.show();
}

function pbc_update_counter(frm) {
	// design P15: what "Clear Selected" will do, with the total of the selected statement lines
	const selected = (frm.doc.matches || []).filter((row) => row.selected);
	let html = "";
	if (selected.length) {
		const counts = pbc_counts(selected);
		const total = selected.reduce((sum, row) => sum + flt(row.amount), 0);
		const text = __("Selected: {0} cheque(s) · {1} bank transfer(s) · {2} bank charge(s) to reconcile · {3} to create — {4}", [
			counts.cheque,
			counts.transfer,
			counts.reconcile,
			counts.create,
			format_currency(total, selected[0].currency),
		]);
		html = `<span class="text-muted">${frappe.utils.escape_html(text)}</span>`;
	}
	const field = frm.get_field("matches");
	if (field && field.df.description !== html) {
		frm.set_df_property("matches", "description", html);
	}
}

function pbc_create_table(rows) {
	// design P16: the charges that will be created, per category
	const esc = frappe.utils.escape_html;
	const groups = {};
	rows.filter((row) => pbc_kind(row) === "create").forEach((row) => {
		const group = (groups[row.bank_charge_category] = groups[row.bank_charge_category] || {
			lines: 0,
			amount: 0,
			currency: row.currency,
		});
		group.lines += 1;
		group.amount += flt(row.amount);
	});
	const body = Object.entries(groups)
		.map(
			([category, group]) => `<tr>
				<td>${esc(__(category))}</td>
				<td style="text-align: end">${cint(group.lines)}</td>
				<td style="text-align: end"><bdi>${esc(format_currency(group.amount, group.currency))}</bdi></td>
			</tr>`
		)
		.join("");
	if (!body) {
		return "";
	}
	return `<table class="table table-sm" style="margin-top: var(--margin-md)">
		<thead><tr>
			<th>${esc(__("Charge Category", null, PBC_MATCH))}</th>
			<th style="text-align: end">${esc(__("Lines", null, PBC_MATCH))}</th>
			<th style="text-align: end">${esc(__("Amount", null, "Treasury"))}</th>
		</tr></thead>
		<tbody>${body}</tbody>
	</table>
	<p class="text-muted small">${esc(__("Each new charge is dated on its statement line and submitted at once."))}</p>`;
}

function pbc_show_created(created) {
	// design P16: the new charges, in one list
	if (!created.length) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const url = `/app/bank-charge?name=${encodeURIComponent(JSON.stringify(["in", created]))}`;
	frappe.show_alert(
		{
			message: `${esc(__("{0} bank charge(s) created.", [created.length]))} <a href="${url}">${esc(
				__("View", null, PBC_CHARGE)
			)}</a>`,
			indicator: "green",
		},
		10
	);
}

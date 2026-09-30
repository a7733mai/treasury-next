// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury_company_settings.py and treasury/accounting/payment_modes.py.
// Form scripts share the global scope: the names below are prefixed to stay unique.
const TCS_MAP_DOCTYPE = "Treasury Payment Mode Map";
const TCS_METHODS = [
	"Cheque Out",
	"Cheque In",
	"Bank Transfer",
	"Fawran",
	"SWIFT",
	"Internal Transfer",
	"Auto Debit",
	"Bank Charge",
];
const TCS_DEFAULT_METHODS = ["Cheque Out", "Cheque In", "Bank Transfer"];
const TCS_GENERATE_ROLES = ["Treasury Manager", "System Manager"];
const TCS_CTX = "Treasury Company Settings";
const TCS_FEE_RULE = "Bank Transfer Fee Rule";
const TCS_LIMIT = "Bank Transfer Limit";
const TCS_BT_CTX = "Bank Transfer";
const TCS_SCOPE_COLORS = { "Same Bank": "green", "Other Bank": "blue", International: "purple" };

frappe.ui.form.on("Treasury Company Settings", {
	setup(frm) {
		const account_query = (extra) => () => ({
			filters: Object.assign({ company: frm.doc.company, is_group: 0 }, extra || {}),
		});

		frm.set_query("bounce_charges_account", account_query());
		frm.set_query("bank_account", "pdc_accounts", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query(
			"pdc_payable_account",
			"pdc_accounts",
			account_query({ account_type: "Bank", root_type: "Liability" })
		);
		frm.set_query(
			"pdc_receivable_account",
			"pdc_accounts",
			account_query({ account_type: "Bank", root_type: "Asset" })
		);
		frm.set_query(
			"under_collection_account",
			"pdc_accounts",
			account_query({ account_type: "Bank", root_type: "Asset" })
		);
		frm.set_query("bank_charges_account", "pdc_accounts", account_query());
		frm.set_query("bank_account", "payment_modes", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("mode_of_payment", "payment_modes", () => ({ filters: { enabled: 1 } }));

		// Bank Transfers
		const company_bank = () => ({ filters: { company: frm.doc.company, is_company_account: 1 } });
		["default_normal_bank_account", "default_fawran_bank_account", "default_swift_bank_account"].forEach(
			(fieldname) => frm.set_query(fieldname, company_bank)
		);
		// Pay / receive from documents: banks with a PDC Accounts row
		["default_outgoing_cheque_bank_account", "default_incoming_cheque_bank_account"].forEach((fieldname) =>
			frm.set_query(fieldname, () => ({
				filters: {
					company: frm.doc.company,
					is_company_account: 1,
					name: ["in", (frm.doc.pdc_accounts || []).map((row) => row.bank_account).filter(Boolean)],
				},
			}))
		);
		frm.set_query("bank_account", "transfer_fee_rules", company_bank);
		frm.set_query("bank_account", "transfer_limits", company_bank);
		frm.set_query("expense_account", "transfer_fee_rules", () => ({
			filters: { company: frm.doc.company, root_type: "Expense", is_group: 0 },
		}));
	},

	refresh(frm) {
		frm.events.render_vouchers_help(frm);
		frm.events.mark_unregistered_series(frm);
		frm.events.set_series_headline(frm);
		frm.events.add_mode_buttons(frm);
		frm.events.render_transfer_fees_help(frm);
		frm.events.format_transfer_grids(frm);
		frm.events.add_transfer_buttons(frm);
	},

	transfer_completion(frm) {
		const messages = {
			"On Send": __(
				"Sent transfers will be marked Completed at once and their Payment Entries cleared on the execution date, without waiting for the bank statement."
			),
			"On Send for Fawran": __(
				"Fawran transfers will be marked Completed at once; other methods wait for bank confirmation."
			),
		};
		const message = messages[frm.doc.transfer_completion];
		if (message) {
			frappe.show_alert({ message, indicator: "orange" });
		}
	},

	render_transfer_fees_help(frm) {
		const field = frm.get_field("transfer_fees_help");
		if (!field) {
			return;
		}
		const esc = frappe.utils.escape_html;
		const examples = [
			__("Fawran up to 1,000 → 0.50; above → 2.00: two rows, 0 – 1,000 fixed 0.50 and 1,000.01 – 50,000 fixed 2."),
			__("Transfer to another bank: 10 + 0.1 %, min 15, max 50: one row, Scope Other Bank, range 0 – 0."),
			__("Incoming transfer fee 5: Direction Incoming, Scope Any, fixed 5."),
			__("SWIFT, all charges on us: Scope International, Charge Bearer OUR, fixed 25, Correspondent Fee 15."),
		];
		field.$wrapper.html(`<div class="text-muted small">
			<p><b>${esc(__("How a fee is chosen:"))}</b> ${esc(
			__(
				'The rule with the same bank account, method and direction whose amount range contains the transfer amount. An exact scope beats "Any", an exact charge bearer beats an empty one, and then the upper row wins. Amounts are in the bank account currency.'
			)
		)}</p>
			<p><b>${esc(__("Examples", null, TCS_CTX))}</b></p>
			<ul>${examples.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>
		</div>`);
	},

	format_transfer_grids(frm) {
		const esc = frappe.utils.escape_html;
		const muted = (text, title) =>
			`<span class="text-muted"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`;
		const number = (value) => esc(format_number(flt(value), null, 2));
		const set_formatter = (doctype, fieldname, formatter) => {
			for (const name of [null, frm.doc.name]) {
				const df = frappe.meta.get_docfield(doctype, fieldname, name);
				if (df) {
					df.formatter = formatter;
				}
			}
		};
		set_formatter(TCS_FEE_RULE, "amount_to", (value) =>
			flt(value) ? number(value) : muted("∞", __("no upper limit"))
		);
		set_formatter(TCS_FEE_RULE, "max_fee", (value) => (flt(value) ? number(value) : muted("—")));
		["method", "direction", "scope"].forEach((fieldname) =>
			set_formatter(TCS_FEE_RULE, fieldname, (value) => esc(value ? __(value, null, TCS_FEE_RULE) : ""))
		);
		["per_transaction_limit", "daily_limit"].forEach((fieldname) =>
			set_formatter(TCS_LIMIT, fieldname, (value) => (flt(value) ? number(value) : muted("—")))
		);
		set_formatter(TCS_LIMIT, "method", (value) => esc(value ? __(value, null, TCS_LIMIT) : ""));
		frm.refresh_field("transfer_fee_rules");
		frm.refresh_field("transfer_limits");
	},

	add_transfer_buttons(frm) {
		const can_preview = frappe.model.can_create("Bank Transfer") || frappe.model.can_write("Bank Transfer");
		if (frm.is_new() || frm.is_dirty() || !(frm.doc.transfer_fee_rules || []).length || !can_preview) {
			return;
		}
		frm.add_custom_button(
			__("Test Fee Rules"),
			() => frm.events.test_fee_rules(frm),
			__("Bank Transfers", null, TCS_CTX)
		);
	},

	test_fee_rules(frm) {
		// the saved rules, through the read-only fee preview of the transfer form
		const esc = frappe.utils.escape_html;
		const options = (values, context) => values.map((value) => ({ value, label: __(value, null, context) }));
		const dialog = new frappe.ui.Dialog({
			title: __("Test Fee Rules"),
			fields: [
				{
					fieldname: "bank_account",
					label: __("Bank Account", null, TCS_BT_CTX),
					fieldtype: "Link",
					options: "Bank Account",
					reqd: 1,
					default: frm.doc.transfer_fee_rules[0].bank_account,
					get_query: () => ({ filters: { company: frm.doc.company, is_company_account: 1 } }),
				},
				{
					fieldname: "method",
					label: __("Method", null, TCS_BT_CTX),
					fieldtype: "Select",
					options: options(["Normal", "Fawran", "SWIFT"], TCS_BT_CTX),
					default: "Normal",
				},
				{
					fieldname: "transfer_type",
					label: __("Transfer Type", null, TCS_BT_CTX),
					fieldtype: "Select",
					options: options(["Party Payment", "Party Receipt", "Internal Transfer"], TCS_BT_CTX),
					default: "Party Payment",
				},
				{ fieldname: "column_break_test", fieldtype: "Column Break" },
				{
					fieldname: "amount",
					label: __("Amount", null, TCS_BT_CTX),
					fieldtype: "Float",
					precision: 2,
					reqd: 1,
				},
				{
					fieldname: "beneficiary_bank",
					label: __("Beneficiary Bank", null, TCS_BT_CTX),
					fieldtype: "Link",
					options: "Bank",
					depends_on: "eval:doc.transfer_type!=='Internal Transfer' && doc.method!=='SWIFT'",
					description: __("Same bank as the bank account → Same Bank scope."),
				},
				{
					fieldname: "to_bank_account",
					label: __("To Bank Account", null, TCS_BT_CTX),
					fieldtype: "Link",
					options: "Bank Account",
					depends_on: "eval:doc.transfer_type==='Internal Transfer'",
					get_query: () => ({ filters: { company: frm.doc.company, is_company_account: 1 } }),
				},
				{
					fieldname: "charge_bearer",
					label: __("Charge Bearer", null, TCS_BT_CTX),
					fieldtype: "Select",
					options: options(["OUR", "SHA", "BEN"], TCS_BT_CTX),
					default: frm.doc.default_charge_bearer || "SHA",
					depends_on: "eval:doc.method==='SWIFT'",
				},
				{ fieldname: "section_result", fieldtype: "Section Break" },
				{ fieldname: "result", fieldtype: "HTML" },
			],
			primary_action_label: __("Calculate"),
			primary_action(values) {
				const internal = values.transfer_type === "Internal Transfer";
				frappe
					.xcall("treasury.bank_transfers.api.get_fee_preview", {
						company: frm.doc.company,
						bank_account: values.bank_account,
						method: values.method,
						transfer_type: values.transfer_type,
						amount: flt(values.amount),
						charge_bearer: values.charge_bearer,
						beneficiary_bank: internal ? null : values.beneficiary_bank,
						to_bank_account: internal ? values.to_bank_account : null,
					})
					.then((r) => {
						// the dialog stays open for the next try
						const scope = r.scope
							? `<span class="indicator-pill ${TCS_SCOPE_COLORS[r.scope] || "gray"}">${esc(
									__(r.scope, null, TCS_BT_CTX)
							  )}</span>`
							: "";
						const messages = (r.limit_messages || [])
							.map((message) => {
								const text = new DOMParser().parseFromString(message, "text/html").body.textContent;
								return `<p class="text-warning">${esc(text || "")}</p>`;
							})
							.join("");
						dialog.get_field("result").$wrapper.html(`<div>
							<p>${scope} <b>${esc(__("Fee", null, TCS_BT_CTX))}:</b>
								<bdi>${esc(format_currency(r.computed_fee, r.currency))}</bdi>
								${r.fee_rule ? `· ${esc(r.fee_rule)}` : ""}</p>
							<p class="text-muted small">${esc(r.fee_basis || "")}</p>
							${messages}
						</div>`);
					});
			},
		});
		dialog.show();
	},

	outgoing_party_posting(frm) {
		if (frm.doc.outgoing_party_posting === "On Creation") {
			frappe.show_alert({
				message: __(
					"Outgoing cheques will be posted to the party as soon as they are created, even if they are never handed over."
				),
				indicator: "orange",
			});
		}
	},

	bank_settlement_voucher(frm) {
		frappe.show_alert({
			message:
				frm.doc.bank_settlement_voucher === "Journal Entry"
					? __(
							"From now on, deposits and bank settlements are posted as Journal Entries (version 1.0 behaviour). Cheques already posted keep their vouchers."
					  )
					: __(
							"From now on, deposits and bank settlements are posted as Payment Entries. Cheques already posted keep their vouchers."
					  ),
			indicator: "orange",
		});
	},

	render_vouchers_help(frm) {
		const field = frm.get_field("vouchers_help");
		if (!field) {
			return;
		}
		const esc = frappe.utils.escape_html;
		field.$wrapper.html(`<div class="text-muted small">
			<p><b>${esc(__("Payment Entry", null, "Treasury Company Settings"))}</b>: ${esc(
			__(
				"One Payment Entry per bank event; bank charges inside the same entry. Foreign-currency cheques settle at their carrying rate."
			)
		)}</p>
			<p><b>${esc(__("Journal Entry", null, "Treasury Company Settings"))}</b>: ${esc(
			__("Version 1.0 behaviour: journal entries, charges in a separate entry, exchange difference booked at clearance.")
		)}</p>
		</div>`);
	},

	mark_unregistered_series(frm) {
		// the Payment Entry series of the map that posting would reject, marked in the grid
		const unregistered = (frm.doc.__onload && frm.doc.__onload.unregistered_series) || [];
		const esc = frappe.utils.escape_html;
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield(TCS_MAP_DOCTYPE, "pe_naming_series", name);
			if (df) {
				df.formatter = (value) => {
					if (!value) {
						return "";
					}
					if (unregistered.includes(value)) {
						return `<span class="indicator-pill orange" title="${esc(
							__("Not a Payment Entry series yet")
						)}"><bdi>${esc(value)}</bdi></span>`;
					}
					return `<bdi>${esc(value)}</bdi>`;
				};
			}
			const method = frappe.meta.get_docfield(TCS_MAP_DOCTYPE, "method", name);
			if (method) {
				method.formatter = (value) => esc(value ? __(value, null, TCS_MAP_DOCTYPE) : "");
			}
		}
		frm.refresh_field("payment_modes");
	},

	set_series_headline(frm) {
		const unregistered = (frm.doc.__onload && frm.doc.__onload.unregistered_series) || [];
		if (frm.is_new() || !unregistered.length) {
			return;
		}
		const esc = frappe.utils.escape_html;
		const codes = unregistered.map((s) => `<code><bdi>${esc(s)}</bdi></code>`).join(", ");
		const system_manager = frappe.user.has_role("System Manager");
		const action = system_manager
			? `<a class="treasury-add-series">${esc(__("Add Series to Payment Entry"))}</a>`
			: esc(__('Ask a System Manager to click "Add Series to Payment Entry".'));
		// the series codes are escaped above; the messages are our own translations
		const $headline = frm.dashboard.set_headline(
			`<div><b>${__("{0} Payment Entry series are not registered yet: {1}", [unregistered.length, codes])}</b><br>
				${esc(__("Posting a cheque on these banks will fail until they are added to Payment Entry."))}<br>
				${action}</div>`,
			"orange"
		);
		$headline && $headline.find(".treasury-add-series").on("click", () => frm.events.add_series(frm));
	},

	add_mode_buttons(frm) {
		if (frm.is_new() || frm.is_dirty()) {
			return;
		}
		const group = __("Modes of Payment", null, "Treasury Company Settings");
		if ((frm.doc.pdc_accounts || []).length && frappe.user.has_role(TCS_GENERATE_ROLES)) {
			frm.add_custom_button(__("Generate Modes of Payment"), () => frm.events.generate_modes(frm), group);
		}
		const unregistered = (frm.doc.__onload && frm.doc.__onload.unregistered_series) || [];
		if (unregistered.length && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Add Series to Payment Entry"), () => frm.events.add_series(frm), group);
		}
	},

	generate_modes(frm) {
		const banks = [...new Set((frm.doc.pdc_accounts || []).map((row) => row.bank_account).filter(Boolean))];
		const names = (frm.doc.__onload && frm.doc.__onload.bank_accounts) || {};
		let dialog = null;
		const update_preview = () => {
			// illustrative only: the server builds the real names (and avoids duplicates)
			if (!dialog) {
				return;
			}
			const method = (dialog.get_value("methods") || [])[0];
			const bank = (dialog.get_value("bank_accounts") || [])[0];
			const esc = frappe.utils.escape_html;
			let example = "";
			if (method && bank) {
				const values = names[bank] || { bank, account: bank };
				example = (frm.doc.mode_name_template || "{method} - {bank}")
					.replace("{method}", __(method, null, TCS_MAP_DOCTYPE))
					.replace("{bank}", values.bank)
					.replace("{account}", values.account);
			}
			dialog.get_field("preview").$wrapper.html(`<p class="text-muted small">
				${example ? esc(__("Example: {0}", [example])) + "<br>" : ""}
				${esc(__("Existing Modes of Payment and map rows are never changed."))}</p>`);
		};
		dialog = new frappe.ui.Dialog({
			title: __("Generate Modes of Payment"),
			fields: [
				{
					fieldname: "methods",
					label: __("Methods", null, "Treasury Company Settings"),
					fieldtype: "MultiCheck",
					columns: 2,
					sort_options: false,
					reqd: 1,
					options: TCS_METHODS.map((method) => ({
						label: __(method, null, TCS_MAP_DOCTYPE),
						value: method,
						checked: TCS_DEFAULT_METHODS.includes(method) ? 1 : 0,
					})),
					on_change: () => update_preview(),
				},
				{
					fieldname: "bank_accounts",
					label: __("Bank Accounts", null, "Treasury Company Settings"),
					fieldtype: "MultiCheck",
					reqd: 1,
					options: banks.map((bank) => ({
						label: (names[bank] && names[bank].account) || bank,
						value: bank,
						checked: 1,
					})),
					on_change: () => update_preview(),
				},
				{ fieldname: "preview", fieldtype: "HTML" },
			],
			primary_action_label: __("Generate", null, "Treasury Company Settings"),
			primary_action(values) {
				const methods = dialog.get_value("methods") || [];
				const bank_accounts = dialog.get_value("bank_accounts") || [];
				if (!methods.length || !bank_accounts.length) {
					frappe.msgprint(__("Select at least one method and one bank account."));
					return;
				}
				dialog.hide();
				frm.call({
					doc: frm.doc,
					method: "generate_modes_of_payment",
					args: { methods, bank_accounts },
					freeze: true,
					freeze_message: __("Generating Modes of Payment…"),
				}).then((r) => {
					frm.events.show_generate_result(frm, r.message || {});
					frm.reload_doc();
				});
			},
		});
		dialog.show();
		update_preview();
	},

	show_generate_result(frm, result) {
		const created = result.created || [];
		const linked = result.linked || [];
		const skipped = result.skipped || [];
		if (!created.length && !linked.length && !skipped.length) {
			frappe.show_alert({ message: __("All Modes of Payment already exist."), indicator: "green" });
			return;
		}
		const esc = frappe.utils.escape_html;
		const link = (name) =>
			`<a href="/app/mode-of-payment/${encodeURIComponent(name)}"><bdi>${esc(name)}</bdi></a>`;
		const block = (title, items) =>
			items.length ? `<p><b>● ${esc(title)} (${items.length})</b></p><ul>${items.join("")}</ul>` : "";
		frappe.msgprint({
			title: __("Modes of Payment", null, "Treasury Company Settings"),
			indicator: skipped.length ? "orange" : "green",
			message:
				block(
					__("Created", null, "Treasury Company Settings"),
					created.map((name) => `<li>${link(name)}</li>`)
				) +
				block(
					__("Linked to this company"),
					linked.map((name) => `<li>${link(name)}</li>`)
				) +
				block(
					__("Skipped", null, "Treasury Company Settings"),
					skipped.map((d) => `<li><bdi>${esc(d.mode)}</bdi> — ${esc(d.reason || "")}</li>`)
				),
		});
	},

	add_series(frm) {
		const unregistered = (frm.doc.__onload && frm.doc.__onload.unregistered_series) || [];
		if (!unregistered.length) {
			return;
		}
		const esc = frappe.utils.escape_html;
		const default_series = (frm.doc.__onload && frm.doc.__onload.pe_default_series) || "";
		const list = unregistered.map((s) => `<li><code><bdi>${esc(s)}</bdi></code></li>`).join("");
		const dialog = frappe.confirm(
			`<p>${esc(
				__("These series will be added to the Series list of Payment Entry for all companies and users:")
			)}</p>
			<ul>${list}</ul>
			<p>${__(
				"The current default series ({0}) stays first and stays the default. You can remove them later in Document Naming Settings.",
				[`<code><bdi>${esc(default_series)}</bdi></code>`]
			)}</p>`,
			() => {
				frm.call({
					doc: frm.doc,
					method: "add_series_to_payment_entry",
					args: { confirm: 1 },
					freeze: true,
				}).then((r) => {
					const added = r.message || [];
					if (added.length) {
						frappe.show_alert({
							message: __("Added {0} series to Payment Entry.", [added.length]),
							indicator: "green",
						});
					}
					frm.reload_doc();
				});
			}
		);
		dialog && dialog.set_title && dialog.set_title(__("Add Series to Payment Entry"));
	},
});

// Tiered fee rules are typed as a sequence of rows: a new row continues the previous one.
frappe.ui.form.on(TCS_FEE_RULE, {
	transfer_fee_rules_add(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const previous = (frm.doc.transfer_fee_rules || []).find((r) => r.idx === row.idx - 1);
		if (!previous) {
			return;
		}
		const values = {};
		// the expense account first: the bank account trigger only fills an empty one
		["expense_account", "bank_account", "method", "direction", "scope", "charge_bearer"].forEach((fieldname) => {
			if (previous[fieldname]) {
				values[fieldname] = previous[fieldname];
			}
		});
		if (flt(previous.amount_to) > 0) {
			values.amount_from = flt(previous.amount_to) + 0.01;
		}
		frappe.model.set_value(cdt, cdn, values);
	},

	bank_account(frm, cdt, cdn) {
		// mirrors TreasuryCompanySettings.default_fee_expense_account
		const row = locals[cdt][cdn];
		if (row.expense_account || !row.bank_account) {
			return;
		}
		const mapping = (frm.doc.pdc_accounts || []).find(
			(r) => r.bank_account === row.bank_account && r.bank_charges_account
		);
		const account = (mapping && mapping.bank_charges_account) || frm.doc.bounce_charges_account;
		if (account) {
			frappe.model.set_value(cdt, cdn, "expense_account", account);
		}
	},
});

frappe.ui.form.on(TCS_LIMIT, {
	transfer_limits_add(frm, cdt, cdn) {
		// limits are mostly for Fawran (the JSON default method)
		if (frm.doc.default_fawran_bank_account && !locals[cdt][cdn].bank_account) {
			frappe.model.set_value(cdt, cdn, "bank_account", frm.doc.default_fawran_bank_account);
		}
	},
});

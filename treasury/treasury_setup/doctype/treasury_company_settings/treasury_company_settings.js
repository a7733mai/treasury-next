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
const TCS_THRESHOLD = "Treasury Balance Threshold";
const TCS_ALERT_ROLES = ["Treasury Manager", "System Manager"];
const TCS_ALERTS_METHOD = "treasury.balance_alerts.notify.";

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
		// Pay / receive from documents: banks with a PDC Accounts row (mirrors validate_document_settings);
		// without any row, every company bank is offered and the server message explains the rule
		["default_outgoing_cheque_bank_account", "default_incoming_cheque_bank_account"].forEach((fieldname) =>
			frm.set_query(fieldname, () => {
				const filters = { company: frm.doc.company, is_company_account: 1 };
				const mapped = (frm.doc.pdc_accounts || []).map((row) => row.bank_account).filter(Boolean);
				if (mapped.length) {
					filters.name = ["in", mapped];
				}
				return { filters };
			})
		);
		frm.set_query("bank_account", "transfer_fee_rules", company_bank);
		frm.set_query("bank_account", "transfer_limits", company_bank);
		// Bank Balance Alerts: any company bank account, with or without a PDC Accounts row
		frm.set_query("bank_account", "balance_thresholds", company_bank);
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
		frm.events.render_balance_alerts_help(frm);
		tcs_format_thresholds(frm);
		tcs_threshold_buttons(frm);
		tcs_thresholds_headline(frm);
		tcs_add_alert_buttons(frm);
		tcs_render_setup_checklist(frm);
		tcs_render_naming_series(frm);
		tcs_checklist_headline(frm);
	},

	render_balance_alerts_help(frm) {
		const field = frm.get_field("balance_alerts_help");
		if (!field) {
			return;
		}
		const esc = frappe.utils.escape_html;
		const bold = (label) => `<b>${esc(label)}</b>`;
		// the labels are escaped; the sentence is our own translation
		field.$wrapper.html(`<div class="text-muted small"><b>${esc(__("How it works:"))}</b> ${__(
			"Each bank's balance is projected day by day for the horizon (Treasury Settings), from its GL balance plus expected cheques, transfers and installments. Below the {0} the day turns orange; below the {1} it turns red and entries that would cause it are checked. Amounts are in the bank account currency. Warning = Minimum means one level only.",
			[bold(__("Warning Balance", null, TCS_THRESHOLD)), bold(__("Minimum Balance", null, TCS_THRESHOLD))]
		)}</div>`);
	},

	on_low_balance(frm) {
		const messages = {
			Block: __(
				"Entries that would take a bank below its minimum balance will be refused. Only a Treasury Manager can override."
			),
			Ignore: __("Entries are not checked. The daily digest and alerts still run."),
		};
		const message = messages[frm.doc.on_low_balance];
		if (message) {
			frappe.show_alert({ message: frappe.utils.escape_html(message), indicator: "orange" });
		}
	},

	auto_select_cheque_book(frm) {
		if (!frm.doc.auto_select_cheque_book) {
			frappe.show_alert({
				message: __("Outgoing cheques created from documents will need a cheque book chosen by hand."),
				indicator: "blue",
			});
		}
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

	bank_charge_voucher(frm) {
		if (frm.doc.bank_charge_voucher === "Journal Entry") {
			frappe.show_alert({
				message: __(
					"New bank charges will be booked by Journal Entry (Bank Entry), without a Payment Entry number."
				),
				indicator: "orange",
			});
		}
	},

	bank_charge_clearance(frm) {
		if (frm.doc.bank_charge_clearance === "On Bank Transaction") {
			frappe.show_alert({
				message: __("New bank charges stay uncleared until they are matched in PDC Bank Clearing."),
				indicator: "orange",
			});
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

// ------------------------------------------------------------------------------ Bank Balance Alerts
// Levels, thresholds and checks are server rules (treasury/balance_alerts); the grid only helps typing:
// one row per bank, usually just the minimum.

frappe.ui.form.on(TCS_THRESHOLD, {
	balance_thresholds_add(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, { warning_balance: 0, minimum_balance: 0 });
	},

	bank_account(frm, cdt, cdn) {
		// a preview only: the server sets the currency of the GL account on save
		const row = locals[cdt][cdn];
		if (!row.bank_account || frm.is_new()) {
			return;
		}
		tcs_threshold_banks(frm).then((banks) => {
			const bank = banks.find((b) => b.bank_account === row.bank_account);
			bank && bank.currency && frappe.model.set_value(cdt, cdn, "currency", bank.currency);
		});
	},

	minimum_balance(frm, cdt, cdn) {
		// the user usually types only the minimum: the warning follows until it is raised
		const row = locals[cdt][cdn];
		if (!flt(row.warning_balance) || flt(row.warning_balance) < flt(row.minimum_balance)) {
			frappe.model.set_value(cdt, cdn, "warning_balance", flt(row.minimum_balance));
		}
	},
});

function tcs_threshold_banks(frm) {
	// the company's bank accounts with their currency, read once per form load (Treasury users need not
	// read Bank Account or Account)
	if (!frm._tcs_threshold_banks || frm._tcs_threshold_company !== frm.doc.company) {
		frm._tcs_threshold_company = frm.doc.company;
		frm._tcs_threshold_banks = frm.call({ doc: frm.doc, method: "get_threshold_banks" }).then((r) => r.message || []);
	}
	return frm._tcs_threshold_banks;
}

function tcs_format_thresholds(frm) {
	const esc = frappe.utils.escape_html;
	const amount = (value, row) => {
		const text = `<bdi>${esc(format_currency(value, row && row.currency))}</bdi>`;
		return flt(value) < 0 ? `<span title="${esc(__("Overdraft"))}">${text}</span>` : text;
	};
	const formatters = {
		currency: (value) => `<bdi>${esc(value || "")}</bdi>`,
		minimum_balance: (value, df, options, row) => amount(value, row),
		warning_balance: (value, df, options, row) =>
			row && flt(value) === flt(row.minimum_balance)
				? `<span class="text-muted" title="${esc(__("No separate warning level"))}">${esc(
						__("= minimum")
				  )}</span>`
				: amount(value, row),
	};
	Object.entries(formatters).forEach(([fieldname, formatter]) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield(TCS_THRESHOLD, fieldname, name);
			if (df) {
				df.formatter = formatter;
			}
		}
	});
	frm.refresh_field("balance_thresholds");
}

function tcs_threshold_buttons(frm) {
	const grid = frm.fields_dict.balance_thresholds && frm.fields_dict.balance_thresholds.grid;
	if (!grid) {
		return;
	}
	// the grid keeps its buttons (by label) across refreshes: show or hide them
	const can_write = !!(frm.perm[0] && frm.perm[0].write);
	grid.add_custom_button(__("Add All Bank Accounts"), () => tcs_add_all_banks(frm), "top").toggleClass(
		"hidden",
		frm.is_new() || !can_write
	);
	grid.add_custom_button(
		__("Open Bank Balance Outlook"),
		() => frappe.set_route("query-report", "Bank Balance Outlook", { company: frm.doc.company }),
		"top"
	).toggleClass("hidden", frm.is_new() || frm.is_dirty());
}

function tcs_add_all_banks(frm) {
	// design 7e P3: one row (0 / 0) per company bank account that has none yet
	tcs_threshold_banks(frm).then((banks) => {
		const present = new Set((frm.doc.balance_thresholds || []).map((row) => row.bank_account));
		const missing = banks.filter((b) => !present.has(b.bank_account));
		if (!missing.length) {
			frappe.show_alert({ message: __("Every bank account already has a row."), indicator: "blue" });
			return;
		}
		missing.forEach((bank) => {
			const row = frm.add_child("balance_thresholds");
			Object.assign(row, {
				bank_account: bank.bank_account,
				currency: bank.currency,
				warning_balance: 0,
				minimum_balance: 0,
			});
		});
		frm.refresh_field("balance_thresholds");
		frm.dirty();
		frappe.show_alert({
			message: __("{0} bank account(s) added. Enter their minimum balances.", [missing.length]),
			indicator: "green",
		});
	});
}

function tcs_thresholds_headline(frm) {
	// design 7e P8: after an upgrade nothing is alerted until thresholds exist
	if (frm.is_new() || frm.is_dirty() || (frm.doc.balance_thresholds || []).length) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const can_write = !!(frm.perm[0] && frm.perm[0].write);
	const add = can_write ? ` <a class="treasury-add-thresholds">${esc(__("Add All Bank Accounts"))} →</a>` : "";
	const $headline = frm.dashboard.set_headline(
		`<div>${esc(
			__("No balance thresholds yet: banks are listed in the digest but never alerted or checked.")
		)}${add}</div>`,
		"blue"
	);
	$headline &&
		$headline.find(".treasury-add-thresholds").on("click", () => {
			frm.scroll_to_field("balance_thresholds");
			tcs_add_all_banks(frm);
		});
}

// ---------------------------------------------------------------- Setup Checklist (spec 7h §3.4)

// Design 7h §3.1: one row per item in setup order (the list doubles as a guide). The checklist reflects the saved
// document (__onload): it is evaluated again on save / reload.
const TCS_MAX_MISSING = 5;
const TCS_OK = "OK";
const TCS_WARNING = "Warning";
const TCS_MISSING = "Missing";
const TCS_REQUIRED = "Required";

function tcs_arrow() {
	return frappe.utils.is_rtl() ? "←" : "→";
}

function tcs_can_write(frm) {
	return !!(frm.perm[0] && frm.perm[0].write);
}

function tcs_checklist_items(frm) {
	return (frm.doc.__onload && frm.doc.__onload.setup_checklist) || [];
}

function tcs_status_pill(item) {
	// a status always has a colour and a word
	const pills = {
		[TCS_OK]: ["green", __("Done", null, TCS_CTX)],
		[TCS_WARNING]: ["orange", __("Check", null, TCS_CTX)],
	};
	const [color, word] =
		pills[item.status] || [item.level === TCS_REQUIRED ? "red" : "orange", __("Missing", null, TCS_CTX)];
	return `<span class="indicator-pill ${color} no-indicator-dot" style="min-inline-size: 72px; justify-content: center;">${frappe.utils.escape_html(
		word
	)}</span>`;
}

function tcs_missing_text(item) {
	const esc = frappe.utils.escape_html;
	if (!(item.missing || []).length) {
		return "";
	}
	let names = item.missing.slice(0, TCS_MAX_MISSING).join(", ");
	if (item.missing.length > TCS_MAX_MISSING) {
		names += " " + __("and {0} more", [item.missing.length - TCS_MAX_MISSING]);
	}
	// the default banks item lists field labels, the others list banks / categories / series
	const text =
		item.key === "default_banks"
			? __("Missing: {0}", [names], TCS_CTX)
			: __("Missing for: {0}", [names], TCS_CTX);
	return `<div class="small">${esc(text)}</div>`;
}

function tcs_checklist_row(frm, item, index) {
	const esc = frappe.utils.escape_html;
	if (item.status === TCS_OK) {
		// done rows are compact: one muted line
		return `<div class="tcs-check-row" style="display: flex; gap: var(--margin-sm, 8px); align-items: center; padding-block: 4px;">
			${tcs_status_pill(item)}<span class="text-muted">${esc(item.label)}</span></div>`;
	}
	const level =
		item.level === TCS_REQUIRED
			? `<span class="indicator-pill red no-indicator-dot" style="background: transparent; border: 1px solid var(--red-500);">${esc(
					__("Required", null, TCS_CTX)
			  )}</span>`
			: `<span class="text-muted small">${esc(__("Recommended", null, TCS_CTX))}</span>`;
	const actions = [];
	if (item.quick_fill && tcs_can_write(frm)) {
		actions.push(
			`<button type="button" class="btn btn-xs btn-default tcs-quick-fill" data-index="${index}">${esc(
				__("Use {0}", [item.quick_fill])
			)}</button>`
		);
	}
	if (item.key === "balance_thresholds" && tcs_can_write(frm)) {
		actions.push(
			`<button type="button" class="btn btn-xs btn-default tcs-add-thresholds">${esc(
				__("Add All Bank Accounts")
			)}</button>`
		);
	}
	actions.push(
		`<a href="#" class="tcs-fix" data-index="${index}">${esc(__("Fix", null, TCS_CTX))} ${tcs_arrow()}</a>`
	);
	return `<div class="tcs-check-row" style="display: flex; gap: var(--margin-sm, 8px); align-items: flex-start; padding-block: 8px; border-block-start: 1px solid var(--border-color);">
		<div>${tcs_status_pill(item)}</div>
		<div style="flex: 1; min-inline-size: 0;">
			<div><b>${esc(item.label)}</b> ${level}</div>
			${tcs_missing_text(item)}
			${item.detail ? `<div class="text-muted small">${esc(item.detail)}</div>` : ""}
		</div>
		<div style="display: flex; gap: var(--margin-sm, 8px); align-items: center; flex-wrap: wrap; justify-content: flex-end;">${actions.join(
			""
		)}</div>
	</div>`;
}

function tcs_checklist_progress(items) {
	const esc = frappe.utils.escape_html;
	const done = items.filter((i) => i.status === TCS_OK).length;
	const required = items.filter((i) => i.level === TCS_REQUIRED && i.status === TCS_MISSING).length;
	const color = required ? "red" : done < items.length ? "orange" : "green";
	const percent = items.length ? Math.round((done * 100) / items.length) : 0;
	const parts = [__("{0} of {1} done", [done, items.length], TCS_CTX)];
	if (required) {
		parts.push(__("{0} required step(s) missing", [required], TCS_CTX));
	}
	return `<div style="display: flex; gap: var(--margin-md, 12px); align-items: center; flex-wrap: wrap; margin-block-end: 8px;">
		<b>${esc(__("Setup progress", null, TCS_CTX))}</b>
		<div style="flex: 1; min-inline-size: 120px; max-inline-size: 320px; block-size: 8px; border-radius: 4px; background: var(--gray-200); overflow: hidden;">
			<div style="inline-size: ${percent}%; block-size: 100%; background: var(--${color}-500);"></div>
		</div>
		<span class="small">${esc(parts.join(" · "))}</span>
	</div>`;
}

function tcs_render_setup_checklist(frm) {
	const field = frm.get_field("setup_checklist");
	if (!field) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const items = tcs_checklist_items(frm);
	tcs_checklist_chip(frm, items);
	if (frm.is_new() || !items.length) {
		field.$wrapper.html(
			`<div class="text-muted small">${esc(__("Save the settings to see the Setup Checklist."))}</div>`
		);
		return;
	}
	// shown as soon as the form turns dirty (the checklist reflects the saved document)
	const stale = `<div class="tcs-stale text-muted small ${frm.is_dirty() ? "" : "hidden"}" style="margin-block-end: 8px;">${esc(
		__("Save to refresh the checklist.")
	)}</div>`;
	if (!frm._tcs_dirty_bound) {
		frm._tcs_dirty_bound = true;
		$(frm.wrapper).on("dirty", () => $(frm.wrapper).find(".tcs-stale").removeClass("hidden"));
	}
	const complete = items.every((i) => i.status === TCS_OK)
		? `<div style="color: var(--green-600); margin-block-end: 8px;"><a href="#" class="tcs-open-dashboard" style="color: inherit;">${esc(
				__("Setup complete. Open the Treasury Dashboard")
		  )} ${tcs_arrow()}</a></div>`
		: "";
	field.$wrapper.html(`<div class="tcs-setup-checklist">
		${tcs_checklist_progress(items)}${stale}${complete}
		${items.map((item, index) => tcs_checklist_row(frm, item, index)).join("")}
	</div>`);
	const item_of = (e) => items[cint($(e.currentTarget).attr("data-index"))];
	field.$wrapper.find(".tcs-fix").on("click", (e) => {
		e.preventDefault();
		tcs_fix(frm, item_of(e));
	});
	field.$wrapper.find(".tcs-quick-fill").on("click", (e) => {
		const item = item_of(e);
		(item.empty_fields || []).forEach((fieldname) => frm.set_value(fieldname, item.quick_fill));
		frappe.show_alert({ message: esc(__("Defaults set: save to apply.")), indicator: "blue" });
	});
	field.$wrapper.find(".tcs-add-thresholds").on("click", () => {
		// design 7h P21
		frm.scroll_to_field("balance_thresholds");
		tcs_add_all_banks(frm);
	});
	field.$wrapper.find(".tcs-open-dashboard").on("click", (e) => {
		e.preventDefault();
		frappe.route_options = { company: frm.doc.company };
		frappe.set_route("treasury-dashboard");
	});
}

function tcs_fix(frm, item) {
	const action = (item && item.action) || {};
	if (!action.route) {
		action.field && frm.scroll_to_field(action.field);
		return;
	}
	// a field of the target form: its refresh scrolls to route_options.scroll_to (form.js scroll_to_element)
	const route_options = Object.assign(
		{},
		action.route_options || {},
		action.field ? { scroll_to: action.field } : {}
	);
	// Frappe 16 drops or mis-parses a trailing options object on Form routes: use frappe.route_options
	const go = () => {
		frappe.route_options = route_options;
		frappe.set_route(...action.route);
	};
	if (!frm.is_dirty()) {
		go();
		return;
	}
	// design 7h P20: no lost edits
	const target = __(action.route[1] || "");
	frappe.confirm(
		frappe.utils.escape_html(__("Discard unsaved changes and open {0}?", [target], TCS_CTX)),
		go
	);
}

function tcs_checklist_chip(frm, items) {
	// design 7h P18: the state stays visible when the section is collapsed
	const section = frm.fields_dict.setup_checklist_section;
	if (!section || !section.head) {
		return;
	}
	section.head.find(".tcs-checklist-chip").remove();
	if (frm.is_new() || !items.length) {
		return;
	}
	const done = items.filter((i) => i.status === TCS_OK).length;
	const color = done === items.length ? "green" : "orange";
	$(
		`<span class="tcs-checklist-chip indicator-pill ${color} no-indicator-dot" style="margin-inline-start: 8px;"></span>`
	)
		.text(`${done === items.length ? "✓ " : ""}${__("{0} of {1} done", [done, items.length], TCS_CTX)}`)
		.insertBefore(section.head.find(".collapse-indicator"));
}

function tcs_checklist_headline(frm) {
	if (frm.is_new() || frm.is_dirty()) {
		return;
	}
	const missing = tcs_checklist_items(frm).filter((i) => i.level === TCS_REQUIRED && i.status === TCS_MISSING);
	if (!missing.length) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const $headline = frm.dashboard.set_headline(
		`<div>${esc(__("Setup incomplete: {0}.", [missing.map((i) => i.label).join(", ")]))}
		<a class="tcs-open-checklist">${esc(__("Open the checklist"))} ↓</a></div>`,
		"red"
	);
	$headline && $headline.find(".tcs-open-checklist").on("click", () => frm.scroll_to_field("setup_checklist"));
}

// ---------------------------------------------------------------- Naming Series (spec 7h §3.5)

// the series field is visible and required on these forms: the user picks, the first option is the default
const TCS_USER_SERIES = ["Bank Transfer", "Bank Charge"];
// naming that no setting changes (spec 7h §2.4)
const TCS_FIXED_NAMING = [
	"Cheque Layout",
	"Bank Charge Category",
	"Custody Location",
	"Treasury Company Settings",
	"Treasury Alert Log",
];

function tcs_open_naming_settings(doctype) {
	// route_options only fill new documents, and the Single form never reads them: a one-shot refresh handler
	// selects the doctype instead (bound once; the pending value is cleared when used)
	frappe.provide("treasury");
	treasury.tcs_naming_doctype = doctype;
	if (!treasury.tcs_naming_handler_bound) {
		treasury.tcs_naming_handler_bound = true;
		frappe.ui.form.on("Document Naming Settings", "refresh", (dns_frm) => {
			const pending = treasury.tcs_naming_doctype;
			treasury.tcs_naming_doctype = null;
			if (pending && dns_frm.doc.transaction_type !== pending) {
				dns_frm.set_value("transaction_type", pending);
			}
		});
	}
	frappe.set_route("Form", "Document Naming Settings");
}

function tcs_render_naming_series(frm) {
	const field = frm.get_field("naming_series_help");
	if (!field) {
		return;
	}
	const rows = (frm.doc.__onload && frm.doc.__onload.naming_series) || [];
	const esc = frappe.utils.escape_html;
	const can_settings = frappe.model.can_write("Document Naming Settings");
	const can_rule = frappe.model.can_create("Document Naming Rule");
	const code = (series) => `<code><bdi>${esc(series || "")}</bdi></code>`;
	const series_cell = (row) => {
		// the default in bold, the other options muted below it
		const defaults =
			typeof row.default === "object" && row.default
				? Object.entries(row.default).map(
						([direction, series]) => `<b>${esc(__(direction))}: ${code(series)}</b>`
				  )
				: [`<b>${code(row.default)}</b>`];
		const shown = typeof row.default === "object" && row.default ? Object.values(row.default) : [row.default];
		const others = (row.options || [])
			.filter((option) => !shown.includes(option))
			.map((option) => `<div class="text-muted small">${code(option)}</div>`);
		return defaults.join("<br>") + others.join("");
	};
	const chosen = (row) => {
		if (row.forced_by) {
			return __("By direction", null, TCS_CTX);
		}
		return TCS_USER_SERIES.includes(row.doctype)
			? __("Chosen by the user (first option by default)", null, TCS_CTX)
			: __("First option", null, TCS_CTX);
	};
	const rules_cell = (row) =>
		(row.rules || [])
			.map((r) => {
				const style = r.disabled ? ' style="text-decoration: line-through;"' : "";
				const disabled = r.disabled
					? ` <span class="indicator-pill gray no-indicator-dot">${esc(__("Disabled", null, TCS_CTX))}</span>`
					: "";
				return `<div class="small"><a href="/app/document-naming-rule/${encodeURIComponent(
					r.name
				)}"${style}>${code(r.prefix)}</a> <span class="text-muted"${style}>${esc(r.conditions || "")} · #${esc(
					r.counter || 0
				)}</span>${disabled}</div>`;
			})
			.join("") || `<span class="text-muted small">—</span>`;
	const body = rows.map((row, index) => {
		const buttons = [
			// design 7h P22: re-ordering the options has no effect on the direction-forced series
			!row.forced_by && can_settings
				? `<button type="button" class="btn btn-xs btn-default tcs-naming-settings" data-index="${index}">${esc(
						__("Document Naming Settings", null, TCS_CTX)
				  )}</button>`
				: "",
			can_rule
				? `<button type="button" class="btn btn-xs btn-default tcs-naming-rule" data-index="${index}">${esc(
						__("Add Rule per Bank")
				  )}</button>`
				: "",
		]
			.filter(Boolean)
			.join(" ");
		return `<tr><td>${esc(__(row.doctype))}</td><td>${series_cell(row)}</td><td>${esc(chosen(row))}</td>
			<td>${rules_cell(row)}</td><td style="white-space: nowrap;">${buttons}</td></tr>`;
	});
	body.push(`<tr><td>${esc(__("Payment Entry", null, TCS_CTX))}</td>
		<td colspan="3">${esc(__("Per bank and method in Modes of Payment", null, TCS_CTX))}</td>
		<td><a href="#" class="tcs-naming-modes">${esc(__("Fix", null, TCS_CTX))} ${tcs_arrow()}</a></td></tr>`);
	const bold = (label) => `<b>${esc(label)}</b>`;
	const fixed = TCS_FIXED_NAMING.map((doctype) => __(doctype)).join(", ");
	// the labels are escaped; the sentence is our own translation
	field.$wrapper.html(`<p class="text-muted small">${__(
		"Treasury documents are numbered by their naming series. To change a series, use {0}. To number differently per bank (or per direction), add a {1}: a rule overrides the series, rules are matched highest priority first, and a rule's counter does not restart each year. Payment Entry series are set per bank and method in {2} above.",
		[
			bold(__("Document Naming Settings", null, TCS_CTX)),
			bold(__("Document Naming Rule", null, TCS_CTX)),
			bold(__("Modes of Payment", null, TCS_CTX)),
		]
	)}</p>
		<div style="overflow-x: auto;"><table class="table table-sm"><thead><tr>
			<th>${esc(__("Document", null, TCS_CTX))}</th>
			<th>${esc(__("Current series", null, TCS_CTX))}</th>
			<th>${esc(__("How it's chosen", null, TCS_CTX))}</th>
			<th>${esc(__("Per-bank rules", null, TCS_CTX))}</th><th></th>
		</tr></thead><tbody>${body.join("")}</tbody></table></div>
		<p class="text-muted small">${esc(__("These names cannot be changed: {0}", [fixed], TCS_CTX))}</p>`);
	field.$wrapper.find(".tcs-naming-modes").on("click", (e) => {
		e.preventDefault();
		frm.scroll_to_field("payment_modes");
	});
	field.$wrapper.find(".tcs-naming-settings").on("click", (e) => {
		const row = rows[cint($(e.currentTarget).attr("data-index"))];
		tcs_open_naming_settings(row.doctype);
	});
	field.$wrapper.find(".tcs-naming-rule").on("click", (e) => {
		const row = rows[cint($(e.currentTarget).attr("data-index"))];
		frappe.model.with_doctype("Document Naming Rule", () => {
			const rule = frappe.model.get_new_doc("Document Naming Rule");
			rule.document_type = row.doctype;
			const first = row.options[0] || "";
			rule.prefix = `${first.split("-")[0] || "SER"}-.YYYY.-`;
			const conditions = [{ field: row.bank_field, condition: "=", value: "" }];
			if (row.forced_by) {
				conditions.push({ field: row.forced_by, condition: "=", value: "Outgoing" });
			}
			conditions.forEach((values) => Object.assign(frappe.model.add_child(rule, "conditions"), values));
			frappe.set_route("Form", "Document Naming Rule", rule.name);
		});
	});
}

function tcs_add_alert_buttons(frm) {
	// design 7e P1, for this company: see what a user gets, send it to yourself, check the alerts now
	if (frm.is_new() || frm.is_dirty() || !frappe.user.has_role(TCS_ALERT_ROLES)) {
		return;
	}
	const esc = frappe.utils.escape_html;
	const company = frm.doc.company;
	const group = __("Bank Balance Alerts", null, TCS_CTX);
	frm.add_custom_button(
		__("Preview Digest"),
		() => {
			const dialog = new frappe.ui.Dialog({
				title: __("Preview Digest"),
				fields: [
					{
						fieldname: "user",
						label: __("User"),
						fieldtype: "Link",
						options: "User",
						reqd: 1,
						default: frappe.session.user,
						get_query: () => ({ filters: { enabled: 1, user_type: "System User" } }),
						description: __(
							"Shows exactly what this user would receive: only the banks they may see, in their language."
						),
					},
				],
				primary_action_label: __("Preview Digest"),
				primary_action(values) {
					dialog.hide();
					tcs_show_digest(company, values.user);
				},
			});
			dialog.show();
		},
		group
	);
	frm.add_custom_button(
		__("Send Test to Me"),
		() =>
			frappe.xcall(TCS_ALERTS_METHOD + "send_test_digest", { company }).then((r) => {
				if (r.sent) {
					frappe.show_alert({ message: esc(r.message), indicator: "green" });
				} else {
					frappe.msgprint({ message: esc(r.message || ""), indicator: "orange" });
				}
			}),
		group
	);
	frm.add_custom_button(
		__("Run Alerts Now"),
		() =>
			frappe.confirm(
				esc(
					__(
						"Evaluates the thresholds now and sends any new critical or warning alert. Alerts already sent today are not repeated."
					)
				),
				() =>
					frappe.xcall(TCS_ALERTS_METHOD + "run_alerts_now", { company }).then((r) => {
						frappe.show_alert({ message: esc(r.message), indicator: "blue" });
					})
			),
		group
	);
}

function tcs_show_digest(company, user) {
	const esc = frappe.utils.escape_html;
	frappe.xcall(TCS_ALERTS_METHOD + "preview_digest", { company, user }).then((r) => {
		if (!r.html) {
			frappe.msgprint({ message: esc(r.message || ""), indicator: "orange" });
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Digest preview for {0} ({1})", [esc(r.user), esc(company)]),
			size: "extra-large",
			fields: [{ fieldname: "preview", fieldtype: "HTML" }],
		});
		// a sandboxed frame: the e-mail's inline styles stay out of the desk, its links open a new tab
		const $frame = $(
			'<iframe sandbox="allow-popups allow-popups-to-escape-sandbox" style="width: 100%; height: 70vh; border: 0;"></iframe>'
		);
		$frame.attr("srcdoc", `<base target="_blank">${r.html}`);
		dialog
			.get_field("preview")
			.$wrapper.empty()
			.append(`<p class="text-muted small"><b>${esc(r.subject || "")}</b></p>`)
			.append($frame);
		dialog.show();
	});
}

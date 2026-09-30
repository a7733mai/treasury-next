// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_transfers (fees.py, lifecycle.py) and the controller; this script only
// mirrors them. The fee preview (treasury.bank_transfers.api.get_fee_preview) announces what the server
// computes again on save: the fee, the limits and the approval rule never block where the server would not.
// Form scripts share the global scope: every name below is prefixed `bt_` / `BT_`.
const BT_CTX = "Bank Transfer";
const BT_LOG_CTX = "Bank Transfer Status Log";
const BT_API = "treasury.bank_transfers.api.";
const BT_USER_ROLES = ["Treasury User", "Treasury Manager", "System Manager"];
const BT_MANAGER_ROLES = ["Treasury Manager", "System Manager"];
const BT_PAYMENT = "Party Payment";
const BT_RECEIPT = "Party Receipt";
const BT_INTERNAL = "Internal Transfer";
const BT_SWIFT = "SWIFT";
const BT_INDICATORS = {
	Draft: "gray",
	Scheduled: "purple",
	Sent: "blue",
	Completed: "green",
	Returned: "red",
	Cancelled: "darkgrey",
};
const BT_SCOPE_COLORS = { "Same Bank": "green", "Other Bank": "blue", International: "purple" };
// the beneficiary's bank details, defaulted from the party's bank account
const BT_BENEFICIARY_FIELDS = ["party_bank_account", "beneficiary_iban", "beneficiary_bank", "beneficiary_swift"];
// fields whose change may change the fee, the limits or the approval: the preview runs again (debounced)
const BT_PREVIEW_FIELDS = [
	"amount",
	"to_bank_account",
	"charge_bearer",
	"beneficiary_bank",
	"beneficiary_swift",
	"exchange_rate",
	"received_amount",
];

// mirrors treasury.utils.references.reference_rules: invoices / orders only for a supplier's payment or a
// customer's receipt; any transfer may settle a Journal Entry of its party
function bt_reference_types(transfer_type, party_type) {
	if (transfer_type === BT_PAYMENT && party_type === "Supplier") {
		return ["Purchase Invoice", "Purchase Order", "Journal Entry"];
	}
	if (transfer_type === BT_RECEIPT && party_type === "Customer") {
		return ["Sales Invoice", "Sales Order", "Journal Entry"];
	}
	return ["Journal Entry"];
}

const bt_handlers = {
	setup(frm) {
		bt_setup_queries(frm);
		frm._bt_preview_later = frappe.utils.debounce(() => bt_preview(frm), 300);
	},

	onload(frm) {
		frm._bt_settings = (frm.doc.__onload && frm.doc.__onload.transfer_settings) || null;
		frm._bt_method = frm.doc.method;
		if (!frm.is_new() || frm.doc.docstatus !== 0) {
			return;
		}
		if (!frm.doc.company) {
			// the company trigger loads the settings and applies the defaults
			const company = frappe.defaults.get_user_default("Company");
			company && frm.set_value("company", company);
			return;
		}
		bt_load_settings(frm).then(() => bt_apply_defaults(frm, true));
	},

	refresh(frm) {
		frm._bt_method = frm.doc.method;
		if (!frm._bt_settings && frm.doc.__onload && frm.doc.__onload.transfer_settings) {
			frm._bt_settings = frm.doc.__onload.transfer_settings;
		}
		bt_set_labels(frm);
		bt_translate_log(frm);
		bt_render_vouchers(frm);
		bt_headline(frm, null);
		frm.set_intro();
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		if (frm.doc.docstatus === 0) {
			frm._bt_result = null;
			bt_render_all(frm);
			// a saved, unchanged draft only shows what the server computed; a new or edited one is updated
			if (frm.has_perm("write") || frm.is_new()) {
				bt_preview(frm, frm.is_new() || frm.is_dirty());
			}
		} else {
			bt_render_fee_strip(frm);
			bt_set_submitted_headline(frm);
			bt_set_indicator(frm);
			if (!frm.is_dirty()) {
				bt_add_actions(frm);
				bt_add_view_buttons(frm);
			}
		}
		if (!frm.is_new()) {
			bt_add_print_button(frm);
		}
	},

	company(frm) {
		if (frm.doc.docstatus !== 0) {
			return;
		}
		bt_load_settings(frm)
			.then(() => bt_set_values(frm, { bank_account: "", to_bank_account: "", fee_account: "" }))
			.then(() => bt_apply_defaults(frm, frm.is_new()));
	},

	transfer_type(frm) {
		if (frm._bt_applying) {
			return;
		}
		const doc = frm.doc;
		const values = {};
		const clear_party = () =>
			["party_name", "contact_person", ...BT_BENEFICIARY_FIELDS].forEach((f) => (values[f] = ""));
		if (doc.transfer_type === BT_INTERNAL) {
			values.party_type = "";
			values.party = "";
			clear_party();
		} else {
			values.to_bank_account = "";
			// an Employee / Shareholder ... the user picked is kept; only the Customer / Supplier default follows
			const party_type = doc.transfer_type === BT_RECEIPT ? "Customer" : "Supplier";
			if ((!doc.party_type || ["Customer", "Supplier"].includes(doc.party_type)) && doc.party_type !== party_type) {
				values.party_type = party_type;
				values.party = "";
				clear_party();
			}
		}
		if ((doc.references || []).length) {
			frm.clear_table("references");
			frm.refresh_field("references");
		}
		frm._bt_allocated = 0;
		bt_set_values(frm, values).then(() => {
			bt_set_labels(frm);
			bt_preview(frm);
		});
	},

	method(frm) {
		const previous = frm._bt_method;
		frm._bt_method = frm.doc.method;
		if (frm._bt_applying) {
			return;
		}
		const values = {};
		// the bank follows the method until the user chose one himself
		if (!frm.doc.bank_account || frm.doc.bank_account === bt_default_bank(frm, previous)) {
			const bank = bt_default_bank(frm, frm.doc.method);
			if (bank) {
				values.bank_account = bank;
				values.exchange_rate = 0;
			}
		}
		const bearer = frm._bt_settings && frm._bt_settings.default_charge_bearer;
		if (frm.doc.method === BT_SWIFT && !frm.doc.charge_bearer && bearer) {
			values.charge_bearer = bearer;
		}
		bt_set_values(frm, values).then(() => {
			bt_set_labels(frm);
			bt_preview(frm);
		});
	},

	bank_account(frm) {
		if (frm._bt_applying) {
			return;
		}
		// a rate belongs to the currency of the bank: the preview takes the rate of the new one
		bt_set_values(frm, { exchange_rate: 0 }).then(() => bt_schedule_preview(frm));
	},

	party_type(frm) {
		bt_set_labels(frm);
		if (frm._bt_applying) {
			return;
		}
		const values = { party: "", party_name: "", contact_person: "" };
		BT_BENEFICIARY_FIELDS.forEach((f) => (values[f] = ""));
		bt_set_values(frm, values).then(() => bt_schedule_preview(frm));
	},

	party(frm) {
		if (frm._bt_applying) {
			return;
		}
		// the party's own details come back with the preview (or with the save)
		const values = { party_name: "", contact_person: "" };
		BT_BENEFICIARY_FIELDS.forEach((f) => (values[f] = ""));
		bt_set_values(frm, values).then(() => bt_preview(frm));
	},

	party_bank_account(frm) {
		if (frm._bt_applying) {
			return;
		}
		// the IBAN, bank and SWIFT code belong to the account: taken again from the new one
		bt_set_values(frm, { beneficiary_iban: "", beneficiary_bank: "", beneficiary_swift: "" }).then(() =>
			bt_preview(frm)
		);
	},

	beneficiary_iban(frm) {
		const value = frm.doc.beneficiary_iban || "";
		const compact = value.replace(/\s+/g, "").toUpperCase();
		if (compact !== value) {
			// stored compact; the print groups it in fours
			frm.set_value("beneficiary_iban", compact);
			return;
		}
		bt_set_iban_hint(frm);
		if (!frm._bt_applying) {
			bt_schedule_preview(frm);
		}
	},

	execution_date(frm) {
		bt_set_execution_hint(frm);
		if (!frm._bt_applying) {
			bt_schedule_preview(frm);
		}
	},

	override_fee(frm) {
		if (frm.doc.override_fee) {
			// the rule fee is the starting point
			frm.scroll_to_field("fee_amount");
			bt_render_all(frm);
			return;
		}
		const had_reason = !!(frm.doc.fee_override_reason || "").trim();
		bt_set_values(frm, { fee_override_reason: "" }).then(() => {
			if (had_reason) {
				frappe.show_alert({
					message: __("Fee override removed; the rule fee applies."),
					indicator: "blue",
				});
			}
			bt_preview(frm);
		});
	},

	fee_amount(frm) {
		if (frm._bt_applying) {
			return;
		}
		bt_set_values(frm, { bank_amount: bt_bank_amount(frm, frm.doc.fee_amount) }).then(() => bt_render_all(frm));
	},

	before_submit(frm) {
		// saves a round trip only: the server rejects the submit anyway
		const r = frm._bt_result;
		if (!r) {
			return;
		}
		if (r.approval_required) {
			frappe.msgprint({ message: bt_approval_text(r), indicator: "orange", title: __("Approval required:") });
			frappe.validated = false;
			return;
		}
		if (r.limit_action === "Block" && (r.limit_messages || []).length) {
			frappe.msgprint({ message: bt_limit_text(r), indicator: "red", title: __("Limit exceeded:") });
			frappe.validated = false;
		}
	},
};

BT_PREVIEW_FIELDS.forEach((fieldname) => {
	bt_handlers[fieldname] = (frm) => {
		if (!frm._bt_applying) {
			bt_schedule_preview(frm);
		}
	};
});

frappe.ui.form.on("Bank Transfer", bt_handlers);

// P4: the amount follows the allocated invoices while the user has not typed another amount
frappe.ui.form.on("PDC Cheque Reference", {
	allocated_amount(frm) {
		frm.doctype === "Bank Transfer" && bt_follow_allocations(frm);
	},
	references_remove(frm) {
		frm.doctype === "Bank Transfer" && bt_follow_allocations(frm);
	},
});

// ------------------------------------------------------------------------------------------ setup / defaults

function bt_setup_queries(frm) {
	frm.set_query("party_type", () => ({
		query: "erpnext.setup.doctype.party_type.party_type.get_party_type",
	}));
	frm.set_query("bank_account", () => ({
		filters: { company: frm.doc.company, is_company_account: 1, disabled: 0 },
	}));
	frm.set_query("to_bank_account", () => ({
		filters: {
			company: frm.doc.company,
			is_company_account: 1,
			disabled: 0,
			name: ["!=", frm.doc.bank_account || ""],
		},
	}));
	frm.set_query("fee_account", () => ({
		filters: { company: frm.doc.company, root_type: "Expense", is_group: 0 },
	}));
	frm.set_query("reference_doctype", "references", () => ({
		filters: { name: ["in", bt_reference_types(frm.doc.transfer_type, frm.doc.party_type)] },
	}));
	frm.set_query("reference_name", "references", (doc, cdt, cdn) => {
		const row = locals[cdt][cdn];
		const filters = { docstatus: 1, company: frm.doc.company };
		const party_field = {
			"Sales Invoice": "customer",
			"Sales Order": "customer",
			"Purchase Invoice": "supplier",
			"Purchase Order": "supplier",
		}[row.reference_doctype];
		if (party_field && frm.doc.party) {
			filters[party_field] = frm.doc.party;
		}
		return { filters };
	});
	bt_setup_accounting_queries(frm);
}

function bt_setup_accounting_queries(frm) {
	frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
	frm.set_query("project", () => ({ filters: { company: frm.doc.company } }));
	frm.set_query("mode_of_payment", () => ({ filters: { enabled: 1 } }));
	frm.set_query("party_bank_account", () => ({
		filters: { party_type: frm.doc.party_type, party: frm.doc.party, is_company_account: 0 },
	}));
	frm.set_query("contact_person", () => ({
		query: "frappe.contacts.doctype.contact.contact.contact_query",
		filters: { link_doctype: frm.doc.party_type, link_name: frm.doc.party },
	}));
	// ERPNext's own helper: dimension queries by company and the company's default dimensions
	if (window.erpnext && erpnext.accounts && erpnext.accounts.dimensions) {
		erpnext.accounts.dimensions.setup_dimension_filters(frm, frm.doctype);
	}
}

function bt_load_settings(frm) {
	if (!frm.doc.company) {
		frm._bt_settings = null;
		return Promise.resolve();
	}
	return frappe.xcall(BT_API + "get_transfer_settings", { company: frm.doc.company }).then((settings) => {
		frm._bt_settings = settings || {};
	});
}

function bt_default_bank(frm, method) {
	const banks = (frm._bt_settings && frm._bt_settings.default_bank_accounts) || {};
	return (method && banks[method]) || null;
}

function bt_company_currency(frm) {
	return (
		(frm._bt_result && frm._bt_result.company_currency) ||
		(frm._bt_settings && frm._bt_settings.company_currency) ||
		frappe.defaults.get_default("currency")
	);
}

function bt_apply_defaults(frm, is_new) {
	// minimum entry: method, bank and charge bearer from Treasury Company Settings
	const settings = frm._bt_settings || {};
	// a transfer opened by a Treasury button keeps the method the user clicked (e.g. Fawran)
	const untouched = is_new && !frm.doc.bank_account && !frm.doc.__treasury_from;
	const values = {};
	if (untouched && settings.default_transfer_method && frm.doc.method !== settings.default_transfer_method) {
		values.method = settings.default_transfer_method;
	}
	const method = values.method || frm.doc.method;
	if (!frm.doc.bank_account) {
		const bank = bt_default_bank(frm, method);
		if (bank) {
			values.bank_account = bank;
		}
	}
	if (settings.default_charge_bearer && (untouched || !frm.doc.charge_bearer)) {
		values.charge_bearer = settings.default_charge_bearer;
	}
	frm._bt_method = method;
	return bt_set_values(frm, values).then(() => {
		bt_set_labels(frm);
		return bt_preview(frm);
	});
}

function bt_set_values(frm, values) {
	// only the values that change: no dirty form and no trigger ping-pong; our own handlers stay quiet
	const changed = {};
	Object.keys(values || {}).forEach((fieldname) => {
		if (!frm.fields_dict[fieldname]) {
			return;
		}
		if (!bt_same(frm.doc[fieldname], values[fieldname])) {
			changed[fieldname] = values[fieldname];
		}
	});
	if (!Object.keys(changed).length) {
		return Promise.resolve();
	}
	frm._bt_applying = true;
	return Promise.resolve(frm.set_value(changed)).finally(() => {
		frm._bt_applying = false;
	});
}

function bt_same(a, b) {
	if (typeof a === "number" || typeof b === "number") {
		return flt(a, 9) === flt(b, 9);
	}
	return (a || "") === (b || "");
}

// ------------------------------------------------------------------------------------------ fee preview

function bt_schedule_preview(frm) {
	frm._bt_preview_later ? frm._bt_preview_later() : bt_preview(frm);
}

function bt_preview(frm, apply = true) {
	const doc = frm.doc;
	if (doc.docstatus !== 0) {
		return Promise.resolve();
	}
	const seq = (frm._bt_seq = (frm._bt_seq || 0) + 1);
	if (!(doc.company && doc.bank_account && doc.method && doc.transfer_type)) {
		frm._bt_result = null;
		return (apply ? bt_clear_fee(frm) : Promise.resolve()).then(() => bt_render_all(frm));
	}
	const internal = doc.transfer_type === BT_INTERNAL;
	const args = {
		company: doc.company,
		bank_account: doc.bank_account,
		method: doc.method,
		transfer_type: doc.transfer_type,
		amount: flt(doc.amount),
		charge_bearer: doc.charge_bearer,
		execution_date: doc.execution_date,
		name: frm.is_new() ? null : doc.name,
		exchange_rate: flt(doc.exchange_rate),
	};
	if (doc.override_fee) {
		// the approval rule counts the fee the user entered
		args.fee_amount = flt(doc.fee_amount);
	}
	if (internal) {
		args.to_bank_account = doc.to_bank_account;
		args.received_amount = flt(doc.received_amount);
	} else {
		args.party_type = doc.party_type;
		args.party = doc.party;
		BT_BENEFICIARY_FIELDS.forEach((f) => (args[f] = doc[f]));
	}
	return frappe.xcall(BT_API + "get_fee_preview", args).then((r) => {
		if (seq !== frm._bt_seq || !r) {
			return; // a later preview is under way
		}
		frm._bt_result = r;
		return (apply ? bt_apply_preview(frm, r) : Promise.resolve()).then(() => bt_render_all(frm));
	});
}

function bt_apply_preview(frm, r) {
	const doc = frm.doc;
	const values = {
		currency: r.currency || doc.currency,
		bank: r.bank || "",
		base_amount: r.base_amount,
	};
	if (doc.transfer_type === BT_INTERNAL) {
		values.to_currency = r.to_currency || "";
		values.to_bank = r.to_bank || "";
	}
	if (r.currency && r.currency === r.company_currency) {
		values.exchange_rate = 1;
	} else if (r.exchange_rate && [0, 1].includes(flt(doc.exchange_rate))) {
		values.exchange_rate = r.exchange_rate;
	}
	BT_BENEFICIARY_FIELDS.forEach((f) => {
		if (r[f] && !doc[f]) {
			values[f] = f === "beneficiary_iban" ? r[f].replace(/\s+/g, "").toUpperCase() : r[f];
		}
	});
	if (flt(doc.amount) > 0) {
		Object.assign(values, {
			scope: r.scope,
			computed_fee: r.computed_fee,
			correspondent_fee: r.correspondent_fee,
			fee_rule: r.fee_rule || "",
			fee_basis: r.fee_basis || "",
		});
		// an overridden fee is the user's: only the rule values (shown beside it) follow
		if (!doc.override_fee) {
			values.fee_amount = r.computed_fee;
			if (r.fee_account) {
				values.fee_account = r.fee_account;
			}
		}
		values.bank_amount = bt_bank_amount(frm, doc.override_fee ? doc.fee_amount : r.computed_fee);
	} else {
		Object.assign(values, bt_cleared_fee(frm));
	}
	return bt_set_values(frm, values);
}

function bt_cleared_fee(frm) {
	const values = { computed_fee: 0, correspondent_fee: 0, fee_rule: "", fee_basis: "", bank_amount: 0 };
	if (!frm.doc.override_fee) {
		values.fee_amount = 0;
	}
	return values;
}

function bt_clear_fee(frm) {
	return bt_set_values(frm, bt_cleared_fee(frm));
}

function bt_bank_amount(frm, fee) {
	// Payment / Internal: amount + fee leave the bank; Receipt: the bank receives amount - fee
	const amount = flt(frm.doc.amount);
	const value = frm.doc.transfer_type === BT_RECEIPT ? amount - flt(fee) : amount + flt(fee);
	return flt(value, precision("bank_amount", frm.doc));
}

function bt_follow_allocations(frm) {
	const doc = frm.doc;
	if (doc.docstatus !== 0 || !doc.currency || doc.currency !== bt_company_currency(frm)) {
		return;
	}
	if (flt(doc.amount) && flt(doc.amount) !== flt(frm._bt_allocated)) {
		return; // the user typed the amount
	}
	const total = flt(
		(doc.references || []).reduce((sum, row) => sum + flt(row.allocated_amount), 0),
		precision("amount", doc)
	);
	frm._bt_allocated = total;
	if (total > 0) {
		frm.set_value("amount", total);
	}
}

// ------------------------------------------------------------------------------------------ display

function bt_render_all(frm) {
	bt_render_fee_strip(frm);
	bt_set_amount_hint(frm);
	bt_set_iban_hint(frm);
	bt_set_execution_hint(frm);
	if (frm.doc.docstatus === 0) {
		bt_set_draft_headline(frm);
		bt_set_intro(frm);
		bt_set_indicator(frm);
	}
}

function bt_esc(value) {
	return frappe.utils.escape_html(value == null ? "" : String(value));
}

function bt_money(value, currency) {
	return `<bdi>${bt_esc(format_currency(flt(value), currency))}</bdi>`;
}

function bt_date(value) {
	return value ? frappe.datetime.str_to_user(value) : "";
}

function bt_plain(html) {
	// server messages carry markup (<strong>) around values that are not escaped: show them as text only
	const parsed = new DOMParser().parseFromString(String(html || ""), "text/html");
	return bt_esc(parsed.body.textContent || "");
}

function bt_pill(text, color) {
	return `<span class="indicator-pill ${color}">${bt_esc(text)}</span>`;
}

function bt_set_labels(frm) {
	const doc = frm.doc;
	const receipt = doc.transfer_type === BT_RECEIPT;
	const internal = doc.transfer_type === BT_INTERNAL;
	const swift = doc.method === BT_SWIFT;
	const label = (fieldname, text) => frm.set_df_property(fieldname, "label", text);

	label("party", doc.party_type ? __(doc.party_type) : __("Party", null, BT_CTX));
	label(
		"party_bank_account",
		receipt ? __("Payer Bank Account", null, BT_CTX) : __("Beneficiary Bank Account", null, BT_CTX)
	);
	label("beneficiary_iban", receipt ? __("Payer IBAN", null, BT_CTX) : __("Beneficiary IBAN", null, BT_CTX));
	label("beneficiary_bank", receipt ? __("Payer Bank", null, BT_CTX) : __("Beneficiary Bank", null, BT_CTX));
	label(
		"bank_account",
		receipt ? __("Receiving Bank Account", null, BT_CTX) : __("From Bank Account", null, BT_CTX)
	);
	label(
		"amount",
		internal
			? __("Amount Transferred", null, BT_CTX)
			: receipt
			? __("Amount Paid by Party", null, BT_CTX)
			: __("Amount to Beneficiary", null, BT_CTX)
	);
	label("received_amount", __("Amount Credited", null, BT_CTX));
	label("bank_amount", receipt ? __("Bank Credit", null, BT_CTX) : __("Bank Debit", null, BT_CTX));

	// SWIFT fields: not used for incoming money
	frm.toggle_display("beneficiary_swift", swift && !receipt && !internal);
	frm.toggle_display("charge_bearer", swift && !receipt);
	frm.toggle_reqd("beneficiary_swift", swift && doc.transfer_type === BT_PAYMENT && doc.docstatus === 0);
	frm.toggle_display("contact_person", !internal && doc.party_type !== "Employee");
	const rate = flt(doc.exchange_rate);
	frm.toggle_display("base_amount", !!rate && rate !== 1);
}

function bt_meta_description(fieldname) {
	const df = (frappe.get_meta("Bank Transfer").fields || []).find((d) => d.fieldname === fieldname);
	return df && df.description ? bt_esc(__(df.description)) : "";
}

function bt_set_description(frm, fieldname, html) {
	const field = frm.get_field(fieldname);
	if (field && field.df.description !== html) {
		frm.set_df_property(fieldname, "description", html);
	}
}

function bt_set_iban_hint(frm) {
	const doc = frm.doc;
	const missing =
		doc.docstatus === 0 &&
		doc.transfer_type === BT_PAYMENT &&
		doc.method !== BT_SWIFT &&
		!doc.beneficiary_iban &&
		!doc.party_bank_account;
	bt_set_description(
		frm,
		"beneficiary_iban",
		missing
			? `<span class="text-warning">${bt_esc(
					__("Required before submit: IBAN or a beneficiary bank account.")
			  )}</span>`
			: ""
	);
}

function bt_set_execution_hint(frm) {
	const doc = frm.doc;
	const future = doc.docstatus === 0 && doc.execution_date && doc.execution_date > frappe.datetime.get_today();
	bt_set_description(
		frm,
		"execution_date",
		future
			? `<span class="text-primary">${bt_esc(
					__("Future date: the transfer will be scheduled and posted automatically on that date.")
			  )}</span>`
			: bt_meta_description("execution_date")
	);
}

function bt_set_amount_hint(frm) {
	// the method limit of the bank account (Fawran above all), muted under the amount
	const r = frm.doc.docstatus === 0 ? frm._bt_result : null;
	const limit = r && r.limit;
	let hint = "";
	if (limit) {
		const currency = frm.doc.currency;
		const method = __(limit.method, null, BT_CTX);
		const per = flt(limit.per_transaction_limit);
		const daily = flt(limit.daily_limit);
		const fmt = (value) => format_currency(value, currency);
		if (per > 0 && daily > 0) {
			hint = __("{0} limit: {1} per transfer · {2} per day · used on {3}: {4}", [
				method,
				fmt(per),
				fmt(daily),
				bt_date(limit.date),
				fmt(limit.used),
			]);
		} else if (per > 0) {
			hint = __("{0} limit: {1} per transfer", [method, fmt(per)]);
		} else if (daily > 0) {
			hint = __("{0} limit: {1} per day · used on {2}: {3}", [
				method,
				fmt(daily),
				bt_date(limit.date),
				fmt(limit.used),
			]);
		}
	}
	bt_set_description(
		frm,
		"amount",
		hint ? `<span class="text-muted">${bt_esc(hint)}</span>` : bt_meta_description("amount")
	);
}

function bt_render_fee_strip(frm) {
	const field = frm.get_field("fee_summary_html");
	if (!field) {
		return;
	}
	const doc = frm.doc;
	if (!flt(doc.amount) || !doc.currency) {
		field.$wrapper.html("");
		return;
	}
	const currency = doc.currency;
	const receipt = doc.transfer_type === BT_RECEIPT;
	const internal = doc.transfer_type === BT_INTERNAL;
	const ben = doc.method === BT_SWIFT && doc.charge_bearer === "BEN";
	const no_rule = !doc.fee_rule && !ben && !doc.override_fee;
	const muted = (html) => `<tr><td colspan="2" class="text-muted small">${html}</td></tr>`;
	const line = (label, value, bold) =>
		`<tr${bold ? ' style="font-weight: bold; border-top: 2px solid var(--border-color)"' : ""}>
			<td>${label}</td><td style="text-align: end">${value}</td></tr>`;

	const first = internal
		? __("Transferred", null, BT_CTX)
		: receipt
		? __("Party pays", null, BT_CTX)
		: __("Beneficiary receives", null, BT_CTX);
	const last = receipt ? __("Bank credit", null, BT_CTX) : __("Bank debit", null, BT_CTX);
	const sign = receipt ? "− " : "+ ";

	let fee_label = bt_esc(__("Fee", null, BT_CTX));
	const tags = [];
	if (doc.fee_rule) {
		tags.push(bt_esc(doc.fee_rule));
	}
	if (doc.scope) {
		tags.push(bt_pill(__(doc.scope, null, BT_CTX), BT_SCOPE_COLORS[doc.scope] || "gray"));
	}
	if (tags.length) {
		fee_label += ` (${tags.join(" · ")})`;
	}
	let fee_value = sign + bt_money(doc.fee_amount, currency);
	if (doc.override_fee) {
		fee_value = `${sign}<s>${bt_money(doc.computed_fee, currency)}</s> <b>${bt_money(doc.fee_amount, currency)}</b>`;
		fee_label += " " + bt_pill(__("Overridden", null, BT_CTX), "orange");
	} else if (no_rule) {
		fee_label += " " + bt_pill(__("No fee rule matched", null, BT_CTX), "orange");
	}

	const rows = [
		line(bt_esc(first), bt_money(doc.amount, currency)),
		line(fee_label, fee_value),
		line(bt_esc(last), bt_money(doc.bank_amount, currency), true),
	];
	if (internal && doc.to_currency) {
		const received = doc.to_currency === currency ? doc.amount : doc.received_amount;
		rows.push(line(bt_esc(__("Target receives", null, BT_CTX)), bt_money(received, doc.to_currency)));
		if (doc.to_currency !== currency && flt(doc.amount) && flt(doc.received_amount)) {
			// the currency the user thinks in: the one that is not the company currency is the "1"
			const source_is_foreign = currency !== bt_company_currency(frm);
			const rate = source_is_foreign
				? flt(doc.received_amount) / flt(doc.amount)
				: flt(doc.amount) / flt(doc.received_amount);
			rows.push(
				muted(
					bt_esc(
						__("Bank rate: 1 {0} = {1} {2}", [
							source_is_foreign ? currency : doc.to_currency,
							format_number(rate, null, 4),
							source_is_foreign ? doc.to_currency : currency,
						])
					)
				)
			);
		}
	}
	if (doc.method === BT_SWIFT && doc.charge_bearer === "OUR" && flt(doc.correspondent_fee)) {
		rows.push(muted(bt_esc(__("incl. correspondent fee {0}", [format_currency(doc.correspondent_fee, currency)]))));
	}
	if (doc.override_fee) {
		rows.push(
			muted(
				bt_esc(
					__("Rule fee {0} replaced by {1}.", [
						format_currency(doc.computed_fee, currency),
						format_currency(doc.fee_amount, currency),
					])
				)
			)
		);
	} else if (no_rule && doc.docstatus === 0) {
		rows.push(muted(bt_esc(__("Tick Override Fee to enter the bank's fee."))));
	} else if (doc.fee_basis) {
		rows.push(muted(bt_esc(doc.fee_basis)));
	}
	field.$wrapper.html(`<table class="table table-sm" style="margin-bottom: var(--margin-md)">${rows.join("")}</table>`);
}

function bt_headline(frm, html, color) {
	// our own block of the form message area (the intro keeps its own)
	if (frm._bt_$headline) {
		frm._bt_$headline.remove();
		frm._bt_$headline = null;
	}
	if (html) {
		frm._bt_$headline = frm.dashboard.set_headline(html, color);
	}
	return frm._bt_$headline;
}

function bt_threshold(r) {
	return format_currency(r.approval_threshold, r.company_currency);
}

function bt_approval_text(r) {
	return bt_esc(
		__("Transfers above {0} are submitted by a Treasury Manager. Save it and ask a Treasury Manager to submit.", [
			bt_threshold(r),
		])
	);
}

function bt_limit_text(r) {
	const block = r.limit_action === "Block";
	return `${(r.limit_messages || []).map(bt_plain).join("<br>")}<br>${bt_esc(
		block
			? __(
					"This transfer cannot be submitted. Split it, use another method or bank, or ask a Treasury Manager to raise the limit."
			  )
			: __("You can still submit this transfer.")
	)}`;
}

function bt_set_draft_headline(frm) {
	const r = frm._bt_result;
	if (frm.doc.docstatus !== 0 || !r) {
		bt_headline(frm, null);
		return;
	}
	const rank = { blue: 1, orange: 2, red: 3 };
	const blocks = [];
	let color = null;
	const add = (html, block_color) => {
		blocks.push(`<div>${html}</div>`);
		if (!color || rank[block_color] > rank[color]) {
			color = block_color;
		}
	};
	if ((r.limit_messages || []).length) {
		add(`<b>${bt_esc(__("Limit exceeded:"))}</b> ${bt_limit_text(r)}`, r.limit_action === "Block" ? "red" : "orange");
	}
	if (r.approval_required) {
		add(`<b>${bt_esc(__("Approval required:"))}</b> ${bt_approval_text(r)}`, "orange");
	} else if (r.above_threshold) {
		add(bt_esc(__("Above the approval threshold ({0}): by submitting you approve it.", [bt_threshold(r)])), "blue");
	}
	bt_headline(frm, blocks.length ? blocks.join("") : null, color);
}

function bt_set_source_intro(frm) {
	// a new transfer opened from a document or a party (Treasury buttons of the ERPNext forms,
	// public/js/treasury_payment_buttons.js; `__treasury_from` is client only and never saved)
	const doc = frm.doc;
	const ref = (doc.references || [])[0];
	const from =
		doc.__treasury_from ||
		(ref && ref.reference_doctype && ref.reference_name
			? { doctype: ref.reference_doctype, name: ref.reference_name }
			: null);
	if (!from || !from.doctype || !from.name) {
		frm.set_intro();
		return;
	}
	const route = `/app/${frappe.router.slug(from.doctype)}/${encodeURIComponent(from.name)}`;
	const link = `<a href="${route}">${bt_esc(__(from.doctype))} <b><bdi>${bt_esc(from.name)}</bdi></b></a>`;
	// the values are escaped; the sentences are our own translations
	if (!ref && !flt(doc.amount)) {
		frm.set_intro(__("For {0}: type the amount, then save.", [link]), "blue");
		return;
	}
	const amount = bt_money(doc.amount, doc.currency);
	frm.set_intro(__("From {0}: {1}. Check and save; Submit sends it.", [link, amount]), "blue");
}

function bt_set_intro(frm) {
	// what Submit will do, on a saved and unchanged draft
	const doc = frm.doc;
	if (frm.is_new() && doc.docstatus === 0) {
		bt_set_source_intro(frm);
		return;
	}
	if (frm.is_new() || frm.is_dirty() || doc.docstatus !== 0 || !flt(doc.amount) || !doc.execution_date) {
		frm.set_intro();
		return;
	}
	const date = bt_date(doc.execution_date);
	const currency = doc.currency;
	const fee = flt(doc.fee_amount);
	const money = (value, cur) => bt_esc(format_currency(value, cur || currency));
	let text;
	if (doc.execution_date > frappe.datetime.get_today()) {
		text = __("On submit: the transfer is scheduled for {0}; the Payment Entry is posted automatically that day.", [
			bt_esc(date),
		]);
	} else if (doc.transfer_type === BT_INTERNAL) {
		const same = !doc.to_currency || doc.to_currency === currency;
		text = __("On submit: Payment Entry dated {0}. {1} out of {2}, {3} into {4}.", [
			bt_esc(date),
			money(doc.bank_amount),
			bt_esc(doc.bank_account),
			money(same ? doc.amount : doc.received_amount, doc.to_currency),
			bt_esc(doc.to_bank_account),
		]);
	} else if (doc.transfer_type === BT_RECEIPT) {
		text = fee
			? __("On submit: Payment Entry dated {0}. Bank credit {1} ({2} − fee {3}).", [
					bt_esc(date),
					money(doc.bank_amount),
					money(doc.amount),
					money(fee),
			  ])
			: __("On submit: Payment Entry dated {0}. Bank credit {1}.", [bt_esc(date), money(doc.bank_amount)]);
	} else {
		text = fee
			? __("On submit: Payment Entry dated {0}. Bank debit {1} ({2} + fee {3}).", [
					bt_esc(date),
					money(doc.bank_amount),
					money(doc.amount),
					money(fee),
			  ])
			: __("On submit: Payment Entry dated {0}. Bank debit {1}.", [bt_esc(date), money(doc.bank_amount)]);
	}
	const completion = frm._bt_settings && frm._bt_settings.transfer_completion;
	const at_once =
		completion === "On Send" || (completion === "On Send for Fawran" && doc.method === "Fawran");
	if (at_once && doc.execution_date <= frappe.datetime.get_today()) {
		text += " " + bt_esc(__("It is marked Completed at once."));
	}
	// the values are escaped above; the sentences are our own translations
	frm.set_intro(`<div>${text}</div>`, "blue");
}

function bt_set_indicator(frm) {
	const doc = frm.doc;
	if (frm.is_new()) {
		return;
	}
	if (doc.docstatus === 0) {
		if (frm._bt_result && frm._bt_result.approval_required) {
			frm.page.set_indicator(__("Awaiting Approval", null, BT_CTX), "orange");
		}
		return;
	}
	if (doc.status === "Scheduled" && doc.execution_date <= frappe.datetime.get_today()) {
		frm.page.set_indicator(__("Overdue", null, "Treasury"), "red");
		return;
	}
	const label =
		doc.status === "Sent" && doc.transfer_type === BT_RECEIPT
			? __("Received", null, BT_CTX)
			: __(doc.status, null, BT_CTX);
	frm.page.set_indicator(label, BT_INDICATORS[doc.status] || "blue");
}

function bt_voucher_link(doctype, name, cancelled) {
	const route = `/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}`;
	const text = `<bdi>${bt_esc(name)}</bdi>`;
	return `<a href="${route}"><b>${cancelled ? `<s>${text}</s>` : text}</b></a>`;
}

function bt_set_submitted_headline(frm) {
	// the Payment Entry number (the paper-filing key) and where the transfer is now, at a glance
	const doc = frm.doc;
	const today = frappe.datetime.get_today();
	const b = (text) => `<b>${bt_esc(text)}</b>`;
	const parts = [];
	let color = BT_INDICATORS[doc.status] || "blue";

	if (doc.status === "Scheduled") {
		if (doc.execution_date > today) {
			const days = frappe.datetime.get_day_diff(doc.execution_date, today);
			bt_headline(
				frm,
				`<div>${__("Scheduled for {0} (in {1} days). The Payment Entry is posted automatically on that date.", [
					b(bt_date(doc.execution_date)),
					days,
				])}</div>`,
				"blue"
			);
		} else {
			let html = __(
				"Scheduled for {0} but not posted yet. Click Send Now, or ask a System Manager to check the Error Log.",
				[b(bt_date(doc.execution_date))]
			);
			if (frappe.user.has_role("System Manager")) {
				const url = `/app/error-log?reference_name=${encodeURIComponent(doc.name)}`;
				html += ` <a href="${url}">${bt_esc(__("Error Log", null, BT_CTX))}</a>`;
			}
			bt_headline(frm, `<div>${html}</div>`, "orange");
		}
		return;
	}
	if (!doc.payment_entry) {
		return;
	}
	const cancelled = doc.status === "Cancelled";
	let pe = bt_voucher_link("Payment Entry", doc.payment_entry, cancelled);
	if (cancelled) {
		pe += " " + bt_esc(__("(cancelled)"));
		color = "white";
	}
	parts.push(__("Payment Entry: {0}", [pe]));
	const receipt = doc.transfer_type === BT_RECEIPT;
	const sent = receipt ? __("Received {0}", [bt_date(doc.execution_date)]) : __("Sent {0}", [bt_date(doc.execution_date)]);

	if (doc.status === "Sent") {
		parts.push(bt_esc(sent));
		parts.push(`<i>${bt_esc(__("Awaiting bank confirmation"))}</i>`);
	} else if (doc.status === "Completed") {
		const cleared = doc.bank_transaction_date || doc.completion_date;
		parts.push(`${bt_esc(sent)} • ${bt_esc(__("Cleared {0}", [bt_date(cleared)]))}`);
		const confirmed = (doc.status_log || []).filter((row) => row.event === "Target Confirmed").pop();
		if (doc.transfer_type === BT_INTERNAL && confirmed) {
			parts.push(bt_esc(__("Target confirmed {0}", [bt_date(confirmed.event_date)])));
		}
	} else if (doc.status === "Returned") {
		let returned = bt_esc(__("Returned {0}", [bt_date(doc.return_date)]));
		const reversal = doc.reversal_payment_entry
			? bt_voucher_link("Payment Entry", doc.reversal_payment_entry)
			: doc.reversal_journal
			? bt_voucher_link("Journal Entry", doc.reversal_journal)
			: "";
		if (reversal) {
			returned += " — " + __("Reversal: {0}", [reversal]);
		}
		if (flt(doc.fee_amount) > 0) {
			returned += ` (${bt_esc(doc.fees_refunded ? __("fee refunded") : __("fee kept as expense"))})`;
		}
		parts.push(returned);
	}
	if (doc.bank_reference) {
		parts.push(bt_esc(__("Bank Ref. {0}", [doc.bank_reference])));
	}
	const copy = [doc.payment_entry, doc.reversal_payment_entry || doc.reversal_journal].filter(Boolean);
	// every value is escaped above; the sentences are our own translations
	const $headline = bt_headline(
		frm,
		`<div>${parts.join(" · ")} &nbsp; <a class="treasury-copy-pe">⧉ ${bt_esc(
			__("Copy", null, "Treasury Voucher")
		)}</a></div>`,
		color
	);
	$headline &&
		$headline.find(".treasury-copy-pe").on("click", () => frappe.utils.copy_to_clipboard(copy.join(", ")));
}

function bt_translate_log(frm) {
	// events and statuses are stored in English in the status log (Data fields): translate them on display
	const contexts = {
		event: BT_LOG_CTX,
		from_status: BT_CTX,
		to_status: BT_CTX,
		from_accounting: BT_CTX,
		to_accounting: BT_CTX,
	};
	Object.entries(contexts).forEach(([fieldname, context]) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield("Bank Transfer Status Log", fieldname, name);
			if (df) {
				df.formatter = (value) => bt_esc(value ? __(value, null, context) : "");
			}
		}
	});
	frm.refresh_field("status_log");
}

function bt_render_vouchers(frm) {
	// server-rendered, escaped summary (treasury.utils.voucher_summary)
	frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
}

// ------------------------------------------------------------------------------------------ buttons

function bt_add_actions(frm) {
	const doc = frm.doc;
	if (!frappe.user.has_role(BT_USER_ROLES)) {
		return;
	}
	const receipt = doc.transfer_type === BT_RECEIPT;
	const today = frappe.datetime.get_today();
	const remarks = { fieldname: "remarks", fieldtype: "Small Text", label: __("Remarks", null, "Treasury") };
	const info = (html) => ({ fieldname: "info", fieldtype: "HTML", options: html });

	if (doc.status === "Scheduled") {
		bt_action(
			frm,
			"send_now",
			receipt ? __("Record Now", null, BT_CTX) : __("Send Now", null, BT_CTX),
			[
				{
					fieldname: "execution_date",
					fieldtype: "Date",
					label: __("Execution Date", null, BT_CTX),
					reqd: 1,
					default: today,
					description: __("Posting date of the Payment Entry: today or an earlier date."),
				},
				info(
					`<p class="text-muted small">${bt_esc(
						__("A Payment Entry of {0} is posted now.", [format_currency(doc.bank_amount, doc.currency)])
					)}</p>`
				),
				remarks,
			],
			["execution_date", "remarks"],
			{
				validate: (values) =>
					values.execution_date > frappe.datetime.get_today() ? __("Use Reschedule for a future date.") : null,
			}
		);
		const later = doc.execution_date > today ? doc.execution_date : today;
		bt_action(
			frm,
			"reschedule",
			__("Reschedule", null, BT_CTX),
			[
				{
					fieldname: "execution_date",
					fieldtype: "Date",
					label: __("New Execution Date", null, BT_CTX),
					reqd: 1,
					default: frappe.datetime.add_days(later, 1),
					description: __("Must be after today. To send today, use Send Now."),
				},
				{ fieldname: "remarks", fieldtype: "Small Text", label: __("Reason", null, BT_CTX) },
			],
			["execution_date", "remarks"],
			{
				after: (values) =>
					frappe.show_alert({
						message: __("Rescheduled to {0}.", [bt_date(values.execution_date)]),
						indicator: "green",
					}),
			}
		);
	}

	if (doc.status === "Sent") {
		bt_action(
			frm,
			"mark_completed",
			receipt ? __("Confirm Bank Credit", null, BT_CTX) : __("Mark Completed", null, BT_CTX),
			[
				{
					fieldname: "completion_date",
					fieldtype: "Date",
					label: __("Completion Date", null, BT_CTX),
					reqd: 1,
					default: today,
				},
				{
					fieldname: "bank_transaction_date",
					fieldtype: "Date",
					label: __("Bank Transaction Date", null, BT_CTX),
					description: __(
						"Date on the bank statement; it becomes the clearance date of the Payment Entry. Empty: the completion date."
					),
				},
				remarks,
				info(
					`<p class="text-muted small">${bt_esc(
						__("Tip: PDC Bank Clearing completes transfers from the bank statement automatically.")
					)}</p>`
				),
			],
			["completion_date", "bank_transaction_date", "remarks"]
		);
		if (frappe.model.can_read("PDC Bank Clearing")) {
			frm.add_custom_button(
				__("Match Bank Statement", null, BT_CTX),
				() => {
					frappe.route_options = { company: doc.company, bank_account: doc.bank_account };
					frappe.set_route("Form", "PDC Bank Clearing");
				},
				__("Actions", null, BT_CTX)
			);
		}
	}

	if (["Sent", "Completed"].includes(doc.status) && !receipt && frappe.user.has_role(BT_MANAGER_ROLES)) {
		const fee = flt(doc.fee_amount);
		const refund = !!(frm._bt_settings && frm._bt_settings.refund_fees_on_return);
		let html = bt_esc(
			__("A reversal voucher is posted on the return date. Linked invoices become outstanding again.")
		);
		if (fee > 0) {
			const amount = format_currency(fee, doc.currency);
			html +=
				"<br>" +
				bt_esc(
					refund
						? __("The bank refunds the fee: {0} is reversed too.", [amount])
						: __(
								"The fee of {0} stays as an expense (the bank does not refund fees; see Treasury Company Settings).",
								[amount]
						  )
				);
		}
		bt_action(
			frm,
			"mark_returned",
			__("Mark Returned", null, BT_CTX),
			[
				{
					fieldname: "return_date",
					fieldtype: "Date",
					label: __("Return Date", null, BT_CTX),
					reqd: 1,
					default: today,
				},
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason", null, BT_CTX), reqd: 1 },
				info(`<p class="text-warning small">${html}</p>`),
			],
			["return_date", "reason"]
		);
	}
}

// actions guarded by the low balance check (treasury.balance_alerts.guard)
const BT_LOW_BALANCE_METHODS = ["send_now", "reschedule"];

function bt_action(frm, method, label, fields, args, options = {}) {
	// the server checks the permission, the role, the status and the dates (lifecycle.py)
	frm.add_custom_button(
		label,
		() => {
			const dialog = new frappe.ui.Dialog({
				title: label,
				fields,
				primary_action_label: label,
				primary_action(values) {
					const error = options.validate && options.validate(values);
					if (error) {
						frappe.msgprint(error);
						return;
					}
					dialog.hide();
					const call_args = {};
					args.forEach((key) => (call_args[key] = values[key] || null));
					// Send Now / Reschedule: on a low balance Block the shared dialog offers a Treasury Manager
					// "Override and Continue" (server-checked, see the end of this file)
					const run = (override) => {
						const guarded = !override && BT_LOW_BALANCE_METHODS.includes(method);
						treasury.low_balance.retry = guarded ? () => run(true) : null;
						return frm
							.call({
								doc: frm.doc,
								method,
								args: Object.assign({}, call_args, override ? { allow_low_balance: 1 } : {}),
								freeze: true,
								always: () => (treasury.low_balance.retry = null),
							})
							.then(() => {
								options.after && options.after(values);
								frm.reload_doc();
							});
					};
					run(false);
				},
			});
			dialog.show();
		},
		__("Actions", null, BT_CTX)
	);
}

function bt_add_print_button(frm) {
	frm.add_custom_button(
		__("Print Transfer Order", null, BT_CTX),
		() => {
			const params = new URLSearchParams({
				doctype: frm.doctype,
				name: frm.doc.name,
				format: "Bank Transfer Order",
				_lang: frappe.boot.lang || "",
			});
			window.open(`/printview?${params.toString()}`);
		},
		__("Print", null, BT_CTX)
	);
}

function bt_add_view_buttons(frm) {
	const doc = frm.doc;
	if (doc.docstatus !== 1 || !doc.payment_entry) {
		return;
	}
	const group = __("View", null, BT_CTX);
	if (frappe.model.can_read("Payment Entry")) {
		frm.add_custom_button(
			__("Payment Entry", null, BT_CTX),
			() => frappe.set_route("Form", "Payment Entry", doc.payment_entry),
			group
		);
	}
	if (frappe.model.can_get_report("General Ledger")) {
		frm.add_custom_button(
			__("General Ledger", null, BT_CTX),
			() =>
				frappe.set_route("query-report", "General Ledger", {
					company: doc.company,
					voucher_no: doc.payment_entry,
					from_date: doc.execution_date,
					to_date: doc.execution_date,
				}),
			group
		);
	}
}

// ------------------------------------------------------------------------------------ low balance guard
// The override is offered to Treasury Managers only (the server checks the role and logs the override).
frappe.ui.form.on("Bank Transfer", {
	refresh(frm) {
		frm.toggle_display(
			"allow_low_balance",
			frm.doc.docstatus === 0 && frappe.user.has_role(["Treasury Manager", "System Manager"])
		);
		bt_low_balance_refresh(frm);
	},

	allow_low_balance(frm) {
		bt_low_balance_ticked(frm);
	},
});

function bt_low_balance_ticked(frm) {
	// design 7e §4.4 / P16: a ticked override is never hidden in a collapsed section
	if (!frm.doc.allow_low_balance || frm.doc.docstatus !== 0) {
		return;
	}
	const field = frm.get_field("allow_low_balance");
	const section = field && field.section;
	section && section.is_collapsed && section.is_collapsed() && section.collapse(false);
	if (frm._bt_low_balance_alerted !== frm.doc.name) {
		frm._bt_low_balance_alerted = frm.doc.name;
		frappe.show_alert({
			message: bt_esc(
				__(
					"The block is overridden only if this entry takes a bank below its minimum. Each override is logged."
				)
			),
			indicator: "orange",
		});
	}
}

function bt_low_balance_refresh(frm) {
	// the override event is an orange pill in the status log, and the latest one a line on the dashboard
	const event = "Low Balance Override";
	for (const name of [null, frm.doc.name]) {
		const df = frappe.meta.get_docfield("Bank Transfer Status Log", "event", name);
		if (df && !(df.formatter && df.formatter.treasury_low_balance)) {
			const base = df.formatter;
			df.formatter = (value, ...rest) => {
				if (value === event) {
					return `<span class="indicator-pill orange">${bt_esc(__(value, null, BT_LOG_CTX))}</span>`;
				}
				return base ? base(value, ...rest) : bt_esc(value ? __(value, null, BT_LOG_CTX) : "");
			};
			df.formatter.treasury_low_balance = true;
		}
	}
	frm.refresh_field("status_log");
	if (frm.doc.docstatus === 0 && frm.doc.allow_low_balance) {
		bt_low_balance_ticked(frm);
	}
	const row = (frm.doc.status_log || []).filter((r) => r.event === event).pop();
	if (row) {
		frm.dashboard.set_headline(
			`<div>${bt_esc(
				__("Submitted with a low-balance override by {0} on {1}.", [
					frappe.user.full_name(row.user) || row.user,
					frappe.datetime.str_to_user(row.datetime),
				])
			)}</div>`,
			"orange"
		);
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

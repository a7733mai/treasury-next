// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_transfers/charges.py and the controller; this script only defaults,
// explains and links (design 7d §1). Form scripts share the global scope: every name below is prefixed
// `bch_` / `BCH_`.
const BCH_CTX = "Bank Charge";
const BCH_LOG_CTX = "Bank Charge Status Log";
const BCH_METHOD = "treasury.bank_transfers.doctype.bank_charge.bank_charge.";
const BCH_USER_ROLES = ["Treasury User", "Treasury Manager", "System Manager"];
const BCH_CHARGE = "Charge";
const BCH_REFUND = "Refund";
const BCH_JOURNAL_ENTRY = "Journal Entry";
const BCH_INDICATORS = { Draft: "gray", Posted: "blue", Reconciled: "green", Cancelled: "darkgrey" };
// the fields a refund takes from the charge it refunds (mirrors REFUND_COPY_FIELDS of the controller)
const BCH_REFUND_FIELDS = ["company", "bank_account", "category", "expense_account", "cost_center", "project"];
// the "Refund of" query lists only charges with something left when there are few charges
const BCH_REFUND_QUERY_LIMIT = 50;

frappe.ui.form.on("Bank Charge", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1, disabled: 0 },
		}));
		frm.set_query("expense_account", () => ({
			filters: { company: frm.doc.company, root_type: "Expense", is_group: 0 },
		}));
		frm.set_query("category", () => ({ filters: { disabled: 0 } }));
		frm.set_query("against_bank_charge", () => {
			const filters = {
				docstatus: 1,
				entry_type: BCH_CHARGE,
				company: frm.doc.company,
				bank_account: frm.doc.bank_account,
			};
			if (frm._bch_refundable) {
				filters.name = ["in", frm._bch_refundable];
			}
			return { filters };
		});
		bch_setup_accounting_queries(frm);
		// the "+" beside Refunds ▸ Bank Charge in Connections records a refund of this charge
		frm.custom_make_buttons = { "Bank Charge": "Record Refund" };
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.company) {
			// quietly: the remembered bank account stays (the company trigger would clear it)
			const company = frappe.defaults.get_user_default("Company");
			company && bch_set_values(frm, { company });
		}
	},

	refresh(frm) {
		const doc = frm.doc;
		bch_set_labels(frm);
		bch_toggle_currency_fields(frm);
		bch_translate_log(frm);
		frm.get_field("vouchers_html").$wrapper.html((doc.__onload && doc.__onload.voucher_summary) || "");
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		bch_headline(frm, null);
		bch_set_intro(frm);
		if (doc.docstatus === 0) {
			bch_load_refundable(frm);
			if (frm.is_new() && doc.against_bank_charge && !frm._bch_charge) {
				bch_load_charge(frm, false);
			}
			return;
		}
		frm.page.set_indicator(...bch_indicator(doc));
		bch_set_submitted_headline(frm);
		if (!frm.is_dirty()) {
			bch_add_buttons(frm);
		}
	},

	company(frm) {
		if (frm.doc.docstatus !== 0 || frm._bch_applying) {
			return;
		}
		frm._bch_refundable = null;
		// accounts and banks belong to a company
		bch_set_values(frm, { bank_account: "", expense_account: "", cost_center: "" }).then(() => {
			frm._bch_defaults = null;
			bch_load_category_defaults(frm);
			bch_load_refundable(frm);
		});
	},

	category(frm) {
		if (!frm._bch_applying) {
			bch_load_category_defaults(frm);
		}
	},

	bank_account(frm) {
		frm._bch_refundable = null;
		bch_load_refundable(frm);
	},

	entry_type(frm) {
		bch_set_labels(frm);
		if (frm.doc.entry_type === BCH_CHARGE) {
			frm._bch_charge = null;
			bch_set_values(frm, { against_bank_charge: "" });
		} else {
			bch_load_refundable(frm);
			frm.scroll_to_field("against_bank_charge");
		}
		bch_set_intro(frm);
	},

	against_bank_charge(frm) {
		frm._bch_charge = null;
		if (frm.doc.against_bank_charge) {
			bch_load_charge(frm, true);
		} else {
			bch_set_intro(frm);
		}
	},

	amount(frm) {
		bch_set_intro(frm);
	},
});

// ------------------------------------------------------------------------------------------ setup / defaults

function bch_setup_accounting_queries(frm) {
	frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
	frm.set_query("project", () => ({ filters: { company: frm.doc.company } }));
	frm.set_query("mode_of_payment", () => ({ filters: { enabled: 1 } }));
	// ERPNext's own helper: dimension queries by company and the company's default dimensions
	if (window.erpnext && erpnext.accounts && erpnext.accounts.dimensions) {
		erpnext.accounts.dimensions.setup_dimension_filters(frm, frm.doctype);
	}
}

function bch_set_values(frm, values) {
	// only the values that change; our own triggers stay quiet meanwhile
	const changed = {};
	Object.entries(values || {}).forEach(([fieldname, value]) => {
		if (frm.fields_dict[fieldname] && (frm.doc[fieldname] || "") !== (value || "")) {
			changed[fieldname] = value;
		}
	});
	if (!Object.keys(changed).length) {
		return Promise.resolve();
	}
	frm._bch_applying = true;
	return Promise.resolve(frm.set_value(changed)).finally(() => {
		frm._bch_applying = false;
	});
}

function bch_load_category_defaults(frm) {
	const doc = frm.doc;
	if (doc.docstatus !== 0 || !doc.category || !doc.company || (doc.entry_type === BCH_REFUND && doc.against_bank_charge)) {
		bch_set_account_hint(frm, null);
		return;
	}
	const category = doc.category;
	frappe
		.xcall(BCH_METHOD + "get_category_defaults", { category, company: doc.company })
		.then((r) => {
			if (frm.doc.category !== category) {
				return; // another category was chosen meanwhile
			}
			// a value the user typed survives: only an empty value or the previous category's default follows
			const previous = frm._bch_defaults || {};
			const values = {};
			const follow = (fieldname, value) => {
				const current = frm.doc[fieldname] || "";
				if (value && (!current || current === (previous[fieldname] || ""))) {
					values[fieldname] = value;
				}
			};
			follow("expense_account", r.expense_account);
			follow("cost_center", r.cost_center);
			follow("description", r.default_description);
			frm._bch_defaults = {
				expense_account: r.expense_account,
				cost_center: r.cost_center,
				description: r.default_description,
			};
			return bch_set_values(frm, values).then(() => bch_set_account_hint(frm, r.expense_account ? null : category));
		});
}

function bch_set_account_hint(frm, category) {
	// the category has no account for the company: say so under the expense account
	let html = bch_meta_description("expense_account");
	if (category) {
		const link = `<a href="/app/bank-charge-category/${encodeURIComponent(category)}">${bch_esc(__(category))}</a>`;
		html = `<span class="text-warning">${__(
			"Category {0} has no expense account for {1}. Choose one here, or set it in the category.",
			[link, bch_esc(frm.doc.company)]
		)}</span>`;
	}
	bch_set_description(frm, "expense_account", html);
}

function bch_load_charge(frm, apply) {
	// "Refund of": the charge's category, accounts and bank, and what is left to refund
	const name = frm.doc.against_bank_charge;
	return frappe.db
		.get_value("Bank Charge", name, [...BCH_REFUND_FIELDS, "amount", "refunded_amount", "currency"])
		.then((r) => {
			const charge = r && r.message;
			if (!charge || frm.doc.against_bank_charge !== name) {
				return;
			}
			charge.left = flt(flt(charge.amount) - flt(charge.refunded_amount), precision("amount", frm.doc));
			frm._bch_charge = charge;
			if (!apply || frm.doc.docstatus !== 0) {
				bch_set_intro(frm);
				return;
			}
			const values = {};
			BCH_REFUND_FIELDS.forEach((fieldname) => (values[fieldname] = charge[fieldname] || ""));
			if (!flt(frm.doc.amount) && charge.left > 0) {
				values.amount = charge.left;
			}
			return bch_set_values(frm, values).then(() => {
				bch_set_account_hint(frm, null);
				bch_set_intro(frm);
			});
		});
}

function bch_load_refundable(frm) {
	// design P3: when the bank account has few charges, "Refund of" lists only those with something left
	const doc = frm.doc;
	if (doc.docstatus !== 0 || doc.entry_type !== BCH_REFUND || !doc.company || !doc.bank_account) {
		return;
	}
	if (frm._bch_refundable_key === `${doc.company}|${doc.bank_account}`) {
		return;
	}
	frm._bch_refundable_key = `${doc.company}|${doc.bank_account}`;
	frappe.db
		.get_list("Bank Charge", {
			filters: { docstatus: 1, entry_type: BCH_CHARGE, company: doc.company, bank_account: doc.bank_account },
			fields: ["name", "amount", "refunded_amount"],
			limit: BCH_REFUND_QUERY_LIMIT,
		})
		.then((rows) => {
			// as many as the limit: there may be more, the server validates the choice
			frm._bch_refundable =
				rows && rows.length < BCH_REFUND_QUERY_LIMIT
					? rows.filter((row) => flt(row.refunded_amount) < flt(row.amount)).map((row) => row.name)
					: null;
		});
}

// ------------------------------------------------------------------------------------------ display helpers

function bch_esc(value) {
	return frappe.utils.escape_html(value == null ? "" : String(value));
}

function bch_money(value, currency) {
	return `<bdi>${bch_esc(format_currency(flt(value), currency))}</bdi>`;
}

function bch_date(value) {
	return value ? frappe.datetime.str_to_user(value) : "";
}

function bch_account(name) {
	// the account without the company abbreviation ("Bank Charges - TC" → "Bank Charges")
	return String(name || "").replace(/\s+-\s+[^-]+$/, "");
}

function bch_link(doctype, name, cancelled) {
	const route = `/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}`;
	const text = `<bdi>${bch_esc(name)}</bdi>`;
	return `<a href="${route}"><b>${cancelled ? `<s>${text}</s>` : text}</b></a>`;
}

function bch_meta_description(fieldname) {
	const df = (frappe.get_meta("Bank Charge").fields || []).find((d) => d.fieldname === fieldname);
	return df && df.description ? bch_esc(__(df.description)) : "";
}

function bch_set_description(frm, fieldname, html) {
	const field = frm.get_field(fieldname);
	if (field && field.df.description !== html) {
		frm.set_df_property(fieldname, "description", html);
	}
}

function bch_settings(frm) {
	return (frm.doc.__onload && frm.doc.__onload.charge_settings) || {};
}

function bch_company_currency(frm) {
	return (
		bch_settings(frm).company_currency ||
		(window.erpnext && erpnext.get_currency ? erpnext.get_currency(frm.doc.company) : null) ||
		frappe.defaults.get_default("currency")
	);
}

function bch_set_labels(frm) {
	// a refund is a credit of the bank: its date and amount say so
	const refund = frm.doc.entry_type === BCH_REFUND;
	frm.set_df_property(
		"posting_date",
		"label",
		refund ? __("Credit Date", null, BCH_CTX) : __("Posting Date", null, BCH_CTX)
	);
	bch_set_description(
		frm,
		"posting_date",
		refund
			? bch_esc(__("Date the bank credited the refund; posting date of the voucher."))
			: bch_meta_description("posting_date")
	);
	frm.set_df_property("amount", "label", refund ? __("Refund Amount", null, BCH_CTX) : __("Amount", null, BCH_CTX));
}

function bch_toggle_currency_fields(frm) {
	const foreign = !!frm.doc.currency && frm.doc.currency !== bch_company_currency(frm);
	frm.toggle_display("exchange_rate", foreign);
	frm.toggle_display("base_amount", foreign);
}

function bch_indicator(doc) {
	// [label, colour]; the list uses the same rules (bank_charge_list.js)
	if (doc.docstatus === 0) {
		return [__("Draft", null, BCH_CTX), "gray"];
	}
	if (doc.status === "Cancelled" || doc.docstatus === 2) {
		return [__("Cancelled", null, BCH_CTX), "darkgrey"];
	}
	const refunded = flt(doc.refunded_amount);
	if (doc.entry_type === BCH_CHARGE && refunded > 0) {
		return refunded >= flt(doc.amount)
			? [__("Refunded", null, BCH_CTX), "purple"]
			: [__("Partly Refunded", null, BCH_CTX), "yellow"];
	}
	return [__(doc.status, null, BCH_CTX), BCH_INDICATORS[doc.status] || "blue"];
}

// ------------------------------------------------------------------------------------------ intro / headline

function bch_set_intro(frm) {
	const doc = frm.doc;
	frm.set_intro();
	if (doc.docstatus !== 0) {
		return;
	}
	// Record Refund: what is left, until the first save
	if (frm.is_new()) {
		const charge = frm._bch_charge;
		if (doc.entry_type === BCH_REFUND && doc.against_bank_charge && charge) {
			frm.set_intro(
				__("Refund of {0}: {1} left to refund. Type the date and amount the bank credited, then save and submit.", [
					bch_link("Bank Charge", doc.against_bank_charge),
					bch_money(charge.left, charge.currency || doc.currency),
				]),
				"blue"
			);
		}
		return;
	}
	if (frm.is_dirty() || !flt(doc.amount)) {
		return;
	}
	// what Submit will do, on a saved and unchanged draft
	const settings = bch_settings(frm);
	const refund = doc.entry_type === BCH_REFUND;
	const args = [bch_esc(bch_date(doc.posting_date)), bch_money(doc.amount, doc.currency), bch_esc(bch_account(doc.expense_account))];
	let text;
	if (settings.bank_charge_voucher === BCH_JOURNAL_ENTRY) {
		text = refund
			? __("On submit: Journal Entry (Bank Entry) dated {0}. Bank credit {1}, credited to {2}.", args)
			: __("On submit: Journal Entry (Bank Entry) dated {0}. Bank debit {1}, booked to {2}.", args);
	} else {
		text = refund
			? __("On submit: Payment Entry (receipt) dated {0} from the bank. Bank credit {1}, credited to {2}.", args)
			: __(
					"On submit: Payment Entry dated {0} to the bank. Bank debit {1}, booked to {2}. The supplier balance is not affected.",
					args
			  );
	}
	if (doc.bank_transaction) {
		text += " " + __("It is reconciled with statement line {0} at once.", [`<bdi>${bch_esc(doc.bank_transaction)}</bdi>`]);
	} else if (settings.bank_charge_clearance === "On Bank Transaction") {
		text += " " + bch_esc(__("It stays open until it is matched with the bank statement (PDC Bank Clearing)."));
	} else {
		text += " " + bch_esc(__("It is cleared on its posting date."));
	}
	if (refund && !doc.against_bank_charge) {
		text += `<br><span class="text-muted">${bch_esc(
			__("Not linked to a charge: it reduces the category's expense only.")
		)}</span>`;
	}
	// the values are escaped above; the sentences are our own translations
	frm.set_intro(`<div>${text}</div>`, "blue");
}

function bch_headline(frm, html, color) {
	// our own block of the form message area (the intro keeps its own)
	if (frm._bch_$headline) {
		frm._bch_$headline.remove();
		frm._bch_$headline = null;
	}
	if (html) {
		frm._bch_$headline = frm.dashboard.set_headline(html, color);
	}
	return frm._bch_$headline;
}

function bch_set_submitted_headline(frm) {
	// the voucher number (the paper-filing key), the statement line and the refunds, at a glance
	const doc = frm.doc;
	const voucher_type = doc.payment_entry ? "Payment Entry" : doc.journal_entry ? "Journal Entry" : null;
	const voucher = doc.payment_entry || doc.journal_entry;
	if (!voucher) {
		return;
	}
	const cancelled = doc.status === "Cancelled" || doc.docstatus === 2;
	let link = bch_link(voucher_type, voucher, cancelled);
	if (cancelled) {
		link += " " + bch_esc(__("(cancelled)"));
	}
	const parts = [voucher_type === "Payment Entry" ? __("Payment Entry: {0}", [link]) : __("Journal Entry: {0}", [link])];
	const posted = bch_esc(__("Posted {0}", [bch_date(doc.posting_date)]));
	let color = "gray";
	if (!cancelled) {
		if (doc.status === "Reconciled") {
			color = "green";
			const date = doc.bank_transaction_date || doc.clearance_date;
			parts.push(`${posted} • ${bch_esc(__("Reconciled {0}", [bch_date(date)]))}`);
			if (doc.bank_transaction) {
				const line = frappe.model.can_read("Bank Transaction")
					? bch_link("Bank Transaction", doc.bank_transaction)
					: `<bdi>${bch_esc(doc.bank_transaction)}</bdi>`;
				parts.push(__("Statement line {0}", [line]));
			}
		} else if (doc.clearance_date) {
			color = "green";
			parts.push(`${posted} • ${bch_esc(__("Cleared {0}", [bch_date(doc.clearance_date)]))}`);
		} else {
			color = "blue";
			parts.push(posted);
			parts.push(`<i>${bch_esc(__("Awaiting bank statement"))}</i>`);
		}
		if (doc.bank_reference) {
			parts.push(bch_esc(__("Bank Ref. {0}", [doc.bank_reference])));
		}
	}
	const html = `<div>${parts.join(" · ")} &nbsp; <a class="treasury-copy-pe">⧉ ${bch_esc(
		__("Copy", null, "Treasury Voucher")
	)}</a></div>`;
	// every value is escaped above; the sentences are our own translations
	const show = (refund_html, refund_color) => {
		const $headline = bch_headline(frm, html + (refund_html ? `<div>${refund_html}</div>` : ""), refund_color || color);
		$headline && $headline.find(".treasury-copy-pe").on("click", () => frappe.utils.copy_to_clipboard(voucher));
	};

	if (doc.entry_type === BCH_REFUND && doc.against_bank_charge) {
		show(__("Refund of {0}", [bch_link("Bank Charge", doc.against_bank_charge)]));
		return;
	}
	const refunded = flt(doc.refunded_amount);
	if (doc.entry_type !== BCH_CHARGE || refunded <= 0) {
		show(null);
		return;
	}
	show(null);
	// the refunds of the charge: one read, at most five shown
	frappe.db
		.get_list("Bank Charge", {
			filters: { against_bank_charge: doc.name, entry_type: BCH_REFUND, docstatus: 1 },
			fields: ["name"],
			order_by: "posting_date asc",
			limit: 6,
		})
		.then((rows) => {
			if (frm.doc.name !== doc.name) {
				return;
			}
			const names = (rows || []).map((row) => row.name);
			let links = names.slice(0, 5).map((name) => bch_link("Bank Charge", name)).join(", ");
			if (names.length > 5) {
				links += " " + bch_esc(__("+{0} more", [names.length - 5]));
			}
			const full = refunded >= flt(doc.amount);
			let text = __("Refunded {0} of {1}: {2}", [
				bch_money(refunded, doc.currency),
				bch_money(doc.amount, doc.currency),
				links,
			]);
			if (full) {
				text += " · " + bch_esc(__("Fully refunded"));
			}
			show(text, full && !cancelled ? "purple" : null);
		});
}

// ------------------------------------------------------------------------------------------ buttons

function bch_add_buttons(frm) {
	const doc = frm.doc;
	const voucher_type = doc.payment_entry ? "Payment Entry" : doc.journal_entry ? "Journal Entry" : null;
	const voucher = doc.payment_entry || doc.journal_entry;
	const cancelled = doc.status === "Cancelled";
	const view = __("View", null, BCH_CTX);

	if (!cancelled && frappe.user.has_role(BCH_USER_ROLES)) {
		const refundable = flt(doc.refunded_amount) < flt(doc.amount);
		if (doc.entry_type === BCH_CHARGE && refundable && frappe.model.can_create("Bank Charge")) {
			frm.add_custom_button(__("Record Refund", null, BCH_CTX), () => bch_record_refund(frm), __("Create"));
		}
		if (doc.status === "Posted" && frappe.model.can_read("PDC Bank Clearing")) {
			frm.add_custom_button(
				__("Match Bank Statement", null, BCH_CTX),
				() => {
					frappe.route_options = { company: doc.company, bank_account: doc.bank_account };
					frappe.set_route("Form", "PDC Bank Clearing");
				},
				__("Actions", null, BCH_CTX)
			);
		}
		if (doc.status === "Posted" && frappe.model.can_read("Bank Transaction")) {
			frm.add_custom_button(
				__("Reconcile with Statement Line", null, BCH_CTX),
				() => bch_reconcile_dialog(frm),
				__("Actions", null, BCH_CTX)
			);
		}
	}

	if (voucher && frappe.model.can_read(voucher_type)) {
		frm.add_custom_button(
			__(voucher_type, null, BCH_CTX),
			() => frappe.set_route("Form", voucher_type, voucher),
			view
		);
	}
	if (voucher && frappe.model.can_get_report("General Ledger")) {
		frm.add_custom_button(
			__("General Ledger", null, BCH_CTX),
			() =>
				frappe.set_route("query-report", "General Ledger", {
					company: doc.company,
					voucher_no: voucher,
					from_date: doc.posting_date,
					to_date: doc.posting_date,
				}),
			view
		);
	}
	if (doc.entry_type === BCH_REFUND && doc.against_bank_charge) {
		frm.add_custom_button(
			__("Refunded Charge", null, BCH_CTX),
			() => frappe.set_route("Form", "Bank Charge", doc.against_bank_charge),
			view
		);
	}
}

function bch_record_refund(frm) {
	// nothing is written until the user saves the refund
	frappe.xcall(BCH_METHOD + "make_refund", { source_name: frm.doc.name }).then((refund) => {
		const synced = frappe.model.sync(refund)[0];
		frappe.set_route("Form", synced.doctype, synced.name);
	});
}

function bch_reconcile_dialog(frm) {
	// design P5: reconcile one charge with its statement line without the clearing tool
	const doc = frm.doc;
	const side = doc.entry_type === BCH_REFUND ? "deposit" : "withdrawal";
	const label = __("Reconcile with Statement Line", null, BCH_CTX);
	const dialog = new frappe.ui.Dialog({
		title: label,
		fields: [
			{
				fieldname: "bank_transaction",
				fieldtype: "Link",
				label: __("Bank Transaction", null, BCH_CTX),
				options: "Bank Transaction",
				reqd: 1,
				get_query: () => ({
					filters: {
						bank_account: doc.bank_account,
						docstatus: 1,
						status: ["in", ["Pending", "Unreconciled"]],
						unallocated_amount: flt(doc.amount),
						[side]: [">", 0],
					},
				}),
			},
			{
				fieldname: "info",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${bch_esc(
					__("The voucher gets the statement date as its clearance date.")
				)}</p>`,
			},
		],
		primary_action_label: __("Reconcile", null, BCH_CTX),
		primary_action(values) {
			dialog.hide();
			frm.call({
				doc: frm.doc,
				method: "reconcile_bank_transaction",
				args: { bank_transaction: values.bank_transaction },
				freeze: true,
			}).then(() => {
				frappe.show_alert({
					message: bch_esc(__("Reconciled with {0}.", [values.bank_transaction])),
					indicator: "green",
				});
				frm.reload_doc();
			});
		},
	});
	dialog.show();
}

// ------------------------------------------------------------------------------------------ status log

function bch_translate_log(frm) {
	// events and statuses are stored in English in the status log (Data fields): translate them on display
	const contexts = {
		event: BCH_LOG_CTX,
		from_status: BCH_CTX,
		to_status: BCH_CTX,
		from_accounting: BCH_CTX,
		to_accounting: BCH_CTX,
	};
	const formatters = {};
	Object.entries(contexts).forEach(([fieldname, context]) => {
		formatters[fieldname] = (value) => bch_esc(value ? __(value, null, context) : "");
	});
	// design P7: the refund named in the remarks of a Refunded / Refund Cancelled row links to it
	formatters.remarks = (value, df, options, row) => {
		const raw = row && row.doctype === BCH_LOG_CTX ? row.remarks || "" : value || "";
		if (row && ["Refunded", "Refund Cancelled"].includes(row.event) && /^[\w.-]+$/.test(raw)) {
			return bch_link("Bank Charge", raw);
		}
		return bch_esc(raw);
	};
	Object.entries(formatters).forEach(([fieldname, formatter]) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield(BCH_LOG_CTX, fieldname, name);
			if (df) {
				df.formatter = formatter;
			}
		}
	});
	frm.refresh_field("status_log");
}

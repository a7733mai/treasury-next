// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/security_cheques/lifecycle.py; the buttons below only mirror them.
const SEC_LIVE = ["Received", "In Custody", "Issued"];
const SEC_FINAL = ["Returned", "Encashed", "Lost", "Cancelled"];

frappe.ui.form.on("Security Cheque", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("cheque_book", () => ({
			filters: { company: frm.doc.company, bank_account: frm.doc.bank_account, status: "Active" },
		}));
		frm.set_query("reference_doctype", () => ({
			filters: { name: ["in", frm.sec_reference_doctypes || []] },
		}));
		frappe
			.xcall("treasury.security_cheques.doctype.security_cheque.security_cheque.reference_doctypes")
			.then((doctypes) => {
				frm.sec_reference_doctypes = doctypes;
			});
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_user_default("Company"));
		} else if (frm.is_new()) {
			// replace the user / site default currency Frappe puts in every new `currency` field
			frm.events.set_currency(frm);
		}
	},

	company(frm) {
		if (frm.is_new()) {
			frm.events.set_currency(frm);
		}
	},

	set_currency(frm) {
		// mirrors SecurityCheque.set_currency: outgoing = bank GL currency, incoming = company currency
		if (frm.doc.direction === "Outgoing") {
			if (!frm.doc.bank_account) {
				return;
			}
			frappe.db
				.get_value("Bank Account", frm.doc.bank_account, "account")
				.then((r) => r.message && r.message.account)
				.then((account) => account && frappe.db.get_value("Account", account, "account_currency"))
				.then((r) => r && r.message && frm.set_value("currency", r.message.account_currency));
		} else if (frm.doc.company) {
			frappe.db
				.get_value("Company", frm.doc.company, "default_currency")
				.then((r) => r.message && frm.set_value("currency", r.message.default_currency));
		}
	},

	refresh(frm) {
		frm.events.lock_identity_fields(frm);
		frm.events.translate_logs(frm);
		frm.events.set_indicator(frm);
		if (!frm.is_new()) {
			frm.events.add_actions(frm);
			frm.add_custom_button(__("Print Receipt"), () => {
				const params = new URLSearchParams({
					doctype: frm.doctype,
					name: frm.doc.name,
					format: "Security Cheque Receipt",
					_lang: frappe.boot.lang || "",
				});
				window.open(`/printview?${params.toString()}`);
			});
		}
	},

	direction(frm) {
		if (frm.doc.direction === "Incoming") {
			frm.set_value("bank_account", "");
			frm.set_value("cheque_book", "");
		} else {
			frm.set_value("drawer_bank", "");
		}
		frm.events.set_currency(frm);
	},

	party_type(frm) {
		frm.set_value("party", "");
		frm.set_value("party_name_other", "");
	},

	bank_account(frm) {
		frm.set_value("cheque_book", "");
		frm.events.set_currency(frm);
	},

	amount_type(frm) {
		if (frm.doc.amount_type === "Open / Blank") {
			frm.set_value("amount", 0);
		}
	},

	reference_doctype(frm) {
		frm.set_value("reference_name", "");
	},

	lock_identity_fields(frm) {
		// mirrors SecurityCheque.validate_locked_fields: fixed once received / issued
		const locked = !frm.is_new() && frm.doc.status !== "Created";
		const final = !frm.is_new() && SEC_FINAL.includes(frm.doc.status);
		[
			"direction",
			"company",
			"party_type",
			"party",
			"party_name_other",
			"bank_account",
			"cheque_book",
			"cheque_no",
			"drawer_bank",
			"amount_type",
			"amount",
			"currency",
			"issue_date",
			"cheque_date",
			"expiry_date", // then moved by the Extend Expiry action only
		].forEach((fieldname) => frm.set_df_property(fieldname, "read_only", locked ? 1 : 0));
		frm.set_df_property("currency", "read_only", locked || frm.doc.direction === "Outgoing" ? 1 : 0);
		// what the cheque guaranteed is frozen once it is final
		["required_amount", "reference_doctype", "reference_name"].forEach((fieldname) =>
			frm.set_df_property(fieldname, "read_only", final ? 1 : 0)
		);
	},

	translate_logs(frm) {
		// statuses are stored in English in the status log (Data fields): translate them on display
		["from_status", "to_status"].forEach((fieldname) => {
			for (const name of [null, frm.doc.name]) {
				const df = frappe.meta.get_docfield("Security Cheque Status Log", fieldname, name);
				if (df) {
					df.formatter = (value) =>
						frappe.utils.escape_html(value ? __(value, null, "Security Cheque") : "");
				}
			}
		});
		frm.refresh_field("status_log");
	},

	set_indicator(frm) {
		if (frm.is_new()) {
			return;
		}
		const expired =
			SEC_LIVE.includes(frm.doc.status) &&
			frm.doc.expiry_date &&
			frm.doc.expiry_date < frappe.datetime.get_today();
		if (expired) {
			frm.page.set_indicator(__("Expired"), "red");
		} else {
			frm.page.set_indicator(__(frm.doc.status, null, "Security Cheque"), "blue");
		}
	},

	add_actions(frm) {
		if (frm.is_dirty()) {
			return;
		}
		const doc = frm.doc;
		const status = doc.status;
		const incoming = doc.direction === "Incoming";
		const is_manager = frappe.user.has_role("Treasury Manager");
		const date = (fieldname, label) => ({
			fieldname,
			label,
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		});
		const reason = { fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1 };
		const remarks = { fieldname: "remarks", label: __("Remarks"), fieldtype: "Small Text" };
		const location = (reqd) => ({
			fieldname: "custody_location",
			label: __("Custody Location"),
			fieldtype: "Link",
			options: "Custody Location",
			reqd,
			get_query: () => ({ filters: { is_active: 1 } }),
		});
		const custodian = {
			fieldname: "custodian",
			label: __("Custodian"),
			fieldtype: "Link",
			options: "User",
		};

		if (status === "Created") {
			if (incoming) {
				sec_action(frm, "receive", __("Receive", null, "Security Cheque"), [
					date("receipt_date", __("Receipt Date")),
					location(0),
					custodian,
					remarks,
				]);
			} else {
				sec_action(frm, "issue", __("Issue", null, "Security Cheque"), [
					date("handover_date", __("Handover Date")),
					remarks,
				]);
			}
			sec_action(frm, "cancel_cheque", __("Cancel Cheque"), [reason]);
		}
		if (incoming && status === "Received") {
			sec_action(frm, "store", __("Put in Custody"), [
				date("store_date", __("Date")),
				location(1),
				custodian,
				remarks,
			]);
		}
		if (incoming && status === "In Custody") {
			sec_action(frm, "transfer_custody", __("Transfer Custody"), [
				date("transfer_date", __("Date")),
				location(1),
				custodian,
				remarks,
			]);
		}
		if (SEC_LIVE.includes(status)) {
			sec_action(frm, "return_cheque", __("Return Cheque"), [
				date("return_date", __("Return Date")),
				{
					fieldname: "person",
					label: incoming ? __("Returned To") : __("Received By"),
					fieldtype: "Data",
				},
				reason,
			]);
			sec_action(frm, "extend_expiry", __("Extend Expiry"), [
				{
					fieldname: "new_date",
					label: __("New Expiry Date"),
					fieldtype: "Date",
					reqd: 1,
					default: doc.expiry_date,
				},
				reason,
			]);
			if (is_manager) {
				sec_action(frm, "mark_lost", __("Mark as Lost"), [date("lost_date", __("Lost Date")), reason]);
				frm.add_custom_button(__("Encash"), () => sec_encash(frm), __("Actions"));
			}
		}
	},
});

function sec_action(frm, method, label, fields) {
	frm.add_custom_button(
		label,
		() => {
			frappe.prompt(
				fields,
				(values) => {
					frm.call({ method, args: values, freeze: true }).then(() => frm.reload_doc());
				},
				label,
				label
			);
		},
		__("Actions")
	);
}

function sec_encash(frm) {
	if (frm.doc.direction !== "Incoming") {
		sec_encash_prompt(frm, []);
		return;
	}
	// the deposit bank must be in the currency of the security cheque
	frappe
		.xcall("treasury.security_cheques.doctype.security_cheque.security_cheque.deposit_bank_accounts", {
			security_cheque: frm.doc.name,
		})
		.then((bank_accounts) => sec_encash_prompt(frm, bank_accounts || []));
}

function sec_encash_prompt(frm, bank_accounts) {
	const doc = frm.doc;
	const incoming = doc.direction === "Incoming";
	frappe.prompt(
		[
			{
				fieldname: "info",
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"A PDC Cheque is created for the encashment and posted per Treasury Company Settings. The security cheque itself never posts."
				)}</p>`,
			},
			{
				fieldname: "encashment_date",
				label: __("Encashment Date"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.get_today(),
			},
			{
				fieldname: "amount",
				label: __("Amount"),
				fieldtype: "Currency",
				options: doc.currency,
				default: doc.amount_type === "Fixed" ? doc.amount : null,
				read_only: doc.amount_type === "Fixed" ? 1 : 0,
				reqd: 1,
			},
			{
				fieldname: "due_date",
				label: __("Due Date"),
				fieldtype: "Date",
				default: frappe.datetime.get_today(),
			},
			{
				fieldname: "bank_account",
				label: __("Deposit to Bank Account"),
				fieldtype: "Link",
				options: "Bank Account",
				reqd: incoming ? 1 : 0,
				hidden: incoming ? 0 : 1,
				description: __("Bank accounts in {0}", [doc.currency]),
				get_query: () => ({ filters: { name: ["in", bank_accounts] } }),
			},
			{
				fieldname: "reference_doctype",
				label: __("Settle Invoice Type"),
				fieldtype: "Select",
				options: [
					"",
					...(incoming ? ["Sales Invoice", "Sales Order"] : ["Purchase Invoice", "Purchase Order"]),
				],
				description: __("Optional. Without an invoice the amount is posted as an advance of the party."),
			},
			{
				fieldname: "reference_name",
				label: __("Settle Invoice"),
				fieldtype: "Dynamic Link",
				options: "reference_doctype",
				depends_on: "eval:doc.reference_doctype",
			},
			{ fieldname: "remarks", label: __("Remarks"), fieldtype: "Small Text" },
		],
		(values) => {
			const args = {
				encashment_date: values.encashment_date,
				amount: values.amount,
				due_date: values.due_date,
				bank_account: values.bank_account,
				remarks: values.remarks,
			};
			if (values.reference_doctype && values.reference_name) {
				args.references = [
					{
						reference_doctype: values.reference_doctype,
						reference_name: values.reference_name,
						allocated_amount: values.amount,
					},
				];
			}
			frappe.confirm(__("Encash this security cheque? This cannot be undone."), () => {
				frm.call({ method: "encash", args, freeze: true }).then((r) => {
					if (r.message && r.message.pdc_cheque) {
						frappe.set_route("Form", "PDC Cheque", r.message.pdc_cheque);
					} else {
						frm.reload_doc();
					}
				});
			});
		},
		__("Encash"),
		__("Encash")
	);
}

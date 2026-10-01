// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Loaded through hooks.py `doctype_js` on the custody app's Custody Request (plain file, no bundle; never loaded
// without custody), after public/js/treasury_payment_buttons.js (its `treasury.payment_buttons.open`). A
// "Treasury" button group opens a new PDC Cheque or Bank Transfer prepared by a read-only server mapper
// (treasury.integrations.custody.api); the amounts are hints only, the server checks everything again when the
// document is saved.
//
// Custody draws its own headline on the request (custody.ui.headline): Treasury never calls set_headline /
// set_intro here. It adds one extra block (`set_headline_alert`, what `add_comment` does), kept in
// `frm.__treasury_headline` and removed at the start of each refresh (design 9 §0.1).
frappe.provide("treasury.custody_request");

(() => {
	const CTX = "Custody Request";
	const OUTGOING = "Outgoing";
	const INCOMING = "Incoming";
	const TRANSFER_DEFAULTS = ["Bank Transfer", "Fawran"];
	const SHOWN = 3;
	const esc = (value) => frappe.utils.escape_html(value == null ? "" : String(value));
	const bdi = (value) => `<bdi>${esc(value)}</bdi>`;

	const labels = () => ({
		cheque: __("Pay by Cheque", null, CTX),
		transfer: __("Pay by Transfer", null, CTX),
		receive: __("Receive Cheque", null, CTX),
	});

	const check_saved = (frm) => {
		if (frm.is_dirty()) {
			frappe.throw(__("You have unsaved changes in this form. Please save before you continue."));
		}
	};

	// client only (never saved): the target form's intro names the request and custody's purpose
	const from = (frm, side) => ({ doctype: frm.doctype, name: frm.doc.name, purpose: side && side.purpose });

	const open_cheque = (frm, ctx, direction) => {
		check_saved(frm);
		treasury.payment_buttons.open(
			"treasury.integrations.custody.api.make_pdc_cheque",
			{ custody_request: frm.doc.name, direction },
			{ target: "PDC Cheque", from: from(frm, direction === OUTGOING ? ctx.pay : ctx.receive) }
		);
	};

	// ------------------------------------------------------------------------------ Pay by Transfer dialog

	const fee_preview = (frm, ctx, dialog) => {
		const method = dialog.get_value("method");
		const field = dialog.fields_dict.fee_preview;
		const bank = (ctx.transfer_banks || {})[method];
		if (!bank) {
			field.$wrapper.html(
				`<p class="small" style="color: var(--text-on-orange)">${esc(
					__("No default bank for this method: you choose it on the transfer.")
				)}</p>`
			);
			return;
		}
		field.$wrapper.html("");
		frappe
			.xcall("treasury.bank_transfers.api.get_fee_preview", {
				company: frm.doc.company,
				bank_account: bank,
				method,
				transfer_type: "Party Payment",
				amount: ctx.pay.amount,
				party_type: frm.doc.custodian ? "Custodian" : null,
				party: frm.doc.custodian,
			})
			.then((r) => {
				// another method chosen meanwhile, or the dialog closed
				if (!r || dialog.get_value("method") !== method) return;
				const fee = flt(r.computed_fee);
				const currency = r.currency || ctx.currency;
				const text = fee
					? __("Fee {0} via {1}, posted separately. Bank debit {2}.", [
							bdi(format_currency(fee, currency)),
							bdi(bank),
							bdi(format_currency(flt(ctx.pay.amount) + fee, currency)),
					  ])
					: __("No fee on {0}.", [bdi(bank)]);
				// the values are escaped above; the sentences are our own translations
				field.$wrapper.html(`<p class="text-muted small">${text}</p>`);
			})
			.catch(() => field.$wrapper.html(""));
	};

	const transfer_dialog = (frm, ctx) => {
		check_saved(frm);
		const summary = __("Amount {0} to {1} · {2}", [
			bdi(format_currency(ctx.pay.amount, ctx.currency)),
			bdi(ctx.custodian_name || frm.doc.custodian),
			esc(ctx.pay.purpose ? __(ctx.pay.purpose) : ""),
		]);
		const dialog = new frappe.ui.Dialog({
			title: __("Pay Custody by Transfer"),
			fields: [
				{
					fieldname: "method",
					fieldtype: "Select",
					label: __("Method", null, "Bank Transfer"),
					options: [
						{ value: "Normal", label: __("Normal", null, "Bank Transfer") },
						{ value: "Fawran", label: __("Fawran", null, "Bank Transfer") },
					],
					default: ctx.default_method === "Fawran" ? "Fawran" : "Normal",
					reqd: 1,
					change: () => fee_preview(frm, ctx, dialog),
				},
				// the values are escaped above; the sentence is our own translation
				{ fieldname: "summary", fieldtype: "HTML", options: `<p>${summary}</p>` },
				{ fieldname: "fee_preview", fieldtype: "HTML" },
				{
					fieldname: "help",
					fieldtype: "HTML",
					options: `<p class="text-muted small">${esc(
						__(
							"The transfer opens as a draft: check the IBAN and save. For a custodian, the fee is posted on its own voucher."
						)
					)}</p>`,
				},
			],
			primary_action_label: __("Create Transfer"),
			primary_action(values) {
				dialog.hide();
				treasury.payment_buttons.open(
					"treasury.integrations.custody.api.make_bank_transfer",
					{ custody_request: frm.doc.name, method: values.method },
					{ target: "Bank Transfer", from: from(frm, ctx.pay) }
				);
			},
		});
		dialog.show();
		fee_preview(frm, ctx, dialog);
	};

	// ------------------------------------------------------------------------------ buttons

	const add_buttons = (frm, ctx) => {
		const names = labels();
		const pay = flt(ctx.pay.amount) > 0;
		const receive = flt(ctx.receive.amount) > 0;
		const buttons = [];
		if (pay && ctx.can_cheque) {
			buttons.push(["cheque", () => open_cheque(frm, ctx, OUTGOING)]);
		}
		if (pay && ctx.can_transfer) {
			const transfer = ["transfer", () => transfer_dialog(frm, ctx)];
			// the company's default custody payout method comes first
			TRANSFER_DEFAULTS.includes(ctx.default_method) ? buttons.unshift(transfer) : buttons.push(transfer);
		}
		if (receive && ctx.can_cheque) {
			buttons.push(["receive", () => open_cheque(frm, ctx, INCOMING)]);
		}
		buttons.forEach(([key, action]) => frm.add_custom_button(names[key], action, __("Treasury")));

		// "+" in Connections clicks our button (the same prefilled path), and shows only when the button does.
		// Keyed by the label as shown: Frappe looks the button up by `__(label)`, which keeps a translated label.
		frm.custom_make_buttons = Object.assign(frm.custom_make_buttons || {}, {
			"PDC Cheque": pay ? names.cheque : names.receive,
			"Bank Transfer": names.transfer,
		});
		frm.dashboard && frm.dashboard.after_refresh && frm.dashboard.after_refresh();
		return buttons.length;
	};

	// ------------------------------------------------------------------------------ Treasury block

	const clear_block = (frm) => {
		if (frm.__treasury_headline) {
			frm.__treasury_headline.remove();
			frm.__treasury_headline = null;
		}
	};

	const document_line = (row, currency) => {
		const href = frappe.utils.get_form_link(row.doctype, row.name);
		const link = `<a href="${esc(href)}"><b>${bdi(row.name)}</b></a>`;
		const amount = bdi(format_currency(row.amount, currency));
		const status = esc(__(row.status, null, row.doctype));
		let line =
			row.doctype === "PDC Cheque"
				? __("Cheque {0} · {1} · {2}", [link, amount, status])
				: __("Transfer {0} · {1} · {2}", [link, amount, status]);
		if (row.payment_entry) {
			const pe = `<a href="${esc(frappe.utils.get_form_link("Payment Entry", row.payment_entry))}">${bdi(
				row.payment_entry
			)}</a>`;
			line += " · " + __("PE {0}", [pe]);
		}
		return line;
	};

	const show_block = (frm, ctx, shown_buttons) => {
		const docs = ctx.open_documents || [];
		let html = null;
		let color = "blue";
		if (docs.length) {
			const lines = docs.slice(0, SHOWN).map((row) => document_line(row, ctx.currency));
			if (ctx.more_documents) {
				lines.push(esc(__("+{0} more", [ctx.more_documents])));
			}
			html = __("Treasury: {0}", [lines.join("<br>")]);
		} else if (!shown_buttons) {
			// a draft replenishment / open documents first (custody would accept a payment), then the rest
			const sides = [ctx.pay, ctx.receive].filter((s) => s && s.reason);
			const side = sides.find((s) => s.blocked) || sides[0];
			if (side) {
				html = esc(strip_html(side.reason));
				// "white": Frappe's grey block
				color = side.draft_payment_entry ? "orange" : "white";
				if (side.draft_payment_entry) {
					const href = frappe.utils.get_form_link("Payment Entry", side.draft_payment_entry);
					html += ` <a class="btn btn-xs btn-default" href="${esc(href)}">${bdi(side.draft_payment_entry)}</a>`;
				}
			}
		}
		if (!html) return;
		// every value is escaped above; the sentences are our own translations. `add_comment` (permanent) is
		// `set_headline_alert`, which also returns the block, so the next refresh removes exactly this one.
		frm.__treasury_headline = frm.dashboard.set_headline_alert(html, color, true);
	};

	frappe.ui.form.on(CTX, {
		refresh(frm) {
			clear_block(frm);
			if (frm.is_new() || frm.doc.docstatus !== 1 || frm.doc.is_closed) return;
			if (!frappe.model.can_create("PDC Cheque") && !frappe.model.can_create("Bank Transfer")) return;
			const name = frm.doc.name;
			frappe.xcall("treasury.integrations.custody.api.get_payment_context", { custody_request: name }).then((ctx) => {
				// the form may show another request by now
				if (!ctx || frm.doc.name !== name) return;
				clear_block(frm);
				const shown = add_buttons(frm, ctx);
				show_block(frm, ctx, shown);
			});
		},
	});
})();

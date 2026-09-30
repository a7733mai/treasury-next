// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Form scripts share the global scope: the names below are prefixed `TS_` / `ts_`.
const TS_RECIPIENT = "Treasury Alert Recipient";
const TS_ALERT_ROLES = ["Treasury Manager", "System Manager"];
const TS_ALERTS_METHOD = "treasury.balance_alerts.notify.";

frappe.ui.form.on("Treasury Settings", {
	setup(frm) {
		// New Payee creates Suppliers in this group: a leaf group only
		frm.set_query("payee_supplier_group", () => ({ filters: { is_group: 0 } }));
		frm.set_query("supplier", "bank_suppliers", () => ({ filters: { disabled: 0 } }));
		// Bank Position Alerts: desk roles and enabled system users
		frm.set_query("role", "balance_alert_recipients", () => ({ filters: { disabled: 0, desk_access: 1 } }));
		frm.set_query("user", "balance_alert_recipients", () => ({
			filters: { enabled: 1, user_type: "System User" },
		}));
	},

	refresh(frm) {
		// design 7d P12: an empty Bank Suppliers table is the normal case (shown only while it is empty)
		const field = frm.get_field("bank_suppliers_hint");
		field &&
			field.$wrapper.html(
				`<p class="text-muted small">${frappe.utils.escape_html(
					__(
						"Empty is fine: the first charge posted on a bank creates a Supplier named after the Bank (group: Payee Supplier Group) and adds the row here. Add a row only to reuse an existing Supplier."
					)
				)}</p>`
			);
		ts_format_recipients(frm);
		ts_alert_hints(frm);
		ts_check_outgoing_email(frm);
		ts_preview_recipients(frm);
		ts_add_alert_buttons(frm);
	},

	send_balance_digest: (frm) => ts_alert_hints(frm),
	send_balance_alerts: (frm) => ts_alert_hints(frm),
	balance_alert_notification: (frm) => ts_alert_hints(frm),
	balance_digest_hour: (frm) => ts_alert_hints(frm),
	balance_alert_email(frm) {
		ts_alert_hints(frm);
		ts_check_outgoing_email(frm);
	},
});

// the hint's depends_on follows the rows as they are added or removed
const TS_REFRESH_HINT = (frm) => frm.layout && frm.layout.refresh_dependency && frm.layout.refresh_dependency();
frappe.ui.form.on("Treasury Bank Supplier", {
	bank_suppliers_add: TS_REFRESH_HINT,
	bank_suppliers_remove: TS_REFRESH_HINT,
});

frappe.ui.form.on(TS_RECIPIENT, {
	balance_alert_recipients_add(frm, cdt, cdn) {
		// a new row is a role row
		if (!locals[cdt][cdn].recipient_type) {
			frappe.model.set_value(cdt, cdn, "recipient_type", "Role");
		}
		ts_alert_hints(frm);
	},
	balance_alert_recipients_remove: (frm) => ts_alert_hints(frm),
	recipient_type(frm, cdt, cdn) {
		// the value of the other type is cleared (the server does it too)
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, row.recipient_type === "User" ? "role" : "user", "");
	},
});

// ---------------------------------------------------------------------------------- Bank Position Alerts

function ts_format_recipients(frm) {
	// the column of the other type shows a muted dash; the type is translated
	const esc = frappe.utils.escape_html;
	const dash = '<span class="text-muted">—</span>';
	const formatters = {
		recipient_type: (value) => esc(value ? __(value, null, TS_RECIPIENT) : ""),
		role: (value, df, options, row) => (row && row.recipient_type === "User" ? dash : esc(value ? __(value) : "")),
		user: (value, df, options, row) => (row && row.recipient_type !== "User" ? dash : esc(value || "")),
	};
	Object.entries(formatters).forEach(([fieldname, formatter]) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield(TS_RECIPIENT, fieldname, name);
			if (df) {
				df.formatter = formatter;
			}
		}
	});
	frm.refresh_field("balance_alert_recipients");
}

function ts_alert_hints(frm) {
	const doc = frm.doc;
	// design 7e P5: when the next digest goes out (system time zone)
	const hour = cint(doc.balance_digest_hour);
	const now = frappe.datetime.system_datetime(true);
	const time = `${String(hour).padStart(2, "0")}:00`;
	const next = now.hours() < hour ? __("today {0}", [time]) : __("tomorrow {0}", [time]);
	frm.set_df_property(
		"balance_digest_hour",
		"description",
		__("Hour of day (0–23, system time zone) after which the daily digest is sent. Next digest: {0}.", [next])
	);
	frm.set_df_property(
		"balance_alert_horizon",
		"description",
		`${__("Today and the next days, 1–7.")} ${__("Today counts as day 1: 7 = today and the next 6 days.")}`
	);
	// nothing would be sent: say so on top of the form
	if (!doc.balance_alert_notification && !doc.balance_alert_email) {
		frm.set_intro(__("Both channels are off: no digest or alert is sent."), "orange");
	} else if (!doc.send_balance_digest && !doc.send_balance_alerts) {
		frm.set_intro(__("The daily digest and critical alerts are both off."), "orange");
	} else {
		frm.set_intro();
	}
	const empty = !(doc.balance_alert_recipients || []).length;
	frm.set_df_property(
		"balance_alert_recipients",
		"description",
		empty
			? `<span class="text-warning">${frappe.utils.escape_html(
					__("No recipients: nobody receives the digest or alerts.")
			  )}</span>`
			: __("Roles and / or users. Each recipient only gets the companies and bank accounts they may see.")
	);
}

function ts_check_outgoing_email(frm) {
	// design 7e P6: e-mail is on but nothing can be sent (only users who may read Email Account can tell)
	const set = (missing) =>
		frm.set_df_property(
			"balance_alert_email",
			"description",
			missing
				? `<span class="text-warning">${frappe.utils.escape_html(
						__("No default outgoing e-mail account: only in-system notifications are sent.")
				  )}</span>`
				: __("Sent only when a default outgoing e-mail account exists.")
		);
	if (!frm.doc.balance_alert_email || !frappe.model.can_read("Email Account")) {
		set(false);
		return;
	}
	frappe.db
		.get_list("Email Account", { filters: { default_outgoing: 1, enable_outgoing: 1 }, fields: ["name"], limit: 1 })
		.then((rows) => set(!(rows || []).length));
}

function ts_preview_recipients(frm) {
	// design 7e P7: the roles made concrete (saved rows only)
	const grid = frm.fields_dict.balance_alert_recipients;
	if (!grid || frm.is_dirty() || !frappe.user.has_role(TS_ALERT_ROLES)) {
		return;
	}
	grid.$wrapper.find(".treasury-recipients-preview").remove();
	frappe.xcall("treasury.balance_alerts.recipients.preview_recipients", {}, "GET").then((r) => {
		if (!r || !r.count) {
			return;
		}
		const esc = frappe.utils.escape_html;
		let names = r.names.map((name) => `<bdi>${esc(name)}</bdi>`).join(", ");
		if (r.more) {
			names += ` ${esc(__("+{0} more", [r.more]))}`;
		}
		grid.$wrapper.find(".treasury-recipients-preview").remove();
		// the names are escaped above; the sentence is our own translation
		grid.$wrapper.append(
			`<p class="text-muted small treasury-recipients-preview">${__("Resolves to {0} users: {1}", [
				r.count,
				names,
			])}</p>`
		);
	});
}

function ts_add_alert_buttons(frm) {
	// design 7e P1: see what a user gets, send it to yourself, check the alerts now
	if (frm.is_dirty() || !frappe.user.has_role(TS_ALERT_ROLES)) {
		return;
	}
	const group = __("Bank Position Alerts", null, "Treasury Settings");
	frm.add_custom_button(__("Preview Digest"), () => ts_preview_digest_dialog(), group);
	frm.add_custom_button(__("Send Test to Me"), () => ts_company_dialog(__("Send Test to Me"), ts_send_test), group);
	frm.add_custom_button(
		__("Run Alerts Now"),
		() =>
			ts_company_dialog(
				__("Run Alerts Now"),
				ts_run_alerts,
				__(
					"Evaluates the thresholds now and sends any new critical or warning alert. Alerts already sent today are not repeated."
				)
			),
		group
	);
}

function ts_company_field() {
	return {
		fieldname: "company",
		label: __("Company"),
		fieldtype: "Link",
		options: "Company",
		reqd: 1,
		default: frappe.defaults.get_user_default("Company"),
	};
}

function ts_company_dialog(title, action, info) {
	const fields = [ts_company_field()];
	if (info) {
		fields.push({
			fieldname: "info",
			fieldtype: "HTML",
			options: `<p class="text-muted small">${frappe.utils.escape_html(info)}</p>`,
		});
	}
	const dialog = new frappe.ui.Dialog({
		title,
		fields,
		primary_action_label: title,
		primary_action(values) {
			dialog.hide();
			action(values.company);
		},
	});
	dialog.show();
}

function ts_preview_digest_dialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Preview Digest"),
		fields: [
			ts_company_field(),
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
			ts_show_digest(values.company, values.user);
		},
	});
	dialog.show();
}

function ts_show_digest(company, user) {
	const esc = frappe.utils.escape_html;
	frappe.xcall(TS_ALERTS_METHOD + "preview_digest", { company, user }).then((r) => {
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

function ts_send_test(company) {
	frappe.xcall(TS_ALERTS_METHOD + "send_test_digest", { company }).then((r) => {
		if (r.sent) {
			frappe.show_alert({ message: frappe.utils.escape_html(r.message), indicator: "green" });
		} else {
			frappe.msgprint({ message: frappe.utils.escape_html(r.message || ""), indicator: "orange" });
		}
	});
}

function ts_run_alerts(company) {
	frappe.xcall(TS_ALERTS_METHOD + "run_alerts_now", { company }).then((r) => {
		frappe.show_alert({ message: frappe.utils.escape_html(r.message), indicator: "blue" });
	});
}

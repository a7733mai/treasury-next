// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in cheque_book.py (Cancel Leaf, Void Leaf) and treasury/pdc/cheque_leaves.py (the leaf
// summary); this script only calls them. Form scripts share the global scope: the names below are prefixed.
const CHEQUE_BOOK_METHOD = "treasury.pdc.doctype.cheque_book.cheque_book.";
// cheque_book.VOID_REASON_TYPES / MAX_VOID_RANGE (stored in English, translated with the child DocType as context)
const CHEQUE_BOOK_VOID_REASON_TYPES = ["Damaged", "Misprinted", "Lost", "Stolen", "Other"];
const CHEQUE_BOOK_MAX_VOID = 100;
const CHEQUE_BOOK_LOW_REMAINING = 5;
const CHEQUE_BOOK_LEAF_CTX = "Cheque Book Cancelled Leaf";

frappe.ui.form.on("Cheque Book", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
	},

	refresh(frm) {
		cheque_book_leaf_grid(frm);
		if (frm.is_new()) {
			return;
		}
		frm.events.show_leaf_summary(frm);
		frm.add_custom_button(__("Leaf Register"), () => cheque_book_open_register(frm), __("View"));
		if (frappe.model.can_print("Cheque Book", frm)) {
			frm.add_custom_button(
				__("Print Leaves"),
				() => {
					const params = new URLSearchParams({
						doctype: frm.doctype,
						name: frm.doc.name,
						format: "Cheque Book Leaves",
						no_letterhead: 0,
						_lang: frappe.boot.lang || "",
					});
					window.open(`/printview?${params.toString()}`);
				},
				__("Print", null, "PDC Cheque")
			);
		}
		if (!frm.has_perm("write")) {
			return;
		}
		if (frm.doc.status !== "Closed") {
			frm.add_custom_button(__("Cancel Leaf"), () => cheque_book_cancel_leaf(frm), __("Actions"));
		}
		// record keeping: a leaf of any book (a closed one too) may be voided
		frm.add_custom_button(__("Void Leaf"), () => cheque_book_void_leaves(frm), __("Actions"));
		if (frm.doc.status !== "Closed") {
			frm.add_custom_button(
				__("Close Book"),
				() => {
					frappe.confirm(__("Close this cheque book? No more cheque numbers will be allocated from it."), () =>
						frm.call("close").then(() => frm.reload_doc())
					);
				},
				__("Actions")
			);
		}
	},

	show_leaf_summary(frm) {
		// cheque_leaves.book_summary: {total, used, cancelled, void, remaining}
		const summary = frm.doc.__onload && frm.doc.__onload.leaf_summary;
		const status = frm.doc.status;
		if (status === "Closed") {
			frm.page.set_indicator(__("Closed", null, "Cheque Book"), "gray");
		} else if (status === "Exhausted") {
			frm.page.set_indicator(__("Exhausted", null, "Cheque Book"), "orange");
		} else if (summary) {
			frm.page.set_indicator(
				__("Active · {0} left", [summary.remaining]),
				summary.remaining > CHEQUE_BOOK_LOW_REMAINING ? "green" : "orange"
			);
		}
		if (!summary || !summary.total) {
			return;
		}
		// design 7f P1: one bar, Used · Cancelled · Void, the rest of the track is what remains
		const width = (count) => `${Math.round((count / summary.total) * 10000) / 100}%`;
		const segments = [
			{ count: summary.used, progress_class: "progress-bar-info" },
			{ count: summary.cancelled, progress_class: "progress-bar-warning" },
			{ count: summary.void, progress_class: "progress-bar-danger" },
		]
			.filter((s) => s.count > 0)
			// the title attribute is not escaped by the dashboard: a number only
			.map((s) => ({ title: cint(s.count), width: width(s.count), progress_class: s.progress_class }));
		const esc = frappe.utils.escape_html;
		const message = `${esc(
			__("{0} leaves · {1} used · {2} cancelled · {3} void · {4} remaining", [
				summary.total,
				summary.used,
				summary.cancelled,
				summary.void,
				summary.remaining,
			])
		)} &nbsp; <a class="cheque-book-open-register">${esc(__("Open Leaf Register"))} →</a>`;
		const $chart = frm.dashboard.add_progress(__("Leaves", null, "Cheque Book"), segments, message);
		$chart && $chart.find(".cheque-book-open-register").on("click", () => cheque_book_open_register(frm));
	},
});

function cheque_book_open_register(frm) {
	frappe.set_route("query-report", "Cheque Leaf Register", {
		company: frm.doc.company,
		cheque_book: frm.doc.name,
	});
}

function cheque_book_leaf_grid(frm) {
	// the kind as a pill (a status has a colour and a word); the reason type translated
	const esc = frappe.utils.escape_html;
	for (const name of [null, frm.doc.name]) {
		const kind = frappe.meta.get_docfield("Cheque Book Cancelled Leaf", "kind", name);
		if (kind) {
			kind.formatter = (value) =>
				value
					? `<span class="indicator-pill ${value === "Void" ? "darkgrey" : "gray"}">${esc(
							__(value, null, CHEQUE_BOOK_LEAF_CTX)
					  )}</span>`
					: "";
		}
		const reason_type = frappe.meta.get_docfield("Cheque Book Cancelled Leaf", "reason_type", name);
		if (reason_type) {
			reason_type.formatter = (value) => esc(value ? __(value, null, CHEQUE_BOOK_LEAF_CTX) : "");
		}
	}
	frm.refresh_field("cancelled_leaves");
}

function cheque_book_help(text) {
	return `<p class="text-muted small">${frappe.utils.escape_html(text)}</p>`;
}

function cheque_book_cancel_leaf(frm) {
	frappe.prompt(
		[
			{ fieldname: "help", fieldtype: "HTML", options: cheque_book_help(__("A leaf written by mistake and destroyed (spoilt).")) },
			{ fieldname: "cheque_no", fieldtype: "Data", label: __("Cheque No"), reqd: 1 },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
		],
		(values) => {
			frappe
				.call({
					method: CHEQUE_BOOK_METHOD + "cancel_leaf",
					args: { cheque_book: frm.doc.name, cheque_no: values.cheque_no, reason: values.reason },
					freeze: true,
				})
				.then(() => frm.reload_doc());
		},
		__("Cancel Leaf"),
		__("Cancel Leaf")
	);
}

function cheque_book_pad(number, frm) {
	return String(number).padStart(cint(frm.doc.leaf_digits) || 0, "0");
}

function cheque_book_void_leaves(frm) {
	// design 7f §1.4 / P3: From defaults to the next leaf (the one usually just spoilt in the printer)
	const next = cint(frm.doc.next_no);
	const default_from =
		next >= cint(frm.doc.start_no) && next <= cint(frm.doc.end_no) ? cheque_book_pad(next, frm) : "";
	const dialog = new frappe.ui.Dialog({
		// the action ("Void Leaves" alone is the Void Leaves column)
		title: __("Void Leaves", null, "Void Leaf"),
		size: "small",
		fields: [
			{
				fieldname: "help",
				fieldtype: "HTML",
				options: cheque_book_help(
					__(
						"Record leaves that are damaged, misprinted, lost or stolen, so they are never allocated. This cannot be undone."
					)
				),
			},
			{
				fieldname: "from_cheque_no",
				fieldtype: "Data",
				label: __("From Cheque No"),
				reqd: 1,
				default: default_from,
				onchange: () => cheque_book_range_hint(dialog),
			},
			{
				fieldname: "to_cheque_no",
				fieldtype: "Data",
				label: __("To Cheque No"),
				description: __("Leave empty to void one leaf."),
				onchange: () => cheque_book_range_hint(dialog),
			},
			{ fieldname: "range_preview", fieldtype: "HTML" },
			{
				fieldname: "reason_type",
				fieldtype: "Select",
				label: __("Reason Type", null, CHEQUE_BOOK_LEAF_CTX),
				// no default: the user chooses why
				options: [{ value: "", label: "" }].concat(
					CHEQUE_BOOK_VOID_REASON_TYPES.map((value) => ({ value, label: __(value, null, CHEQUE_BOOK_LEAF_CTX) }))
				),
				reqd: 1,
			},
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
		],
		primary_action_label: __("Void", null, "Void Leaf"),
		primary_action(values) {
			const range = cheque_book_range_text(values.from_cheque_no, values.to_cheque_no);
			frappe.confirm(frappe.utils.escape_html(__("Void leaves {0}? This cannot be undone.", [range])), () => {
				frappe
					.call({
						method: CHEQUE_BOOK_METHOD + "void_leaf",
						args: {
							cheque_book: frm.doc.name,
							from_cheque_no: values.from_cheque_no,
							to_cheque_no: values.to_cheque_no || "",
							reason_type: values.reason_type,
							reason: values.reason,
						},
						freeze: true,
					})
					.then((r) => {
						// errors: Frappe's message; the dialog stays open to correct the range
						const leaves = r.message || [];
						dialog.hide();
						frappe.show_alert({
							message: frappe.utils.escape_html(
								__("Leaves {0} voided.", [
									leaves.length > 3 ? `${leaves[0]} – ${leaves[leaves.length - 1]}` : leaves.join(", "),
								])
							),
							indicator: "green",
						});
						frm.reload_doc();
					});
			});
		},
	});
	dialog.get_primary_btn().removeClass("btn-primary").addClass("btn-danger");
	dialog.show();
	cheque_book_range_hint(dialog);
}

function cheque_book_range_text(from, to) {
	return to && to !== from ? `${from} – ${to}` : from || "";
}

function cheque_book_range_hint(dialog) {
	// a hint only: the server checks the range, the book and every leaf
	const from = (dialog.get_value("from_cheque_no") || "").trim();
	const to = (dialog.get_value("to_cheque_no") || "").trim() || from;
	const $hint = dialog.fields_dict.range_preview.$wrapper;
	if (!/^\d+$/.test(from) || !/^\d+$/.test(to)) {
		$hint.html("");
		return;
	}
	const count = parseInt(to, 10) - parseInt(from, 10) + 1;
	let text, css;
	if (count < 1) {
		text = __("To Cheque No cannot be before From Cheque No.");
		css = "text-danger";
	} else if (count > CHEQUE_BOOK_MAX_VOID) {
		text = __("At most {0} leaves can be voided at once.", [CHEQUE_BOOK_MAX_VOID]);
		css = "text-danger";
	} else {
		text = __("{0} leaf(s): {1} – {2}", [count, from, to]);
		css = "text-muted";
	}
	$hint.html(`<p class="small ${css}">${frappe.utils.escape_html(text)}</p>`);
}

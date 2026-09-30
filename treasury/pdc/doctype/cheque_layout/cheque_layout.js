// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// The preview is rendered by the server (treasury.pdc.cheque_printing.preview_layout) from the unsaved form.
const CHEQUE_LAYOUT_PREVIEW = "treasury.pdc.cheque_printing.preview_layout";
const CHEQUE_LAYOUT_LANGUAGES = { Arabic: "ar", English: "en" };

frappe.ui.form.on("Cheque Layout", {
	setup(frm) {
		frm.set_query("bank_account", () => ({ filters: { is_company_account: 1 } }));
	},

	refresh(frm) {
		frm.events.render_preview(frm);
		if (!frm.is_new()) {
			frm.add_custom_button(__("Test Print"), () => frm.events.test_print(frm));
		}
	},

	test_print(frm) {
		frappe.prompt(
			[
				{
					fieldname: "language",
					fieldtype: "Select",
					label: __("Language", null, "Cheque Layout"),
					options: Object.keys(CHEQUE_LAYOUT_LANGUAGES).map((value) => ({
						value,
						label: __(value, null, "Cheque Layout"),
					})),
					default: frm.doc.default_language || "Arabic",
					reqd: 1,
				},
			],
			(values) => {
				const params = new URLSearchParams({
					doctype: frm.doctype,
					name: frm.doc.name,
					format: "Cheque Layout Test Print",
					_lang: CHEQUE_LAYOUT_LANGUAGES[values.language] || "en",
				});
				window.open(`/printview?${params.toString()}`);
			},
			__("Test Print"),
			__("Print", null, "PDC Cheque")
		);
	},

	render_preview(frm) {
		const field = frm.get_field("preview_html");
		if (!field || !frm.doc.bank_account) {
			return;
		}
		if (!frm.__preview_language) {
			frm.__preview_language = frm.doc.default_language || "Arabic";
		}
		const other = frm.__preview_language === "Arabic" ? "English" : "Arabic";
		frappe
			.call({
				method: CHEQUE_LAYOUT_PREVIEW,
				args: { doc: frm.doc, language: frm.__preview_language },
			})
			.then((r) => {
				const toggle = `<button class="btn btn-xs btn-default cheque-layout-language">${frappe.utils.escape_html(
					__("Show in {0}", [__(other, null, "Cheque Layout")])
				)}</button>`;
				// the server escapes every text of the preview
				field.$wrapper.html(
					`<div style="margin-bottom: 8px">${toggle}</div><div style="overflow: auto; border: 1px solid var(--border-color)">${r.message || ""}</div>`
				);
				field.$wrapper.find(".cheque-layout-language").on("click", () => {
					frm.__preview_language = other;
					frm.events.render_preview(frm);
				});
			});
	},
});

// Any position, size, wording or paper change refreshes the preview (debounced).
const cheque_layout_refresh = frappe.utils.debounce((frm) => frm.events.render_preview(frm), 400);
const cheque_layout_handlers = {};
[
	"bank_account",
	"default_language",
	"date_format",
	"digits",
	"add_only_suffix",
	"show_currency_name",
	"fraction_style",
	"figures_guard",
	"currency_name_en",
	"currency_name_ar",
	"fraction_name_en",
	"fraction_name_ar",
	"leaf_width_mm",
	"leaf_height_mm",
	"paper",
	"offset_top_mm",
	"offset_left_mm",
	"nudge_x_mm",
	"nudge_y_mm",
	"font_family",
	"base_font_size_pt",
	"date_top_mm",
	"date_left_mm",
	"date_width_mm",
	"date_font_size_pt",
	"date_char_spacing_mm",
	"payee_top_mm",
	"payee_left_mm",
	"payee_width_mm",
	"payee_font_size_pt",
	"figures_top_mm",
	"figures_left_mm",
	"figures_width_mm",
	"figures_font_size_pt",
	"words1_top_mm",
	"words1_left_mm",
	"words1_width_mm",
	"words1_max_chars",
	"words2_top_mm",
	"words2_left_mm",
	"words2_width_mm",
	"words_font_size_pt",
	"print_crossing",
	"crossing_top_mm",
	"crossing_left_mm",
	"crossing_width_mm",
	"crossing_rotation_deg",
	"crossing_font_size_pt",
	"crossing_text_en",
	"crossing_text_ar",
	"signature_top_mm",
	"signature_left_mm",
	"signature_width_mm",
	"signature_height_mm",
	"signature_label",
	"signature_text",
	"test_amount",
].forEach((fieldname) => {
	cheque_layout_handlers[fieldname] = (frm) => cheque_layout_refresh(frm);
});
frappe.ui.form.on("Cheque Layout", cheque_layout_handlers);

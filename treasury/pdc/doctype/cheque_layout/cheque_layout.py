# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

# Leaf size limits (mm) and the A4 sheet a carrier layout is printed on.
MIN_WIDTH, MAX_WIDTH, MIN_HEIGHT, MAX_HEIGHT = 50, 300, 30, 150
A4_WIDTH, A4_HEIGHT = 210, 297
MIN_FONT, MAX_FONT = 5, 30
MIN_LINE_CHARS, MAX_LINE_CHARS = 10, 200
MAX_ROTATION = 90
A4_CARRIER = "A4 Carrier"
# Every printed box: (prefix of its top / left / width fields, section of its label, height field).
BOXES = (
	("date", "date_section", None),
	("payee", "payee_section", None),
	("figures", "figures_section", None),
	("words1", "words_section", None),
	("words2", "words_section", None),
	("crossing", "crossing_section", None),
	("signature", "signature_section", "signature_height_mm"),
)
FONT_FIELDS = (
	"date_font_size_pt",
	"payee_font_size_pt",
	"figures_font_size_pt",
	"words_font_size_pt",
	"crossing_font_size_pt",
)


class ChequeLayout(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		add_only_suffix: DF.Check
		bank_account: DF.Link
		base_font_size_pt: DF.Float
		company: DF.Link | None
		crossing_font_size_pt: DF.Float
		crossing_left_mm: DF.Float
		crossing_rotation_deg: DF.Float
		crossing_text_ar: DF.Data | None
		crossing_text_en: DF.Data | None
		crossing_top_mm: DF.Float
		crossing_width_mm: DF.Float
		currency_name_ar: DF.Data | None
		currency_name_en: DF.Data | None
		date_char_spacing_mm: DF.Float
		date_font_size_pt: DF.Float
		date_format: DF.Literal["dd/mm/yyyy", "dd-mm-yyyy", "dd.mm.yyyy", "ddmmyyyy", "yyyy/mm/dd", "mm/dd/yyyy"]
		date_left_mm: DF.Float
		date_top_mm: DF.Float
		date_width_mm: DF.Float
		default_language: DF.Literal["Arabic", "English"]
		digits: DF.Literal["Western", "Arabic-Indic"]
		disabled: DF.Check
		figures_font_size_pt: DF.Float
		figures_guard: DF.Data | None
		figures_left_mm: DF.Float
		figures_top_mm: DF.Float
		figures_width_mm: DF.Float
		font_family: DF.Data | None
		fraction_name_ar: DF.Data | None
		fraction_name_en: DF.Data | None
		fraction_style: DF.Literal["Words", "Figures"]
		is_default: DF.Check
		layout_name: DF.Data
		leaf_height_mm: DF.Float
		leaf_width_mm: DF.Float
		nudge_x_mm: DF.Float
		nudge_y_mm: DF.Float
		offset_left_mm: DF.Float
		offset_top_mm: DF.Float
		paper: DF.Literal["Cheque Leaf", "A4 Carrier"]
		payee_font_size_pt: DF.Float
		payee_left_mm: DF.Float
		payee_top_mm: DF.Float
		payee_width_mm: DF.Float
		preview_html: DF.HTML | None
		print_crossing: DF.Check
		sample_image: DF.AttachImage | None
		show_currency_name: DF.Check
		signature_height_mm: DF.Float
		signature_label: DF.Literal["None", "Company Name", "Custom Text"]
		signature_left_mm: DF.Float
		signature_text: DF.Data | None
		signature_top_mm: DF.Float
		signature_width_mm: DF.Float
		test_amount: DF.Currency
		words1_left_mm: DF.Float
		words1_max_chars: DF.Int
		words1_top_mm: DF.Float
		words1_width_mm: DF.Float
		words2_left_mm: DF.Float
		words2_top_mm: DF.Float
		words2_width_mm: DF.Float
		words_font_size_pt: DF.Float
	# end: auto-generated types

	def validate(self):
		self.validate_bank_account()
		self.validate_leaf_size()
		self.validate_boxes()
		self.validate_fonts()
		self.validate_wording()
		self.validate_carrier()
		if cint(self.disabled):
			self.is_default = 0

	def on_update(self):
		"""One default layout per bank account: the last one saved as default wins."""
		if cint(self.is_default) and not cint(self.disabled):
			for name in frappe.get_all(
				self.doctype,
				filters={"bank_account": self.bank_account, "is_default": 1, "name": ["!=", self.name]},
				pluck="name",
			):
				frappe.db.set_value(self.doctype, name, "is_default", 0)

	def validate_bank_account(self):
		values = frappe.db.get_value(
			"Bank Account", self.bank_account, ["company", "is_company_account", "account"], as_dict=True
		)
		if not values or not values.is_company_account or not values.company or not values.account:
			frappe.throw(
				_("Bank Account {0} must be a company bank account with a GL account.").format(
					frappe.bold(self.bank_account)
				)
			)
		self.company = values.company

	def validate_leaf_size(self):
		width, height = flt(self.leaf_width_mm), flt(self.leaf_height_mm)
		if not (MIN_WIDTH <= width <= MAX_WIDTH and MIN_HEIGHT <= height <= MAX_HEIGHT):
			frappe.throw(_("The cheque leaf must be 50–300 mm wide and 30–150 mm high."))

	def validate_boxes(self):
		width, height = flt(self.leaf_width_mm), flt(self.leaf_height_mm)
		for prefix, section, height_field in BOXES:
			top, left = flt(self.get(f"{prefix}_top_mm")), flt(self.get(f"{prefix}_left_mm"))
			box_width = flt(self.get(f"{prefix}_width_mm"))
			bottom = top + flt(self.get(height_field)) if height_field else top
			if top < 0 or left < 0 or box_width <= 0 or left + box_width > width + 0.01 or bottom > height:
				frappe.throw(
					_("{0} is outside the cheque leaf ({1} × {2} mm).").format(
						_(self.meta.get_label(section), context=self.doctype),
						frappe.format(width, {"fieldtype": "Float", "precision": 1}),
						frappe.format(height, {"fieldtype": "Float", "precision": 1}),
					)
				)

	def validate_fonts(self):
		base = flt(self.base_font_size_pt)
		sizes = [flt(self.get(f)) for f in FONT_FIELDS]
		if not MIN_FONT <= base <= MAX_FONT or any(s and not MIN_FONT <= s <= MAX_FONT for s in sizes):
			frappe.throw(_("Font sizes must be between 5 and 30 pt (0 = base size)."))

	def validate_wording(self):
		if not MIN_LINE_CHARS <= cint(self.words1_max_chars) <= MAX_LINE_CHARS:
			frappe.throw(
				_("Line 1 Max Characters must be between {0} and {1}.").format(MIN_LINE_CHARS, MAX_LINE_CHARS)
			)
		if abs(flt(self.crossing_rotation_deg)) > MAX_ROTATION:
			frappe.throw(_("The crossing rotation must be between -90 and 90 degrees."))
		if self.signature_label == "Custom Text" and not (self.signature_text or "").strip():
			frappe.throw(_("Enter the Signature Text."))

	def validate_carrier(self):
		if self.paper != A4_CARRIER:
			return
		if (
			flt(self.offset_left_mm) < 0
			or flt(self.offset_top_mm) < 0
			or flt(self.offset_left_mm) + flt(self.leaf_width_mm) > A4_WIDTH
			or flt(self.offset_top_mm) + flt(self.leaf_height_mm) > A4_HEIGHT
		):
			frappe.throw(_("The cheque leaf does not fit on an A4 sheet at this offset."))

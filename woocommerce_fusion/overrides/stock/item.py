import json

import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import set_name_by_naming_series
from erpnext.controllers.item_variant import (
	ItemVariantExistsError,
	copy_attributes_to_variant,
	get_variant,
	make_variant_item_code,
	validate_item_variant_attributes,
)
from frappe.utils import (
	cint,
	cstr,
	flt,
	formatdate,
	get_link_to_form,
	getdate,
	now_datetime,
	nowtime,
	strip,
	strip_html,
)

class CustomItem(Item):

	def autoname(self):
		if frappe.db.get_default("item_naming_by") == "Naming Series":
			if self.variant_of:
				if not self.item_code:
					template_item_name = frappe.db.get_value("Item", self.variant_of, "item_name")
					make_variant_item_code(self.variant_of, template_item_name, self)
					set_name_by_naming_series(self)
			else:

				set_name_by_naming_series(self)
				self.item_code = self.name
		else:
			self.item_code = strip(self.item_code)
			self.name = self.item_code
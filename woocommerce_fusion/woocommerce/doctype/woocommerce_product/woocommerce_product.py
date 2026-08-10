# Copyright (c) 2024, Dirk van der Laarse and contributors
# For license information, please see license.txt

import json
from dataclasses import dataclass
from typing import ClassVar

import frappe

from woocommerce_fusion.woocommerce.woocommerce_api import (
	WooCommerceAPI,
	WooCommerceResource,
	get_domain_and_id_from_woocommerce_record_name,
	log_and_raise_error,
	parse_domain_from_url,
)


@dataclass
class WooCommerceProductAPI(WooCommerceAPI):
	"""Class for keeping track of a WooCommerce site."""

	pass


class WooCommerceProduct(WooCommerceResource):
	"""
	Virtual doctype for WooCommerce Products.

	A WooCommerce variation cannot be retrieved from ``products/{variation_id}``.
	It must be retrieved from:

	    products/{parent_product_id}/variations/{variation_id}

	When an ERPNext Item is already linked to a WooCommerce variation, the
	parent product ID is resolved from:

	    Item WooCommerce Server -> Item.variant_of
	    -> parent Item WooCommerce Server

	This keeps the existing ERPNext/WooCommerce mapping as the source of truth and
	avoids scanning every variable product just to find one variation.
	"""

	doctype = "WooCommerce Product"
	resource: str = "products"
	child_resource: str = "variations"
	field_setter_map: ClassVar[dict[str, str]] = {
		"woocommerce_name": "name",
		"woocommerce_id": "id",
	}

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_list(args):
		products = WooCommerceProduct.get_list_of_records(args)

		# A variation ID is not returned by /products?include=<variation_id>.
		# If the normal product lookup returned nothing, use the ERPNext Item
		# mapping to resolve the variation's parent and retrieve it directly.
		if not products:
			variation = WooCommerceProduct._get_mapped_variation_from_args(args)
			if variation:
				return [variation]

		# Extend the list with product variants.
		products_with_variants = [
			(product.get("id"), product.get("woocommerce_name"))
			for product in products
			if product.get("type") == "variable"
		]
		for id, woocommerce_name in products_with_variants:
			args["endpoint"] = f"products/{id}/variations"
			args["metadata"] = {"parent_woocommerce_name": woocommerce_name}
			variants = WooCommerceProduct.get_list_of_records(args)
			products.extend(variants)

		return products

	@classmethod
	def _get_mapped_variation_from_args(cls, args):
		"""
		Return one mapped WooCommerce variation for an exact ID filter.

		This is primarily used by sync_items.get_list_of_wc_products(item=...).
		Normal products continue to use the existing /products endpoint.
		"""
		if not args:
			return None

		variation_id = cls._get_exact_id_filter(args.get("filters"))
		servers = args.get("servers") or []

		if not variation_id or len(servers) != 1:
			return None

		woocommerce_server = servers[0]
		variant_context = cls._get_linked_variant_context(
			woocommerce_server=woocommerce_server,
			variation_id=variation_id,
		)
		if not variant_context:
			return None

		wc_api = next(
			(
				api
				for api in cls._init_api()
				if api.woocommerce_server == woocommerce_server
			),
			None,
		)
		if not wc_api:
			return None

		endpoint = (
			f"products/{variant_context['parent_woocommerce_id']}"
			f"/variations/{variation_id}"
		)
		response = wc_api.api.get(endpoint)

		if response.status_code != 200:
			log_and_raise_error(
				error_text=(
					"Unable to load mapped WooCommerce variation "
					f"{variation_id} under parent "
					f"{variant_context['parent_woocommerce_id']} "
					f"on {woocommerce_server}"
				),
				response=response,
			)

		record = response.json()
		if not isinstance(record, dict) or "id" not in record:
			log_and_raise_error(
				error_text=(
					"Unexpected WooCommerce variation response for "
					f"{woocommerce_server} / {variation_id}: {record}"
				)
			)

		record = cls.pre_init_document(
			record=record,
			woocommerce_server_url=wc_api.woocommerce_server_url,
		)

		variation_args = {
			"metadata": {
				"parent_woocommerce_name": variant_context[
					"parent_woocommerce_name"
				]
			}
		}
		record = cls.during_get_list_of_records(record, variation_args)

		if args.get("as_doc"):
			return frappe.get_doc(record)

		return record

	@staticmethod
	def _get_exact_id_filter(filters):
		"""Extract an exact WooCommerce ID filter from Frappe list arguments."""
		if not filters:
			return None

		for row in filters:
			if (
				len(row) >= 4
				and row[1] == "id"
				and row[2] == "="
				and row[3] not in (None, "")
			):
				return str(row[3])

		return None

	@staticmethod
	def _get_linked_variant_context(
		woocommerce_server: str,
		variation_id,
	):
		"""
		Resolve a WooCommerce variation parent from ERPNext Item mappings.

		Resolution is always server-specific:

		    mapped variant WooCommerce ID
		        -> ERPNext variant Item
		        -> Item.variant_of
		        -> template Item
		        -> template Item WooCommerce Server row for SAME server
		        -> parent WooCommerce Product ID

		Example:
		    Variant Item 5241, variant_of Item 68
		    aden.copy.sareaapp.com:
		        variant WC ID = 5276
		        template WC ID = 5231

		    Correct endpoint:
		        products/5231/variations/5276

		The ERPNext template Item code (68) must never be assumed to be the
		WooCommerce parent Product ID.
		"""
		item_codes = frappe.get_all(
			"Item WooCommerce Server",
			filters={
				"parenttype": "Item",
				"woocommerce_server": woocommerce_server,
				"woocommerce_id": str(variation_id),
			},
			pluck="parent",
			page_length=2,
		)

		if not item_codes:
			return None

		if len(item_codes) > 1:
			frappe.throw(
				(
					"More than one ERPNext Item is linked to WooCommerce ID "
					f"{variation_id} on {woocommerce_server}: "
					f"{', '.join(item_codes)}"
				)
			)

		variant_item_code = item_codes[0]
		variant_item = frappe.get_cached_doc(
			"Item",
			variant_item_code,
		)

		if not variant_item.variant_of:
			return None

		parent_item = frappe.get_cached_doc(
			"Item",
			variant_item.variant_of,
		)

		parent_server_rows = [
			row
			for row in parent_item.woocommerce_servers
			if (
				row.woocommerce_server == woocommerce_server
				and row.woocommerce_id
			)
		]

		if not parent_server_rows:
			frappe.throw(
				(
					f"ERPNext variant Item {variant_item_code} is linked to "
					f"WooCommerce variation {variation_id} on "
					f"{woocommerce_server}, but its template Item "
					f"{parent_item.name} has no WooCommerce Product ID for "
					"the same WooCommerce Server."
				)
			)

		if len(parent_server_rows) > 1:
			frappe.throw(
				(
					f"Template Item {parent_item.name} has more than one "
					f"WooCommerce mapping for {woocommerce_server}. "
					"Only one parent Product ID per server is allowed."
				)
			)

		parent_server_row = parent_server_rows[0]
		parent_woocommerce_id = str(
			parent_server_row.woocommerce_id
		)

		return {
			"variant_item_code": variant_item_code,
			"variant_of": parent_item.name,
			"parent_woocommerce_id": parent_woocommerce_id,
			"parent_woocommerce_name": (
				parent_item.item_name
				or parent_item.name
			),
		}

	@classmethod
	def _get_wc_api_for_domain(cls, wc_server_domain: str):
		"""
		Return the API configured for exactly this WooCommerce Server/domain.

		Do not use substring matching. For example, ``copy.sareaapp.com`` is a
		substring of ``aden.copy.sareaapp.com`` and must not resolve to that API.
		"""
		for api in cls._init_api():
			if api.woocommerce_server == wc_server_domain:
				return api

		for api in cls._init_api():
			try:
				if parse_domain_from_url(api.woocommerce_server_url) == wc_server_domain:
					return api
			except Exception:
				continue

		return None

	def _restrict_api_list_to_current_server(self):
		"""
		Ensure the inherited WooCommerceResource.db_update() can only select the
		exact API for this virtual document.
		"""
		wc_server_domain, _record_id = (
			get_domain_and_id_from_woocommerce_record_name(self.name)
		)
		self.current_wc_api = self._get_wc_api_for_domain(
			wc_server_domain
		)
		if not self.current_wc_api:
			log_and_raise_error(
				error_text=(
					"No enabled WooCommerce API found for exact server "
					f"{wc_server_domain}"
				)
			)

		self.wc_api_list = [self.current_wc_api]

	def load_from_db(self):
		"""
		Load either a normal product or an ERPNext-mapped WooCommerce variation.

		The generic WooCommerceResource loader always uses products/{id}, which is
		invalid for a variation. Here we resolve a mapped variant to its parent
		product and use the WooCommerce variation endpoint instead.
		"""
		if not self.wc_api_list:
			self.init_api()

		wc_server_domain, record_id = (
			get_domain_and_id_from_woocommerce_record_name(self.name)
		)

		self.current_wc_api = self._get_wc_api_for_domain(
			wc_server_domain
		)
		if not self.current_wc_api:
			log_and_raise_error(
				error_text=(
					"No enabled WooCommerce API found for "
					f"{wc_server_domain}"
				)
			)

		variant_context = self._get_linked_variant_context(
			woocommerce_server=self.current_wc_api.woocommerce_server,
			variation_id=record_id,
		)

		if variant_context:
			endpoint = (
				f"{self.resource}/"
				f"{variant_context['parent_woocommerce_id']}/"
				f"{self.child_resource}/{record_id}"
			)
		else:
			endpoint = f"{self.resource}/{record_id}"

		try:
			response = self.current_wc_api.api.get(endpoint)
		except Exception as err:
			log_and_raise_error(
				err,
				error_text=(
					"load_from_db failed for WooCommerce endpoint "
					f"{endpoint}"
				),
			)

		if response.status_code != 200:
			log_and_raise_error(
				error_text=(
					"load_from_db failed for WooCommerce endpoint "
					f"{endpoint}"
				),
				response=response,
			)

		record = response.json()
		if not isinstance(record, dict) or "id" not in record:
			log_and_raise_error(
				error_text=(
					"load_from_db returned an unexpected WooCommerce "
					f"response for endpoint {endpoint}: {record}"
				)
			)

		record = self.pre_init_document(
			record,
			woocommerce_server_url=(
				self.current_wc_api.woocommerce_server_url
			),
		)
		record = self._normalize_virtual_product_fields(
			record
		)

		if variant_context:
			record = self.during_get_list_of_records(
				record,
				{
					"metadata": {
						"parent_woocommerce_name": variant_context[
							"parent_woocommerce_name"
						]
					}
				},
			)

		record = self.after_load_from_db(record)
		self.call_super_init(record)

	def db_update(self, *args, **kwargs):
		"""
		Update through the exact WooCommerce Server API.

		WooCommerceResource.db_update() historically resolves the API using a
		substring domain match. Restricting wc_api_list to one exact server keeps
		the inherited update logic intact while preventing cross-store updates.
		"""
		self._restrict_api_list_to_current_server()
		return super().db_update(*args, **kwargs)

	def after_load_from_db(self, product: dict):
		product.pop("name")
		product = self.set_title(product)
		return product

	@classmethod
	def _normalize_virtual_product_fields(cls, product: dict):
		"""
		Normalize WooCommerce API data only for Frappe virtual-document validation.

		Variable parent products:
		    WooCommerce commonly returns regular_price as an empty string because
		    the actual sell prices live on the variations. The WooCommerce Product
		    virtual DocType marks regular_price mandatory, so use 0 only inside the
		    Frappe virtual document when the parent has no regular price.

		Variations:
		    WooCommerce variation responses do not contain catalog_visibility, but
		    the virtual DocType marks it mandatory. Use "visible" only inside the
		    Frappe document.

		These values do not create new ERPNext Item prices. They only prevent
		Frappe mandatory validation from rejecting valid WooCommerce resources.
		"""
		if (
			product.get("type") == "variable"
			and product.get("regular_price") in (None, "")
		):
			product["regular_price"] = 0.0

		if product.get("parent_id"):
			product["type"] = "variation"
			if not product.get("catalog_visibility"):
				product["catalog_visibility"] = "visible"

		return product

	@classmethod
	def during_get_list_of_records(cls, product: dict, args):
		product = cls._normalize_virtual_product_fields(product)

		# In the case of variations
		if product.get("parent_id"):
			if variation_name := cls.get_variation_name(product, args):
				# Set the name in args, for use by set_title()
				args["metadata"]["woocommerce_name"] = variation_name

				# Override the woocommerce_name field
				product = cls.override_woocommerce_name(product, variation_name)

		product = cls.set_title(product, args)
		return product

	@staticmethod
	def set_title(product: dict, args=None):
		if args and (metadata := args.get("metadata")) and (set_name := metadata.get("woocommerce_name")):
			product["title"] = set_name
		elif wc_name := product.get("woocommerce_name"):
			if sku := product.get("sku"):
				product["title"] = f"{sku} - {wc_name}"
			else:
				product["title"] = wc_name
		else:
			product["title"] = product["woocommerce_id"]

		return product

	@staticmethod
	def override_woocommerce_name(product: dict, name: str):
		product["woocommerce_name"] = name
		return product

	@staticmethod
	def get_variation_name(product: dict, args):
		# If this is a variation, we expect the variation's parent name in the metadata, then we can
		# build an item name in the format of {parent_name}, {attribute 1}, {attribute n}
		if (
			(product["type"] == "variation")
			and (metadata := args.get("metadata"))
			and (attributes := product.get("attributes"))
			and (parent_wc_name := metadata.get("parent_woocommerce_name"))
		):
			attr_values = [attr["option"] for attr in json.loads(attributes)]
			return parent_wc_name + " - " + ", ".join(attr_values)
		return None

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_count(args) -> int:
		return WooCommerceProduct.get_count_of_records(args)

	def before_db_insert(self, product: dict):
		return self.clean_up_product_before_write(product)

	def before_db_update(self, product: dict):
		return self.clean_up_product_before_write(product)

	def after_db_update(self):
		pass

	@staticmethod
	def clean_up_product_before_write(product: dict) -> dict:
		"""
		Perform some tasks to make sure that a product is in the correct format for the WC API
		"""

		if product.get("type") == "variation" or product.get("parent_id"):
			product["status"] = "publish"

			# catalog_visibility is a product-level property and is not part of
			# the WooCommerce Product Variation REST resource. The value is only
			# synthesized on the virtual Frappe document to satisfy its mandatory
			# field validation.
			product.pop("catalog_visibility", None)

		# Convert numeric fields back to strings required by WC API
		product["weight"] = str(product["weight"])
		product["regular_price"] = str(product["regular_price"])
		# Sale price: send empty string to clear an existing sale, or string value to set one.
		# Popping the field entirely has no effect on an existing WC sale price.
		if product.get("sale_price") and float(product["sale_price"]) > 0:
			product["sale_price"] = str(product["sale_price"])
		else:
			product["sale_price"] = ""
		# Sale date fields: normalise to ISO 8601 string, or None to clear via API
		for date_field in ("date_on_sale_from", "date_on_sale_to"):
			value = product.get(date_field)
			if value:
				product[date_field] = (
					value.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(value, "strftime") else str(value)
				)
			else:
				product[date_field] = None

		# Set corrected properties
		product["name"] = str(product["woocommerce_name"])

		# Drop 'related_ids' field
		product.pop("related_ids")

		return product

import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote

import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _, _dict
from frappe.query_builder import Criterion
from frappe.utils import get_datetime, now
from jsonpath_ng.ext import parse

from woocommerce_fusion.exceptions import SyncDisabledError
from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.tasks.sync_item_prices import _format_sale_date
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
	log_and_raise_error,
)


def run_item_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Item
	"""
	if (
		doc.doctype == "Item"
		and not doc.flags.get("created_by_sync", None)
		and len(doc.woocommerce_servers) > 0
		and any(
			frappe.get_cached_doc("WooCommerce Server", row.woocommerce_server).enable_sync
			for row in doc.woocommerce_servers
			if row.woocommerce_server
		)
	):
		frappe.msgprint(
			_("Background sync to WooCommerce triggered for {0} {1}").format(frappe.bold(doc.name), method),
			indicator="blue",
			alert=True,
		)
		frappe.enqueue(clear_sync_hash_and_run_item_sync, item_code=doc.name)


@frappe.whitelist()
def run_item_sync(
	item_code: str | None = None,
	item: Item | None = None,
	woocommerce_product_name: str | None = None,
	woocommerce_product: WooCommerceProduct | None = None,
	enqueue: bool = False,
) -> tuple[Item, WooCommerceProduct]:
	"""
	Helper funtion that prepares arguments for item sync
	"""
	# Validate inputs, at least one of the parameters should be provided
	if not any([item_code, item, woocommerce_product_name, woocommerce_product]):
		raise ValueError(
			"At least one of item_code, item, woocommerce_product_name, woocommerce_product parameters required"
		)

	# Get ERPNext Item and WooCommerce product if they exist
	if woocommerce_product or woocommerce_product_name:
		if not woocommerce_product:
			woocommerce_product = frappe.get_doc(
				{"doctype": "WooCommerce Product", "name": woocommerce_product_name}
			)
			woocommerce_product.load_from_db()

		# Trigger sync
		sync = SynchroniseItem(woocommerce_product=woocommerce_product)
		if enqueue:
			frappe.enqueue(sync.run)
		else:
			sync.run()

	elif item or item_code:
		if not item:
			item = frappe.get_doc("Item", item_code)
		if not item.woocommerce_servers:
			frappe.throw(_("No WooCommerce Servers defined for Item {0}").format(item_code))
		for wc_server in item.woocommerce_servers:
			# Trigger sync for every linked server
			sync = SynchroniseItem(
				item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=wc_server.idx)
			)
			if enqueue:
				frappe.enqueue(sync.run)
			else:
				sync.run()

	return (
		sync.item.item if sync and sync.item else None,
		sync.woocommerce_product if sync else None,
	)


def sync_woocommerce_products_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce products modified since date_time_from
	"""
	wc_settings = frappe.get_doc("WooCommerce Integration Settings")

	if not date_time_from:
		date_time_from = wc_settings.wc_last_sync_date_items

	# Validate
	if not date_time_from:
		error_text = _(
			"'Last Items Syncronisation Date' field on 'WooCommerce Integration Settings' is missing"
		)
		frappe.log_error(
			"WooCommerce Items Sync Task Error",
			error_text,
		)
		raise ValueError(error_text)

	wc_products = get_list_of_wc_products(date_time_from=date_time_from)
	for wc_product in wc_products:
		try:
			run_item_sync(woocommerce_product=wc_product, enqueue=True)
		# Skip items with errors, as these exceptions will be logged
		except Exception:
			pass

	frappe.db.set_single_value("WooCommerce Integration Settings", "wc_last_sync_date_items", now())


@dataclass
class ERPNextItemToSync:
	"""Class for keeping track of an ERPNext Item and the relevant WooCommerce Server to sync to"""

	item: Item
	item_woocommerce_server_idx: int

	@property
	def item_woocommerce_server(self):
		return self.item.woocommerce_servers[self.item_woocommerce_server_idx - 1]

def _serialized_item_sync(method):
	"""
	Serialize WooCommerce synchronization per ERPNext Item.

	Multiple WooCommerce Server jobs may synchronize the same Item at the
	same time. Each queued job may contain an older Item document and can
	therefore raise TimestampMismatchError when saving.

	This wrapper:
	- uses one lock per ERPNext Item;
	- reloads the latest Item inside the lock;
	- finds the WooCommerce Server row again;
	- then executes the existing SynchroniseItem.run().
	"""
	from functools import wraps

	@wraps(method)
	def wrapper(self, *args, **kwargs):
		# A WooCommerce-originated sync may start without self.item.
		# Resolve the linked ERPNext Item first.
		if not self.item and self.woocommerce_product:
			self.get_corresponding_item_or_product()

		if self.item:
			item_code = self.item.item.name

			woocommerce_server = (
				self.item.item_woocommerce_server.woocommerce_server
			)

			lock_name = (
				f"woocommerce_fusion:item_sync:"
				f"{frappe.local.site}:{item_code}"
			)

			with frappe.cache.lock(
				lock_name,
				timeout=900,
			):
				# Important:
				# discard the potentially stale Item object carried
				# by the background job.
				fresh_item = frappe.get_doc(
					"Item",
					item_code,
				)

				fresh_server_row = next(
					(
						row
						for row in fresh_item.woocommerce_servers
						if row.woocommerce_server
						== woocommerce_server
					),
					None,
				)

				if not fresh_server_row:
					frappe.throw(
						_(
							"WooCommerce Server {0} is no longer "
							"linked to Item {1}."
						).format(
							woocommerce_server,
							item_code,
						)
					)

				self.item = ERPNextItemToSync(
					item=fresh_item,
					item_woocommerce_server_idx=(
						fresh_server_row.idx
					),
				)

				return method(
					self,
					*args,
					**kwargs,
				)

		# If this is a WooCommerce product that does not yet have
		# an ERPNext Item, protect creation using the remote identity.
		if self.woocommerce_product:
			lock_name = (
				f"woocommerce_fusion:item_sync:new:"
				f"{frappe.local.site}:"
				f"{self.woocommerce_product.woocommerce_server}:"
				f"{self.woocommerce_product.woocommerce_id}"
			)

			with frappe.cache.lock(
				lock_name,
				timeout=900,
			):
				return method(
					self,
					*args,
					**kwargs,
				)

		return method(
			self,
			*args,
			**kwargs,
		)

	return wrapper

class SynchroniseItem(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of WooCommerce Product with ERPNext Item
	"""

	def __init__(
		self,
		servers: list[WooCommerceServer | _dict] | None = None,
		item: ERPNextItemToSync | None = None,
		woocommerce_product: WooCommerceProduct | None = None,
	) -> None:
		super().__init__(servers)
		self.item = item
		self.woocommerce_product = woocommerce_product
		self.settings = frappe.get_cached_doc("WooCommerce Integration Settings")

		# Per-sync caches for WooCommerce global attribute metadata/terms.
		# Attribute IDs are server-specific and must never be reused between stores.
		self._wc_api_cache = {}
		self._wc_global_attributes_cache = {}
		self._wc_global_terms_cache = {}
		self._wc_global_term_records_cache = {}

	@_serialized_item_sync
	def run(self):
		"""
		Run synchronisation
		"""
		try:
			self.get_corresponding_item_or_product()
			self.sync_wc_product_with_erpnext_item()
		except Exception as err:
			try:
				woocommerce_product_dict = (
					self.woocommerce_product.as_dict()
					if isinstance(self.woocommerce_product, WooCommerceProduct)
					else self.woocommerce_product
				)
			except ValidationError:
				woocommerce_product_dict = self.woocommerce_product
			error_message = f"{frappe.get_traceback()}\n\nItem Data: \n{str(self.item) if self.item else ''}\n\nWC Product Data \n{str(woocommerce_product_dict) if self.woocommerce_product else ''})"
			frappe.log_error("WooCommerce Error", error_message)
			raise err

	def get_corresponding_item_or_product(self):
		"""
		If we have an ERPNext Item, get the corresponding WooCommerce Product
		If we have a WooCommerce Product, get the corresponding ERPNext Item
		"""
		if self.item and not self.woocommerce_product and self.item.item_woocommerce_server.woocommerce_id:
			# Validate that this Item's WooCommerce Server has sync enabled
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.item.item_woocommerce_server.woocommerce_server
			)
			if not wc_server.enable_sync:
				raise SyncDisabledError(wc_server)

			wc_products = get_list_of_wc_products(item=self.item)
			if len(wc_products) == 0:
				raise ValueError(
					f"No WooCommerce Product found with ID {self.item.item_woocommerce_server.woocommerce_id} on {self.item.item_woocommerce_server.woocommerce_server}"
				)
			self.woocommerce_product = wc_products[0]

		if self.woocommerce_product and not self.item:
			self.get_erpnext_item()

	def get_erpnext_item(self):
		"""
		Get erpnext item for a WooCommerce Product
		"""
		if not all([self.woocommerce_product.woocommerce_server, self.woocommerce_product.woocommerce_id]):
			raise ValueError("Both woocommerce_server and woocommerce_id required")

		iws = frappe.qb.DocType("Item WooCommerce Server")
		itm = frappe.qb.DocType("Item")

		and_conditions = [
			iws.woocommerce_server == self.woocommerce_product.woocommerce_server,
			iws.woocommerce_id == self.woocommerce_product.woocommerce_id,
		]

		item_codes = (
			frappe.qb.from_(iws)
			.join(itm)
			.on(iws.parent == itm.name)
			.where(Criterion.all(and_conditions))
			.select(iws.parent, iws.name)
			.limit(1)
		).run(as_dict=True)

		found_item = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None
		if found_item:
			self.item = ERPNextItemToSync(
				item=found_item,
				item_woocommerce_server_idx=next(
					server.idx
					for server in found_item.woocommerce_servers
					if server.name == item_codes[0].name
				),
			)

	def sync_wc_product_with_erpnext_item(self):
		"""
		Syncronise Item between ERPNext and WooCommerce
		"""
		if self.item and not self.woocommerce_product:
			# create missing product in WooCommerce
			self.create_woocommerce_product(self.item)
		elif self.woocommerce_product and not self.item:
			# create missing item in ERPNext
			self.create_item(self.woocommerce_product)
		elif self.item and self.woocommerce_product:
			# both exist, check sync hash
			if (
				self.woocommerce_product.woocommerce_date_modified
				!= self.item.item_woocommerce_server.woocommerce_last_sync_hash
			):
				if get_datetime(self.woocommerce_product.woocommerce_date_modified) > get_datetime(
					self.item.item.modified
				):
					self.update_item(self.woocommerce_product, self.item)
				if get_datetime(self.woocommerce_product.woocommerce_date_modified) < get_datetime(
					self.item.item.modified
				):
					self.update_woocommerce_product(self.woocommerce_product, self.item)

	def update_item(self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync):
		"""
		Update the ERPNext Item with fields from it's corresponding WooCommerce Product
		"""
		item_dirty = False
		if item.item.item_name != woocommerce_product.woocommerce_name:
			item.item.item_name = woocommerce_product.woocommerce_name
			item_dirty = True

		fields_updated, item.item = self.set_item_fields(item=item.item)

		wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
		if wc_server.enable_image_sync:
			wc_product_images = json.loads(woocommerce_product.images)
			if len(wc_product_images) > 0:
				if item.item.image != wc_product_images[0]["src"]:
					item.item.image = wc_product_images[0]["src"]
					item_dirty = True

		if item_dirty or fields_updated:
			item.item.flags.created_by_sync = True
			item.item.save()

		self.set_sync_hash()

	def update_woocommerce_product(
		self,
		wc_product: WooCommerceProduct,
		item: ERPNextItemToSync,
	) -> None:
		"""
		Update WooCommerce Product from ERPNext Item.
	
		If WooCommerce changes the same product between load and save
		(for example through stock or price synchronization), reload the
		latest WooCommerce resource and safely retry the update.
		"""
	
		product_name = wc_product.name
		max_attempts = 3
	
		for attempt in range(max_attempts):
		
			# After a TimestampMismatchError, discard the stale virtual
			# document completely and load the newest WooCommerce resource.
			if attempt > 0:
				wc_product = frappe.get_doc(
					"WooCommerce Product",
					product_name,
				)
	
			wc_product_dirty = False
	
			# Apply Item name.
			if wc_product.woocommerce_name != item.item.item_name:
				wc_product.woocommerce_name = item.item.item_name
				wc_product_dirty = True
	
			# Re-apply all ERPNext -> WooCommerce mapped fields
			# against the freshly loaded WooCommerce resource.
			product_fields_changed, wc_product = (
				self.set_product_fields(
					wc_product,
					item,
				)
			)
	
			if product_fields_changed:
				wc_product_dirty = True
	
			if not wc_product_dirty:
				self.woocommerce_product = wc_product
				self.set_sync_hash()
				return
	
			try:
				wc_product.save()
	
			except frappe.TimestampMismatchError:
				# save() adds the document to currently_saving before
				# check_if_latest(). Since the save failed, clean that
				# request-level state before retrying.
				saving_key = (
					wc_product.doctype,
					wc_product.name,
				)
	
				if saving_key in frappe.flags.currently_saving:
					frappe.flags.currently_saving.remove(
						saving_key
					)
	
				frappe.clear_last_message()
	
				if attempt == max_attempts - 1:
					raise
	
				# Retry using a completely fresh WooCommerce Product.
				continue
	
			# Successful save.
			self.woocommerce_product = wc_product
			self.set_sync_hash()
			return



	def _normalise_attribute_key(self, value) -> str:
		"""
		Normalise ERPNext/WooCommerce attribute names for matching only.

		This does not modify the value sent to WooCommerce. It lets names such as
		"pa_color", "Color", "color", or URL-decoded Unicode slugs be compared
		safely while preserving the original display name.
		"""
		value = unquote(str(value or "")).strip().casefold()

		if value.startswith("pa_"):
			value = value[3:]

		value = value.replace("_", " ").replace("-", " ")
		return " ".join(value.split())

	def _get_wc_api_for_server(self, woocommerce_server: str):
		if woocommerce_server in self._wc_api_cache:
			return self._wc_api_cache[woocommerce_server]

		wc_api = next(
			(
				api
				for api in WooCommerceProduct._init_api()
				if api.woocommerce_server == woocommerce_server
			),
			None,
		)

		if not wc_api:
			frappe.throw(
				_(
					"No enabled WooCommerce API is available for "
					"server {0}."
				).format(woocommerce_server)
			)

		self._wc_api_cache[woocommerce_server] = wc_api
		return wc_api

	def _get_all_wc_records(
		self,
		woocommerce_server: str,
		endpoint: str,
	) -> list[dict]:
		"""Fetch all REST records from a paginated WooCommerce endpoint."""
		wc_api = self._get_wc_api_for_server(woocommerce_server)
		records = []
		page = 1
		per_page = 100

		while True:
			response = wc_api.api.get(
				endpoint,
				params={
					"per_page": per_page,
					"page": page,
				},
			)

			if response.status_code != 200:
				log_and_raise_error(
					error_text=(
						f"Unable to read WooCommerce endpoint "
						f"{endpoint} on {woocommerce_server}"
					),
					response=response,
				)

			page_records = response.json()
			if not isinstance(page_records, list):
				frappe.throw(
					_(
						"Unexpected WooCommerce response from {0} "
						"on server {1}."
					).format(
						endpoint,
						woocommerce_server,
					)
				)

			records.extend(page_records)

			total_pages = int(
				response.headers.get("X-WP-TotalPages") or 0
			)

			if (
				(total_pages and page >= total_pages)
				or len(page_records) < per_page
			):
				break

			page += 1

		return records

	def get_global_woocommerce_attributes(
		self,
		woocommerce_server: str,
	) -> list[dict]:
		"""Return global WooCommerce attributes for exactly one server."""
		if woocommerce_server not in self._wc_global_attributes_cache:
			self._wc_global_attributes_cache[woocommerce_server] = (
				self._get_all_wc_records(
					woocommerce_server,
					"products/attributes",
				)
			)

		return self._wc_global_attributes_cache[
			woocommerce_server
		]

	def find_global_woocommerce_attribute(
		self,
		woocommerce_server: str,
		erpnext_attribute_name: str,
	) -> dict | None:
		"""
		Match an ERPNext Item Attribute to a WooCommerce global attribute.

		Name is preferred. Slug is a fallback. Attribute IDs are never shared
		between WooCommerce Servers.
		"""
		target = self._normalise_attribute_key(
			erpnext_attribute_name
		)

		name_matches = []
		slug_matches = []

		for wc_attribute in self.get_global_woocommerce_attributes(
			woocommerce_server
		):
			if (
				self._normalise_attribute_key(
					wc_attribute.get("name")
				)
				== target
			):
				name_matches.append(wc_attribute)
			elif (
				self._normalise_attribute_key(
					wc_attribute.get("slug")
				)
				== target
			):
				slug_matches.append(wc_attribute)

		matches = name_matches or slug_matches

		if len(matches) > 1:
			frappe.throw(
				_(
					"More than one WooCommerce global attribute "
					"matches ERPNext Item Attribute {0} on server {1}."
				).format(
					erpnext_attribute_name,
					woocommerce_server,
				)
			)

		return matches[0] if matches else None

	def ensure_global_woocommerce_attribute_terms(
		self,
		woocommerce_server: str,
		attribute_id: int,
		values: list[str],
	) -> None:
		"""Create missing term names for a matched global WooCommerce attribute."""
		cache_key = (
			woocommerce_server,
			int(attribute_id),
		)

		if cache_key not in self._wc_global_terms_cache:
			terms = self._get_all_wc_records(
				woocommerce_server,
				f"products/attributes/{attribute_id}/terms",
			)
			self._wc_global_terms_cache[cache_key] = {
				self._normalise_attribute_key(
					term.get("name")
				): term
				for term in terms
				if term.get("name")
			}

		terms_by_key = self._wc_global_terms_cache[
			cache_key
		]
		wc_api = self._get_wc_api_for_server(
			woocommerce_server
		)

		for value in values:
			value = str(value or "").strip()
			if not value:
				continue

			key = self._normalise_attribute_key(value)
			if key in terms_by_key:
				continue

			response = wc_api.api.post(
				f"products/attributes/{attribute_id}/terms",
				data={"name": value},
			)

			if response.status_code not in (200, 201):
				response_data = response.json()
				error_code = (
					response_data.get("code")
					if isinstance(response_data, dict)
					else ""
				)

				# Another concurrent variant job may have created the same term
				# after our initial GET. Treat a term-exists race as success.
				if "term_exists" not in str(error_code):
					log_and_raise_error(
						error_text=(
							"Unable to create WooCommerce attribute "
							f"term {value} for attribute {attribute_id} "
							f"on {woocommerce_server}"
						),
						response=response,
					)

			terms_by_key[key] = {
				"name": value,
			}

	def build_woocommerce_attributes_from_erpnext(
		self,
		item: ERPNextItemToSync,
		variation: bool = False,
	) -> list[dict]:
		"""
		Build WooCommerce attributes using the correct global/local model.

		Global WooCommerce attribute:
		    parent/template -> {"id": ..., "options": [...]}
		    variation       -> {"id": ..., "option": ...}

		Local/product-specific attribute:
		    parent/template -> {"name": ..., "options": [...]}
		    variation       -> {"name": ..., "option": ...}
		"""
		woocommerce_server = (
			item.item_woocommerce_server.woocommerce_server
		)
		result = []

		for row in item.item.attributes:
			item_attribute = frappe.get_doc(
				"Item Attribute",
				row.attribute,
			)
			attribute_name = (
				item_attribute.attribute_name
				or item_attribute.name
			)

			global_attribute = (
				self.find_global_woocommerce_attribute(
					woocommerce_server,
					attribute_name,
				)
			)

			if variation:
				option = str(
					row.attribute_value or ""
				).strip()

				if global_attribute:
					self.ensure_global_woocommerce_attribute_terms(
						woocommerce_server,
						global_attribute["id"],
						[option],
					)
					result.append(
						{
							"id": int(
								global_attribute["id"]
							),
							"option": option,
						}
					)
				else:
					result.append(
						{
							"name": attribute_name,
							"option": option,
						}
					)

				continue

			options = [
				str(option.attribute_value or "").strip()
				for option in item_attribute.item_attribute_values
				if str(option.attribute_value or "").strip()
			]

			if global_attribute:
				self.ensure_global_woocommerce_attribute_terms(
					woocommerce_server,
					global_attribute["id"],
					options,
				)
				result.append(
					{
						"id": int(global_attribute["id"]),
						"visible": True,
						"variation": True,
						"options": options,
					}
				)
			else:
				result.append(
					{
						"name": attribute_name,
						"visible": True,
						"variation": True,
						"options": options,
					}
				)

		return result

	def _find_existing_erpnext_item_attribute(
		self,
		attribute_name: str,
		attribute_slug: str | None = None,
	) -> str | None:
		"""Find an existing ERPNext Item Attribute using normalised name/slug."""
		if frappe.db.exists(
			"Item Attribute",
			attribute_name,
		):
			return attribute_name

		target_keys = {
			self._normalise_attribute_key(
				attribute_name
			)
		}

		if attribute_slug:
			target_keys.add(
				self._normalise_attribute_key(
					attribute_slug
				)
			)

		matches = []

		for row in frappe.get_all(
			"Item Attribute",
			fields=[
				"name",
				"attribute_name",
			],
		):
			candidate_keys = {
				self._normalise_attribute_key(
					row.name
				),
				self._normalise_attribute_key(
					row.attribute_name
				),
			}

			if target_keys & candidate_keys:
				matches.append(row.name)

		if len(matches) > 1:
			frappe.throw(
				_(
					"More than one ERPNext Item Attribute matches "
					"WooCommerce attribute {0}: {1}"
				).format(
					attribute_name,
					", ".join(matches),
				)
			)

		return matches[0] if matches else None

	def _get_wc_attribute_display_name(
		self,
		wc_product: WooCommerceProduct,
		wc_attribute: dict,
	) -> str:
		"""
		Return the canonical WooCommerce attribute name.

		For global attributes, prefer the global attribute master name resolved
		from the current server's attribute ID.
		"""
		attribute_id = int(
			wc_attribute.get("id") or 0
		)

		if attribute_id:
			for global_attribute in (
				self.get_global_woocommerce_attributes(
					wc_product.woocommerce_server
				)
			):
				if int(global_attribute.get("id") or 0) == attribute_id:
					if global_attribute.get("name"):
						return str(
							global_attribute["name"]
						).strip()

		name = str(
			wc_attribute.get("name") or ""
		).strip()

		if not name:
			frappe.throw(
				_(
					"WooCommerce attribute has neither a resolvable "
					"global ID nor a name on product {0}."
				).format(wc_product.name)
			)

		return name

	def _get_parent_woocommerce_attributes(
		self,
		parent_wc_product: WooCommerceProduct,
	) -> list[dict]:
		"""Return the parent WooCommerce product attributes as a Python list."""
		raw_attributes = parent_wc_product.attributes

		if not raw_attributes:
			return []

		if isinstance(raw_attributes, str):
			attributes = json.loads(raw_attributes)
		else:
			attributes = raw_attributes

		if not isinstance(attributes, list):
			frappe.throw(
				_(
					"Unexpected attributes data on WooCommerce parent "
					"Product {0} ({1})."
				).format(
					parent_wc_product.woocommerce_id,
					parent_wc_product.woocommerce_server,
				)
			)

		return attributes

	def _find_parent_woocommerce_attribute(
		self,
		parent_attributes: list[dict],
		erpnext_attribute_name: str,
	) -> dict | None:
		"""
		Match an ERPNext Item Attribute to the attribute that is ACTUALLY
		configured on the WooCommerce parent product.

		This is intentionally different from looking up the store's global
		attribute master. An existing parent may legitimately/ historically
		use a local product attribute even when a same-named global attribute
		also exists on that WooCommerce Server.
		"""
		target = self._normalise_attribute_key(
			erpnext_attribute_name
		)

		name_matches = []
		slug_matches = []

		for parent_attribute in parent_attributes:
			if (
				self._normalise_attribute_key(
					parent_attribute.get("name")
				)
				== target
			):
				name_matches.append(parent_attribute)
				continue

			if (
				parent_attribute.get("slug")
				and self._normalise_attribute_key(
					parent_attribute.get("slug")
				)
				== target
			):
				slug_matches.append(parent_attribute)

		matches = name_matches or slug_matches

		if len(matches) > 1:
			frappe.throw(
				_(
					"More than one parent WooCommerce attribute "
					"matches ERPNext Item Attribute {0}."
				).format(
					erpnext_attribute_name
				)
			)

		return matches[0] if matches else None

	def _build_parent_attributes_update_payload(
		self,
		parent_attributes: list[dict],
	) -> list[dict]:
		"""
		Build only fields accepted by WooCommerce when updating a variable
		product's attributes. Do not send response-only fields such as slug.
		"""
		payload = []

		for attribute in parent_attributes:
			attribute_id = int(
				attribute.get("id") or 0
			)

			row = {
				"position": int(
					attribute.get("position") or 0
				),
				"visible": bool(
					attribute.get("visible", True)
				),
				"variation": bool(
					attribute.get("variation", False)
				),
				"options": list(
					attribute.get("options") or []
				),
			}

			if attribute_id:
				row["id"] = attribute_id
			else:
				row["name"] = attribute.get("name")

			payload.append(row)

		return payload

	def build_woocommerce_variation_attributes_from_parent(
		self,
		item: ERPNextItemToSync,
		parent_wc_product: WooCommerceProduct,
	) -> list[dict]:
		"""
		Build variation attributes from the ACTUAL WooCommerce parent product.

		Why:
		A variation attribute must match the exact attribute attached to its
		parent product. Looking up a same-named global attribute independently
		can be wrong when the parent uses a local/product-specific attribute.

		Behavior per WooCommerce Server:
		- Parent attribute is global -> use that exact parent attribute ID.
		- Parent attribute is local  -> use that exact parent attribute name.
		- Ensure a global option exists as a taxonomy term.
		- Ensure the option is assigned to the parent attribute before creating
		  the variation.
		"""
		woocommerce_server = (
			item.item_woocommerce_server.woocommerce_server
		)

		if (
			parent_wc_product.woocommerce_server
			!= woocommerce_server
		):
			frappe.throw(
				_(
					"Parent WooCommerce Product server {0} does not "
					"match variant server {1}."
				).format(
					parent_wc_product.woocommerce_server,
					woocommerce_server,
				)
			)

		parent_attributes = (
			self._get_parent_woocommerce_attributes(
				parent_wc_product
			)
		)

		result = []
		parent_changed = False

		for row in item.item.attributes:
			item_attribute = frappe.get_doc(
				"Item Attribute",
				row.attribute,
			)
			attribute_name = (
				item_attribute.attribute_name
				or item_attribute.name
			)
			option = str(
				row.attribute_value or ""
			).strip()

			parent_attribute = (
				self._find_parent_woocommerce_attribute(
					parent_attributes,
					attribute_name,
				)
			)

			if not parent_attribute:
				frappe.throw(
					_(
						"WooCommerce parent Product {0} on server {1} "
						"does not contain variant attribute {2}. "
						"Synchronize/review the template product before "
						"creating variant {3}."
					).format(
						parent_wc_product.woocommerce_id,
						woocommerce_server,
						attribute_name,
						item.item.name,
					)
				)

			if not parent_attribute.get(
				"variation",
				False,
			):
				frappe.throw(
					_(
						"WooCommerce parent attribute {0} on Product "
						"{1} / server {2} is not enabled for variations."
					).format(
						parent_attribute.get("name")
						or attribute_name,
						parent_wc_product.woocommerce_id,
						woocommerce_server,
					)
				)

			parent_attribute_id = int(
				parent_attribute.get("id") or 0
			)

			# Use the exact global attribute ID attached to THIS parent.
			if parent_attribute_id:
				self.ensure_global_woocommerce_attribute_terms(
					woocommerce_server,
					parent_attribute_id,
					[option],
				)

				result.append(
					{
						"id": parent_attribute_id,
						"option": option,
					}
				)
			else:
				# Parent is using a local/product-specific attribute.
				result.append(
					{
						"name": (
							parent_attribute.get("name")
							or attribute_name
						),
						"option": option,
					}
				)

			# The variation can only be properly selectable if its option is
			# assigned to the corresponding parent attribute.
			parent_options = list(
				parent_attribute.get("options") or []
			)
			parent_option_keys = {
				self._normalise_attribute_key(
					value
				)
				for value in parent_options
			}

			if (
				option
				and self._normalise_attribute_key(
					option
				)
				not in parent_option_keys
			):
				parent_options.append(option)
				parent_attribute["options"] = (
					parent_options
				)
				parent_changed = True

		# Repair only missing parent OPTIONS while preserving the parent's
		# existing global/local attribute structure.
		if parent_changed:
			wc_api = self._get_wc_api_for_server(
				woocommerce_server
			)
			endpoint = (
				f"products/"
				f"{parent_wc_product.woocommerce_id}"
			)

			response = wc_api.api.put(
				endpoint,
				data={
					"attributes": (
						self._build_parent_attributes_update_payload(
							parent_attributes
						)
					)
				},
			)

			if response.status_code != 200:
				log_and_raise_error(
					error_text=(
						"Unable to assign variant attribute options "
						f"to WooCommerce parent Product "
						f"{parent_wc_product.woocommerce_id} "
						f"on {woocommerce_server}"
					),
					response=response,
				)

			updated_parent = response.json()

			if isinstance(updated_parent, dict):
				parent_wc_product.attributes = json.dumps(
					updated_parent.get(
						"attributes",
						parent_attributes,
					)
				)

		return result
	def get_parent_woocommerce_product_for_server(
		self,
		item: ERPNextItemToSync,
	) -> tuple[Item, WooCommerceProduct]:
		"""
		Get the variant parent WooCommerce Product from the same WooCommerce Server.

		The ERPNext template Item code must never be assumed to be the WooCommerce
		parent Product ID. The parent ID is always read from the template Item's
		WooCommerce Server row matching the current variant's server.

		If the template has not yet been created on that server, synchronise only
		that template/server row first.
		"""
		if not item.item.variant_of:
			frappe.throw(
				_("Item {0} is not a variant.").format(
					item.item.name
				)
			)

		woocommerce_server = (
			item.item_woocommerce_server.woocommerce_server
		)

		parent_item = frappe.get_doc(
			"Item",
			item.item.variant_of,
		)

		parent_server_row = next(
			(
				row
				for row in parent_item.woocommerce_servers
				if row.woocommerce_server == woocommerce_server
			),
			None,
		)

		if not parent_server_row:
			frappe.throw(
				_(
					"Template Item {0} is not linked to WooCommerce "
					"Server {1}. Add the same WooCommerce Server to "
					"the template before synchronising variant {2}."
				).format(
					parent_item.name,
					woocommerce_server,
					item.item.name,
				)
			)

		if not parent_server_row.enabled:
			frappe.throw(
				_(
					"WooCommerce synchronization is disabled for "
					"Template Item {0} on server {1}."
				).format(
					parent_item.name,
					woocommerce_server,
				)
			)

		# The template may have just been created in ERPNext while its
		# background WooCommerce synchronization has not completed yet.
		# Synchronise only the template row for this same server.
		if not parent_server_row.woocommerce_id:
			parent_sync = SynchroniseItem(
				item=ERPNextItemToSync(
					item=parent_item,
					item_woocommerce_server_idx=(
						parent_server_row.idx
					),
				)
			)
			parent_sync.run()

			# Reload the template because parent_sync writes the WC ID
			# back to the Item WooCommerce Server child row.
			parent_item = frappe.get_doc(
				"Item",
				item.item.variant_of,
			)

			parent_server_row = next(
				(
					row
					for row in parent_item.woocommerce_servers
					if row.woocommerce_server
					== woocommerce_server
				),
				None,
			)

		if (
			not parent_server_row
			or not parent_server_row.woocommerce_id
		):
			frappe.throw(
				_(
					"Template Item {0} does not have a WooCommerce "
					"Product ID on server {1}."
				).format(
					parent_item.name,
					woocommerce_server,
				)
			)

		parent_product_name = (
			generate_woocommerce_record_name_from_domain_and_id(
				woocommerce_server,
				parent_server_row.woocommerce_id,
			)
		)

		# Load the exact parent from the exact WooCommerce Server.
		# This also verifies that the stored WooCommerce ID really exists.
		parent_wc_product = frappe.get_doc(
			"WooCommerce Product",
			parent_product_name,
		)

		if parent_wc_product.type != "variable":
			frappe.throw(
				_(
					"WooCommerce Product {0} on server {1} is type "
					"'{2}', but a variant requires a variable "
					"parent product."
				).format(
					parent_server_row.woocommerce_id,
					woocommerce_server,
					parent_wc_product.type,
				)
			)

		return parent_item, parent_wc_product
	def create_woocommerce_product(self, item: ERPNextItemToSync) -> None:
		"""
		Create the WooCommerce Product with fields from it's corresponding ERPNext Item
		"""
		if (
			item.item_woocommerce_server.woocommerce_server
			and item.item_woocommerce_server.enabled
			and not item.item_woocommerce_server.woocommerce_id
		):
			# Create a new WooCommerce Product doc
			wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})

			wc_product.type = "simple"
			wc_product.manage_stock = item.item.is_stock_item

			# Handle variants
			if item.item.has_variants:
				wc_product.type = "variable"

				wc_product.attributes = json.dumps(
					self.build_woocommerce_attributes_from_erpnext(
						item=item,
						variation=False,
					)
				)

			if item.item.variant_of:
				# Resolve the parent WooCommerce Product from the SAME
				# WooCommerce Server as the current variant.
				parent_item, parent_wc_product = (
					self.get_parent_woocommerce_product_for_server(item)
				)
				wc_product.parent_id = parent_wc_product.woocommerce_id
				wc_product.type = "variation"

				# Build attributes from the ACTUAL parent product on this server.
				# This preserves each parent's global/local attribute model and
				# prevents WooCommerce from silently dropping mismatched attrs.
				wc_product.attributes = json.dumps(
					self.build_woocommerce_variation_attributes_from_parent(
						item=item,
						parent_wc_product=parent_wc_product,
					)
				)

			# Set properties
			wc_product.woocommerce_server = item.item_woocommerce_server.woocommerce_server
			wc_product.woocommerce_name = item.item.item_name
			wc_product.regular_price = get_item_price_rate(item) or "0"

			sale_price_data = get_item_sale_price_data(item)
			if sale_price_data:
				wc_product.sale_price = sale_price_data.price_list_rate
				wc_product.date_on_sale_from = _format_sale_date(sale_price_data.valid_from)
				wc_product.date_on_sale_to = _format_sale_date(sale_price_data.valid_upto)

			self.set_product_fields(wc_product, item)

			wc_product.insert()
			self.woocommerce_product = wc_product

			# Reload ERPNext Item
			item.item.reload()
			item.item_woocommerce_server.woocommerce_id = wc_product.woocommerce_id
			item.item.flags.created_by_sync = True
			item.item.save()

			self.set_sync_hash()

	def create_item(self, wc_product: WooCommerceProduct) -> None:
		"""
		Create an ERPNext Item from the given WooCommerce Product
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_product.woocommerce_server)

		# Create Item
		item = frappe.new_doc("Item")

		# Handle variants' attributes.
		# Global and local WooCommerce attributes both resolve to the matched
		# ERPNext Item Attribute name.
		if wc_product.type in ["variable", "variation"]:
			resolved_attribute_names = (
				self.create_or_update_item_attributes(
					wc_product
				)
			)
			wc_attributes = json.loads(
				wc_product.attributes
			)

			for index, wc_attribute in enumerate(
				wc_attributes
			):
				row = item.append("attributes")
				row.attribute = (
					resolved_attribute_names[index]
				)

				if wc_product.type == "variation":
					row.attribute_value = (
						wc_attribute["option"]
					)

		# Handle variants
		if wc_product.type == "variable":
			item.has_variants = 1

		if wc_product.type == "variation":
			# Check if parent exists
			woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
				wc_product.woocommerce_server, wc_product.parent_id
			)
			parent_item, _parent_wc_product = run_item_sync(woocommerce_product_name=woocommerce_product_name)
			item.variant_of = parent_item.item_code

			# create_or_update_item_attributes() has already canonicalised
			# global variation options from taxonomy slugs to term names.
			# Build the ERPNext Item Name from those canonical business values
			# instead of the raw WooCommerce variation payload.
			canonical_attribute_values = [
				str(attribute.get("option") or "").strip()
				for attribute in json.loads(wc_product.attributes)
				if str(attribute.get("option") or "").strip()
			]

			if canonical_attribute_values:
				wc_product.woocommerce_name = (
					parent_item.item_name
					+ " - "
					+ ", ".join(canonical_attribute_values)
				)

		item.item_code = (
			wc_product.sku
			if wc_server.name_by == "Product SKU" and wc_product.sku
			else str(wc_product.woocommerce_id)
		)
		item.stock_uom = wc_server.uom or _("Nos")
		item.item_group = wc_server.item_group
		item.item_name = wc_product.woocommerce_name
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product.woocommerce_id
		row.woocommerce_server = wc_server.name
		item.flags.ignore_mandatory = True
		item.flags.created_by_sync = True

		if wc_server.enable_image_sync:
			wc_product_images = json.loads(wc_product.images)
			if len(wc_product_images) > 0:
				item.image = wc_product_images[0]["src"]

		_modified, item = self.set_item_fields(item=item)
		item.flags.created_by_sync = True

		item.insert()

		self.item = ERPNextItemToSync(
			item=item,
			item_woocommerce_server_idx=next(
				iws.idx
				for iws in item.woocommerce_servers
				if iws.woocommerce_server == wc_product.woocommerce_server
			),
		)

		self.set_sync_hash()



	def _normalise_attribute_value_key(self, value) -> str:
		"""
		Normalise attribute VALUES for equality checks only.

		Unlike attribute-name normalisation, punctuation such as hyphens and
		dots is preserved because it can be meaningful in a business value.
		"""
		return " ".join(
			unquote(str(value or ""))
			.strip()
			.casefold()
			.split()
		)

	def _get_global_woocommerce_attribute_term_records(
		self,
		woocommerce_server: str,
		attribute_id: int,
	) -> list[dict]:
		"""Return all terms for one global WooCommerce attribute, cached per sync."""
		cache_key = (
			woocommerce_server,
			int(attribute_id),
		)

		if cache_key not in self._wc_global_term_records_cache:
			self._wc_global_term_records_cache[cache_key] = (
				self._get_all_wc_records(
					woocommerce_server,
					f"products/attributes/{attribute_id}/terms",
				)
			)

		return self._wc_global_term_records_cache[cache_key]

	def _resolve_inbound_woocommerce_attribute_option(
		self,
		wc_product: WooCommerceProduct,
		wc_attribute: dict,
		option,
	) -> str:
		"""
		Resolve a WooCommerce global variation option to its canonical TERM NAME.

		Some stores/variation payloads expose the stored taxonomy term slug in
		``option`` while the parent/global attribute uses the human-readable term
		name. ERPNext must store the term name, not the slug, otherwise values such
		as ``2.70 متر`` and its slug-like representation become duplicates.

		Local/product-specific attributes (id=0) are left unchanged.
		"""
		option = str(option or "").strip()
		if not option:
			return option

		attribute_id = int(
			wc_attribute.get("id") or 0
		)

		if not attribute_id:
			return option

		terms = self._get_global_woocommerce_attribute_term_records(
			wc_product.woocommerce_server,
			attribute_id,
		)

		option_key = self._normalise_attribute_value_key(
			option
		)

		# Prefer an exact term-name match.
		for term in terms:
			term_name = str(
				term.get("name") or ""
			).strip()

			if (
				term_name
				and self._normalise_attribute_value_key(
					term_name
				)
				== option_key
			):
				return term_name

		# Then match the stored taxonomy slug and return the canonical name.
		for term in terms:
			term_slug = str(
				term.get("slug") or ""
			).strip()

			if (
				term_slug
				and self._normalise_attribute_value_key(
					term_slug
				)
				== option_key
			):
				term_name = str(
					term.get("name") or ""
				).strip()

				return term_name or option

		return option
	def create_or_update_item_attributes(
		self,
		wc_product: WooCommerceProduct,
	) -> list[str]:
		"""
		Create/update ERPNext Item Attributes additively.

		Global WooCommerce options are canonicalised to the WooCommerce term NAME
		before they are stored in ERPNext. This prevents taxonomy slugs from being
		created as duplicate ERPNext Attribute Values.

		Existing ERPNext values are never deleted merely because a particular
		WooCommerce product/variation does not use them.

		Returns ERPNext Item Attribute names in the same order as the WooCommerce
		product's attributes.
		"""
		resolved_attribute_names = []

		if not wc_product.attributes:
			return resolved_attribute_names

		wc_attributes = json.loads(
			wc_product.attributes
		)

		for wc_attribute in wc_attributes:
			attribute_name = (
				self._get_wc_attribute_display_name(
					wc_product,
					wc_attribute,
				)
			)

			erpnext_attribute_name = (
				self._find_existing_erpnext_item_attribute(
					attribute_name,
					wc_attribute.get("slug"),
				)
			)

			if erpnext_attribute_name:
				item_attribute = frappe.get_doc(
					"Item Attribute",
					erpnext_attribute_name,
				)
				is_new = False
			else:
				item_attribute = frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": attribute_name,
					}
				)
				is_new = True

			if wc_product.type == "variable":
				raw_options = (
					wc_attribute.get("options")
					or []
				)

				options = [
					self._resolve_inbound_woocommerce_attribute_option(
						wc_product,
						wc_attribute,
						option,
					)
					for option in raw_options
				]

				# Keep the WooCommerce virtual document canonical too, so any
				# later Item construction reads the proper business values.
				wc_attribute["options"] = options

			else:
				raw_option = wc_attribute.get(
					"option"
				)

				option = (
					self._resolve_inbound_woocommerce_attribute_option(
						wc_product,
						wc_attribute,
						raw_option,
					)
					if raw_option not in (None, "")
					else ""
				)

				options = (
					[option]
					if option
					else []
				)

				# create_item() reads the attributes again after this method.
				# Replace the raw slug-like option with the canonical term name.
				wc_attribute["option"] = option

			existing_by_key = {
				self._normalise_attribute_value_key(
					value.attribute_value
				): value.attribute_value
				for value in (
					item_attribute.item_attribute_values
				)
				if value.attribute_value
			}

			values_added = False

			for option in options:
				option = str(
					option or ""
				).strip()

				if not option:
					continue

				key = self._normalise_attribute_value_key(
					option
				)

				if key in existing_by_key:
					continue

				row = item_attribute.append(
					"item_attribute_values"
				)
				row.attribute_value = option

				# Current rule: new abbreviation = exact business value.
				row.abbr = option

				existing_by_key[key] = option
				values_added = True

			item_attribute.flags.ignore_mandatory = True

			if is_new:
				item_attribute.insert()
			elif values_added:
				item_attribute.save()

			resolved_attribute_names.append(
				item_attribute.name
			)

		# Persist canonical term names on the in-memory virtual Woo product.
		# create_item() immediately reads this JSON to build Variant Attributes.
		wc_product.attributes = json.dumps(
			wc_attributes
		)

		return resolved_attribute_names
	def set_item_fields(self, item: Item) -> tuple[bool, Item]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
		WooCommerce to ERPNext
		"""
		item_dirty = False
		if item and self.woocommerce_product:
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.woocommerce_product.woocommerce_server
			)
			if wc_server.item_field_map:
				woocommerce_product_dict = (
					self.woocommerce_product.deserialize_attributes_of_type_dict_or_list(
						self.woocommerce_product.to_dict()
					)
				)
				for map in wc_server.item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")

					# We expect woocommerce_field_name to be valid JSONPath
					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_product_field_matches = jsonpath_expr.find(woocommerce_product_dict)

					setattr(item, erpnext_item_field_name[0], woocommerce_product_field_matches[0].value)
					item_dirty = True
		return item_dirty, item

	def set_product_fields(
		self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> tuple[bool, WooCommerceProduct]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
		ERPNext to WooCommerce

		Returns true if woocommerce_product was changed
		"""
		wc_product_dirty = False
		if item and woocommerce_product:
			wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
			if wc_server.item_field_map:
				# Deserialize the WooCommerce Product's list and dict fields because we want to potentially perform
				# in-place updates on the whole dict using jsonpath-ng. Use the existing class method for this.
				wc_product_with_deserialised_fields = (
					woocommerce_product.deserialize_attributes_of_type_dict_or_list(woocommerce_product)
				)

				for map in wc_server.item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")
					erpnext_item_field_value = getattr(item.item, erpnext_item_field_name[0])

					# We expect woocommerce_field_name to be valid JSONPath
					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_product_field_matches = jsonpath_expr.find(
						wc_product_with_deserialised_fields
					)

					if len(woocommerce_product_field_matches) == 0:
						if woocommerce_product.name:
							# We're strict about existing WooCommerce Products, the field should exist
							raise ValueError(
								_("Field <code>{0}</code> not found in WooCommerce Product {1}").format(
									map.woocommerce_field_name, woocommerce_product.name
								)
							)
						else:
							# For new WooCommerce Products, the nested field may not exist yet, so don't stop the sync
							continue

					# JSONPath parsing typically returns a list, we'll only take the first value
					woocommerce_product_field_value = woocommerce_product_field_matches[0].value

					if erpnext_item_field_value != woocommerce_product_field_value:
						jsonpath_expr.update(wc_product_with_deserialised_fields, erpnext_item_field_value)
						wc_product_dirty = True

				if wc_product_dirty:
					# Re-serialize the WooCommerce Product's list and dict fields, because we deserialized earlier
					woocommerce_product = woocommerce_product.serialize_attributes_of_type_dict_or_list(
						wc_product_with_deserialised_fields
					)

		return wc_product_dirty, woocommerce_product

	def set_sync_hash(self):
		"""
		Set the last sync hash value using db.set_value, as it does not call the ORM triggers
		and it does not update the modified timestamp (by using the update_modified parameter)
		"""
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"woocommerce_last_sync_hash",
			self.woocommerce_product.woocommerce_date_modified,
			update_modified=False,
		)

		# If item was synchronised but the item is set not to sync, turn on the enabled flag
		# Items that are disabled for sync will still be synced if it is ordered on WooCommerce
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"enabled",
			1,
			update_modified=False,
		)


def get_list_of_wc_products(
	item: ERPNextItemToSync | None = None, date_time_from: datetime | None = None
) -> list[WooCommerceProduct]:
	"""
	Fetches a list of WooCommerce Products within a specified date range or linked with an Item, using pagination.

	At least one of date_time_from, item parameters are required
	"""
	if not any([date_time_from, item]):
		raise ValueError("At least one of date_time_from or item parameters are required")

	wc_records_per_page_limit = 100
	page_length = wc_records_per_page_limit
	new_results = True
	start = 0
	filters = []
	wc_products = []
	servers = None

	# Build filters
	if date_time_from:
		filters.append(["WooCommerce Product", "date_modified", ">", date_time_from])
	if item:
		filters.append(["WooCommerce Product", "id", "=", item.item_woocommerce_server.woocommerce_id])
		servers = [item.item_woocommerce_server.woocommerce_server]

	while new_results:
		woocommerce_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		new_results = woocommerce_product.get_list(
			args={
				"filters": filters,
				"page_lenth": page_length,
				"start": start,
				"servers": servers,
				"as_doc": True,
			}
		)
		for wc_product in new_results:
			wc_products.append(wc_product)
		start += page_length
		if len(new_results) < page_length:
			new_results = []

	return wc_products


def get_item_price_rate(item: ERPNextItemToSync):
	"""
	Get the Item Price if Item Price sync is enabled
	"""
	wc_server = frappe.get_cached_doc("WooCommerce Server", item.item_woocommerce_server.woocommerce_server)
	if wc_server.enable_price_list_sync:
		item_prices = frappe.get_all(
			"Item Price",
			filters={"item_code": item.item.item_name, "price_list": wc_server.price_list},
			fields=["price_list_rate", "valid_upto"],
		)
		return next(
			(
				price.price_list_rate
				for price in item_prices
				if not price.valid_upto or price.valid_upto > now()
			),
			None,
		)


def get_item_sale_price_data(item: ERPNextItemToSync) -> frappe._dict | None:
	"""
	Get the sale price rate and validity dates for an Item if Sales Price List
	sync is enabled on the linked WooCommerce Server.

	Returns a _dict with price_list_rate, valid_from, valid_upto or None if
	sale price sync is not enabled or no valid price record is found.
	"""
	wc_server = frappe.get_cached_doc("WooCommerce Server", item.item_woocommerce_server.woocommerce_server)
	if not (
		wc_server.enable_price_list_sync
		and wc_server.enable_sales_price_list_sync
		and wc_server.sales_price_list
	):
		return None

	item_prices = frappe.get_all(
		"Item Price",
		filters={"item_code": item.item.item_name, "price_list": wc_server.sales_price_list},
		fields=["price_list_rate", "valid_from", "valid_upto"],
	)
	return next(
		(price for price in item_prices if not price.valid_upto or price.valid_upto > now()),
		None,
	)


def clear_sync_hash_and_run_item_sync(item_code: str):
	"""
	Clear the last sync hash value using db.set_value, as it does not call the ORM triggers
	and it does not update the modified timestamp (by using the update_modified parameter)
	"""

	iws = frappe.qb.DocType("Item WooCommerce Server")

	iwss = (frappe.qb.from_(iws).where(iws.enabled == 1).where(iws.parent == item_code).select(iws.name)).run(
		as_dict=True
	)

	for iws in iwss:
		frappe.db.set_value(
			"Item WooCommerce Server",
			iws.name,
			"woocommerce_last_sync_hash",
			None,
			update_modified=False,
		)

	if len(iwss) > 0:
		run_item_sync(item_code=item_code, enqueue=True)

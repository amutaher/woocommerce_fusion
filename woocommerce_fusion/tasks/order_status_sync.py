from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WC_ORDER_STATUS_MAPPING,
	WooCommerceOrder,
)
from woocommerce_fusion.woocommerce.woocommerce_api import log_and_raise_error


def _get_woocommerce_order_api(woocommerce_server: str):
	wc_api = next(
		(
			api
			for api in WooCommerceOrder._init_api()
			if api.woocommerce_server == woocommerce_server
		),
		None,
	)

	if not wc_api:
		frappe.throw(
			_(
				"No enabled WooCommerce API was found for server {0}."
			).format(woocommerce_server)
		)

	return wc_api


def _get_expected_woocommerce_status(sales_order, wc_server):
	"""
	Return:
	    (configured_label, WooCommerce REST slug)

	Example:
	    ERP Sales Order status = Completed
	    configured Woo label   = Shipped
	    Woo REST slug          = completed
	"""
	mapping = next(
		(
			row
			for row in (wc_server.sales_order_status_map or [])
			if row.erpnext_sales_order_status == sales_order.status
		),
		None,
	)

	if not mapping:
		return None, None

	status_label = str(
		mapping.woocommerce_sales_order_status or ""
	).strip()

	if not status_label:
		return None, None

	status_slug = WC_ORDER_STATUS_MAPPING.get(
		status_label
	)

	if not status_slug:
		frappe.throw(
			_(
				"WooCommerce status {0} configured for ERPNext "
				"Sales Order status {1} has no REST status mapping."
			).format(
				status_label,
				sales_order.status,
			)
		)

	return status_label, status_slug


def sales_order_status_sync_required(sales_order) -> bool:
	"""
	Decide whether an ERP-side change should enqueue a dedicated remote
	status verification.

	The local woocommerce_status field represents the LAST CONFIRMED remote
	status. It must never be pre-written with the intended status.

	Cancelled ERPNext Sales Orders are intentionally allowed here. Cancellation
	is a valid business status transition that must be pushed to WooCommerce
	when a Cancelled -> Cancelled mapping is configured.
	"""
	if (
		not sales_order
		or sales_order.flags.get("created_by_sync")
		or not sales_order.woocommerce_id
		or not sales_order.woocommerce_server
	):
		return False

	wc_server = frappe.get_cached_doc(
		"WooCommerce Server",
		sales_order.woocommerce_server,
	)

	if (
		not wc_server.enable_sync
		or not wc_server.enable_so_status_sync
	):
		return False

	expected_label, _expected_slug = (
		_get_expected_woocommerce_status(
			sales_order,
			wc_server,
		)
	)

	if not expected_label:
		return False

	# Primary trigger: local confirmed Woo status differs from the status
	# currently expected from ERPNext.
	if (
		str(
			sales_order.woocommerce_status
			or ""
		).strip()
		!= expected_label
	):
		return True

	# Also verify whenever ERPNext's own Sales Order status changed.
	# This protects against stale/incorrect tracking data.
	try:
		return bool(
			sales_order.has_value_changed(
				"status"
			)
		)
	except Exception:
		return False


def queue_sales_order_status_sync(
	sales_order,
) -> bool:
	"""
	Queue a dedicated order-status synchronization job after commit.

	Return True if queued.
	"""
	if not sales_order_status_sync_required(
		sales_order
	):
		return False

	frappe.enqueue(
		sync_sales_order_status,
		queue="long",
		enqueue_after_commit=True,
		sales_order_name=sales_order.name,
	)

	return True


def sync_sales_order_status(
	sales_order_name: str,
	force_verify: bool = True,
):
	"""
	Synchronize exactly one ERPNext Sales Order status to WooCommerce.

	Important invariants:
	1. GET actual WooCommerce status.
	2. PUT only if actual != expected.
	3. GET again and verify.
	4. ONLY AFTER remote confirmation:
	   - update Sales Order.woocommerce_status;
	   - update custom_woocommerce_last_sync_hash.

	This function intentionally does not rely on the generic order
	timestamp/hash direction-selection logic.

	Cancelled documents (docstatus=2) are supported. They remain readable in
	ERPNext and can still be synchronized to WooCommerce status ``cancelled``.
	"""
	lock_name = (
		"woocommerce_fusion:"
		"sales_order_status_sync:"
		f"{frappe.local.site}:"
		f"{sales_order_name}"
	)

	with frappe.cache.lock(
		lock_name,
		timeout=900,
	):
		sales_order = frappe.get_doc(
			"Sales Order",
			sales_order_name,
		)

		if (
			not sales_order.woocommerce_id
			or not sales_order.woocommerce_server
		):
			return {
				"sales_order": sales_order_name,
				"result": "SKIPPED",
			}

		wc_server = frappe.get_cached_doc(
			"WooCommerce Server",
			sales_order.woocommerce_server,
		)

		if (
			not wc_server.enable_sync
			or not wc_server.enable_so_status_sync
		):
			return {
				"sales_order": sales_order_name,
				"result": "SKIPPED",
			}

		expected_label, expected_slug = (
			_get_expected_woocommerce_status(
				sales_order,
				wc_server,
			)
		)

		if not expected_label:
			return {
				"sales_order": sales_order_name,
				"result": "NO_MAPPING",
			}

		wc_api = _get_woocommerce_order_api(
			sales_order.woocommerce_server
		)

		order_id = int(
			sales_order.woocommerce_id
		)

		endpoint = f"orders/{order_id}"

		response = wc_api.api.get(
			endpoint
		)

		if response.status_code != 200:
			log_and_raise_error(
				error_text=(
					"Unable to read WooCommerce order "
					"during Sales Order status synchronization"
				),
				response=response,
			)

		remote_order = response.json()

		actual_slug = str(
			remote_order.get("status")
			or ""
		).strip()

		changed = False

		if actual_slug != expected_slug:
			update_response = wc_api.api.put(
				endpoint,
				data={
					"status": expected_slug,
				},
			)

			if update_response.status_code != 200:
				log_and_raise_error(
					error_text=(
						"Unable to update WooCommerce order "
						"status from ERPNext"
					),
					response=update_response,
				)

			changed = True

		# Always verify with a fresh GET. This makes a successful HTTP PUT
		# insufficient by itself to mark ERP as synchronized.
		verify_response = wc_api.api.get(
			endpoint
		)

		if verify_response.status_code != 200:
			log_and_raise_error(
				error_text=(
					"Unable to verify WooCommerce order "
					"status after synchronization"
				),
				response=verify_response,
			)

		verified_order = (
			verify_response.json()
		)

		verified_slug = str(
			verified_order.get("status")
			or ""
		).strip()

		if verified_slug != expected_slug:
			frappe.throw(
				_(
					"WooCommerce order status verification failed "
					"for Sales Order {0}. Expected {1}, got {2}."
				).format(
					sales_order.name,
					expected_slug,
					verified_slug,
				)
			)

		values = {
			"woocommerce_status": (
				expected_label
			),
		}

		remote_modified = (
			verified_order.get(
				"date_modified"
			)
		)

		if remote_modified:
			values[
				"custom_woocommerce_last_sync_hash"
			] = remote_modified

		frappe.db.set_value(
			"Sales Order",
			sales_order.name,
			values,
			update_modified=False,
		)

		return {
			"sales_order": (
				sales_order.name
			),
			"woocommerce_server": (
				sales_order.woocommerce_server
			),
			"woocommerce_order_id": (
				order_id
			),
			"expected_label": (
				expected_label
			),
			"expected_status": (
				expected_slug
			),
			"previous_status": (
				actual_slug
			),
			"verified_status": (
				verified_slug
			),
			"changed": changed,
			"result": (
				"UPDATED"
				if changed
				else "ALREADY_CORRECT"
			),
		}


def sync_sales_order_status_and_reconciliation(
	sales_order_name: str,
	run_reconciliation_after: bool = False,
):
	"""
	Run the dedicated WooCommerce status synchronization first.

	If the same Sales Order change also needs the existing return/reservation/
	refund reconciliation, enqueue that reconciliation ONLY after WooCommerce
	has confirmed the expected status.

	This prevents the generic reconciliation/order-sync path from racing with
	the status job and pushing an old local woocommerce_status back to Woo.
	"""
	result = sync_sales_order_status(
		sales_order_name=sales_order_name,
	)

	if run_reconciliation_after:
		from woocommerce_fusion.tasks.order_enhancements import (
			_enqueue_sales_order_reconciliation,
		)

		_enqueue_sales_order_reconciliation(
			sales_order_name
		)

	return result


def reconcile_recent_sales_order_statuses():
	"""
	Hourly safety net.

	Verify WooCommerce status for recent linked Sales Orders even if the
	original event-driven job failed because of a transient API/worker issue.

	This intentionally checks only recent orders to keep API traffic bounded.
	"""
	since = add_to_date(
		now_datetime(),
		days=-1,
		as_string=True,
	)

	sales_order_names = frappe.get_all(
		"Sales Order",
		filters={
			"woocommerce_id": [
				"is",
				"set",
			],
			"woocommerce_server": [
				"is",
				"set",
			],
			# Include cancelled Sales Orders (docstatus=2).
			# The configured ERP -> Woo status mapping decides whether
			# there is anything to synchronize.
			"modified": [
				">=",
				since,
			],
		},
		pluck="name",
		order_by="modified desc",
		limit_page_length=100,
	)

	for sales_order_name in (
		sales_order_names
	):
		try:
			frappe.enqueue(
				sync_sales_order_status,
				queue="long",
				sales_order_name=(
					sales_order_name
				),
			)
		except Exception:
			frappe.log_error(
				title=(
					"WooCommerce Sales Order "
					"Status Reconciliation Queue Failed"
				),
				message=(
					frappe.get_traceback()
				),
			)

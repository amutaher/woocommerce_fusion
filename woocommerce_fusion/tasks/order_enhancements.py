from __future__ import annotations

import hashlib

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe import _
from frappe.utils import cint, cstr, flt

from woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WooCommerceOrder,
)
from woocommerce_fusion.woocommerce.woocommerce_api import log_and_raise_error


CREATE_INVOICE_FIELD = "custom_create_sales_invoice"
SUBMIT_INVOICE_FIELD = "custom_submit_sales_invoice"
REFUND_QTY_FROM_RETURN_INVOICE_FIELD = (
	"custom_refund_qty_from_return_sales_invoice"
)
REDUCE_BIN_RESERVED_QTY_FIELD = (
	"custom_reduce_bin_reserved_qty_on_financial_return"
)

ORDER_LINE_ID_FIELD = "custom_woocommerce_order_line_id"
REFUNDED_QTY_FIELD = "custom_woocommerce_refunded_qty"
REFUNDED_AMOUNT_FIELD = "custom_woocommerce_refunded_amount"
LAST_REFUND_ID_FIELD = "custom_woocommerce_last_refund_id"

REFUND_SYNC_KEY_META = "erpnext_refund_sync_key"
REFUND_SALES_ORDER_META = "erpnext_sales_order"


def validate_woocommerce_server_settings(doc, method=None):
	"""Validate the dependency between the two custom invoice settings."""
	create_invoice = bool(doc.get(CREATE_INVOICE_FIELD))
	submit_invoice = bool(doc.get(SUBMIT_INVOICE_FIELD))

	if create_invoice and not doc.submit_sales_orders:
		frappe.throw(
			_(
				"'Create Sales Invoice during Synchronisation' requires "
				"'Submit Sales Orders during Synchronisation'."
			)
		)

	if submit_invoice and not create_invoice:
		frappe.throw(
			_(
				"'Submit Sales Invoice during Synchronisation' requires "
				"'Create Sales Invoice during Synchronisation'."
			)
		)

	if doc.get(REDUCE_BIN_RESERVED_QTY_FIELD) and not cint(
		frappe.db.get_single_value(
			"Selling Settings",
			"dont_reserve_sales_order_qty_on_sales_return",
		)
	):
		frappe.throw(
			_(
				"Enable 'Don't Reserve Sales Order Qty on Sales Return' "
				"in Selling Settings before enabling "
				"'Reduce Bin Reserved Qty on Financial Return'."
			)
		)


def create_sales_invoice_on_sales_order_submit(doc, method=None):
	"""
	Create one Sales Invoice immediately after a WooCommerce Sales Order is submitted.

	The method is idempotent: an existing non-cancelled Sales Invoice linked through
	Sales Invoice Item.sales_order prevents creation of another invoice.
	"""
	if (
		doc.doctype != "Sales Order"
		or doc.docstatus != 1
		or not doc.woocommerce_id
		or not doc.woocommerce_server
	):
		return None

	wc_server = frappe.get_cached_doc(
		"WooCommerce Server",
		doc.woocommerce_server,
	)

	if not wc_server.enable_sync or not wc_server.get(CREATE_INVOICE_FIELD):
		return None

	if not wc_server.submit_sales_orders:
		frappe.throw(
			_(
				"WooCommerce Server {0} must submit Sales Orders before "
				"automatic Sales Invoice creation can be used."
			).format(wc_server.name)
		)

	existing_invoice_names = frappe.get_all(
		"Sales Invoice Item",
		filters={
			"sales_order": doc.name,
			"docstatus": ["<", 2],
		},
		pluck="parent",
	)
	existing_invoice_names = list(dict.fromkeys(existing_invoice_names))

	if existing_invoice_names:
		return frappe.get_doc("Sales Invoice", existing_invoice_names[0])

	sales_invoice = make_sales_invoice(
		doc.name,
		ignore_permissions=True,
	)

	if not sales_invoice.items:
		frappe.throw(
			_(
				"No billable items were found for Sales Order {0}."
			).format(doc.name)
		)

	sales_invoice.flags.created_by_sync = True
	sales_invoice.flags.ignore_permissions = True
	sales_invoice.insert(ignore_permissions=True)

	if wc_server.get(SUBMIT_INVOICE_FIELD):
		sales_invoice.flags.ignore_permissions = True
		sales_invoice.submit()

	return sales_invoice


def sales_order_has_pending_refund(sales_order) -> bool:
	"""Return True when a linked submitted Sales Order has a positive refund delta."""
	if (
		not sales_order
		or sales_order.docstatus != 1
		or not sales_order.woocommerce_id
		or not sales_order.woocommerce_server
	):
		return False

	wc_server = frappe.get_cached_doc(
		"WooCommerce Server",
		sales_order.woocommerce_server,
	)
	target_quantities = _get_target_refund_quantities(
		sales_order=sales_order,
		wc_server=wc_server,
	)
	precision = sales_order.precision("grand_total") or 2

	for so_item in sales_order.items:
		target_qty = flt(target_quantities.get(so_item.name))
		if target_qty <= 0:
			continue

		target_amount = max(
			flt(so_item.net_amount, precision)
			- flt(so_item.billed_amt, precision),
			0,
		)
		synced_qty = flt(so_item.get(REFUNDED_QTY_FIELD))
		synced_amount = flt(
			so_item.get(REFUNDED_AMOUNT_FIELD),
			precision,
		)

		if target_qty > synced_qty or target_amount > synced_amount:
			return True

	return False


def _get_target_refund_quantities(sales_order, wc_server) -> dict[str, float]:
	"""
	Return cumulative target refund quantities keyed by Sales Order Item name.

	Default mode:
		Sales Order Item.returned_qty

	Return Sales Invoice mode:
		Absolute submitted Return Sales Invoice Item quantity linked through
		Sales Invoice Item.so_detail. This works whether Update Stock is enabled
		on the Return Sales Invoice or not.
	"""
	if not wc_server.get(REFUND_QTY_FROM_RETURN_INVOICE_FIELD):
		return {
			so_item.name: flt(so_item.returned_qty)
			for so_item in sales_order.items
		}

	rows = frappe.db.sql(
		"""
		SELECT
			sii.so_detail,
			SUM(ABS(sii.qty)) AS returned_invoice_qty
		FROM `tabSales Invoice Item` sii
		INNER JOIN `tabSales Invoice` si
			ON si.name = sii.parent
		WHERE
			si.docstatus = 1
			AND si.is_return = 1
			AND sii.sales_order = %(sales_order)s
			AND IFNULL(sii.so_detail, '') != ''
		GROUP BY sii.so_detail
		""",
		{"sales_order": sales_order.name},
		as_dict=True,
	)

	return {
		row.so_detail: flt(row.returned_invoice_qty)
		for row in rows
	}


def _get_refund_quantity_source_label(wc_server) -> str:
	if wc_server.get(REFUND_QTY_FROM_RETURN_INVOICE_FIELD):
		return "Return Sales Invoice"

	return "Sales Order returned_qty"


def queue_refund_from_sales_order(
    doc,
    method=None,
):
    """
    Queue return / reservation / refund reconciliation
    on submitted WooCommerce-linked Sales Order changes.

    If the controller already queued a status synchronization,
    the status job will queue this reconciliation AFTER Woo
    confirms the new status.
    """

    if (
        doc.doctype != "Sales Order"
        or doc.docstatus != 1
        or not doc.woocommerce_id
        or not doc.woocommerce_server
    ):
        return

    wc_server = frappe.get_cached_doc(
        "WooCommerce Server",
        doc.woocommerce_server,
    )

    if not wc_server.enable_sync:
        return

    # Prevent two jobs from competing for the same Sales Order.
    if doc.flags.get(
        "woocommerce_status_sync_queued"
    ):
        return

    _enqueue_sales_order_reconciliation(
        doc.name
    )


def queue_refund_from_sales_invoice(doc, method=None):
	"""
	Queue reconciliation after any Return Sales Invoice submit/cancel.

	This covers both:
	- Update Stock enabled returns, which standard ERPNext uses to update
	  Sales Order Item.returned_qty; and
	- financial returns where Update Stock is disabled.

	Running after commit ensures the reconciler executes after ERPNext's standard
	status updater has completed.
	"""
	if (
		doc.doctype != "Sales Invoice"
		or not cint(doc.is_return)
		or doc.docstatus not in (1, 2)
	):
		return

	for sales_order_name in _get_return_document_sales_orders(doc):
		_enqueue_sales_order_reconciliation(sales_order_name)


def queue_reconciliation_from_delivery_note(doc, method=None):
	"""
	Queue reconciliation after any Return Delivery Note submit/cancel.

	ERPNext's return status updater can overwrite Sales Order Item.returned_qty
	with physical-return sources only. The queued reconciliation restores the
	combined physical and financial return quantity after commit.
	"""
	if (
		doc.doctype != "Delivery Note"
		or not cint(doc.is_return)
		or doc.docstatus not in (1, 2)
	):
		return

	for sales_order_name in _get_return_document_sales_orders(doc):
		_enqueue_sales_order_reconciliation(sales_order_name)


def _get_return_document_sales_orders(doc) -> list[str]:
	"""Return unique linked Sales Orders from a Sales Invoice or Delivery Note."""
	sales_order_names = {
		row.get("sales_order")
		or row.get("against_sales_order")
		for row in doc.items
		if (
			row.get("sales_order")
			or row.get("against_sales_order")
		)
	}

	return sorted(
		name
		for name in sales_order_names
		if name
	)


def _enqueue_sales_order_reconciliation(
	sales_order_name: str,
):
	"""Queue one deduplicated reconciliation job per Sales Order."""
	frappe.enqueue(
		"woocommerce_fusion.tasks.order_enhancements."
		"reconcile_sales_order_and_sync",
		queue="long",
		enqueue_after_commit=True,
		job_id=(
			"woocommerce-order-reconciliation::"
			f"{sales_order_name}"
		),
		deduplicate=True,
		sales_order_name=sales_order_name,
		check_permissions=False,
	)


@frappe.whitelist()
def reconcile_sales_order_and_sync(
	sales_order_name: str,
	check_permissions: bool | str = True,
):
	"""
	Rebuild return quantities, recalculate Bin.reserved_qty, and queue
	WooCommerce stock/refund synchronization for one Sales Order.

	This is also the method used by the Sales Order manual action button.
	"""
	check_permissions = cint(check_permissions)

	sales_order = frappe.get_doc(
		"Sales Order",
		sales_order_name,
	)

	if check_permissions:
		sales_order.check_permission("write")

	lock_name = (
		"woocommerce_fusion:"
		"sales_order_reconciliation:"
		f"{sales_order_name}"
	)

	with frappe.cache.lock(lock_name, timeout=900):
		return _reconcile_sales_order_and_sync(
			sales_order_name
		)


def _reconcile_sales_order_and_sync(
	sales_order_name: str,
) -> dict:
	"""
	Internal reconciliation implementation.

	When the financial-return reservation option is enabled, returned_qty becomes
	a derived value rebuilt from all submitted return documents. This prevents
	ERPNext's Delivery Note/Sales Invoice status updater from permanently removing
	financial return quantities.
	"""
	sales_order = frappe.get_doc(
		"Sales Order",
		sales_order_name,
	)

	result = {
		"sales_order": sales_order.name,
		"recalculated_returned_qty": False,
		"changed_rows": [],
		"capped_rows": [],
		"stock_items_queued": [],
		"refund_queued": False,
		"skipped": False,
		"reason": None,
	}

	if sales_order.docstatus != 1:
		result.update(
			{
				"skipped": True,
				"reason": "Sales Order is not submitted.",
			}
		)
		return result

	if (
		not sales_order.woocommerce_id
		or not sales_order.woocommerce_server
	):
		result.update(
			{
				"skipped": True,
				"reason": (
					"Sales Order is not linked to WooCommerce."
				),
			}
		)
		return result

	wc_server = frappe.get_cached_doc(
		"WooCommerce Server",
		sales_order.woocommerce_server,
	)

	if not wc_server.enable_sync:
		result.update(
			{
				"skipped": True,
				"reason": "WooCommerce Server synchronization is disabled.",
			}
		)
		return result

	so_detail_names = [
		row.name
		for row in sales_order.items
	]
	returned_qty_map = get_total_returned_qty_map(
		sales_order.name
	)

	if wc_server.get(REDUCE_BIN_RESERVED_QTY_FIELD):
		if not cint(
			frappe.db.get_single_value(
				"Selling Settings",
				"dont_reserve_sales_order_qty_on_sales_return",
			)
		):
			frappe.throw(
				_(
					"Enable 'Don't Reserve Sales Order Qty on Sales Return' "
					"in Selling Settings before using "
					"'Reduce Bin Reserved Qty on Financial Return'."
				)
			)

		for so_item in sales_order.items:
			cumulative_return_qty = flt(
				returned_qty_map.get(so_item.name)
			)

			# Prevent get_reserved_qty() from producing a negative reservation
			# when a financial credit note exceeds the still-undelivered quantity.
			max_return_qty_for_reservation = max(
				flt(so_item.qty)
				- flt(so_item.delivered_qty),
				0,
			)
			target_returned_qty = min(
				cumulative_return_qty,
				max_return_qty_for_reservation,
			)

			if (
				cumulative_return_qty
				> max_return_qty_for_reservation
			):
				result["capped_rows"].append(
					{
						"sales_order_item": so_item.name,
						"item_code": so_item.item_code,
						"cumulative_return_qty": (
							cumulative_return_qty
						),
						"stored_returned_qty": (
							target_returned_qty
						),
						"reason": (
							"Return quantity exceeds the "
							"undelivered Sales Order quantity."
						),
					}
				)

			if flt(so_item.returned_qty) == flt(
				target_returned_qty
			):
				continue

			frappe.db.set_value(
				"Sales Order Item",
				so_item.name,
				"returned_qty",
				target_returned_qty,
				update_modified=False,
			)
			result["changed_rows"].append(
				{
					"sales_order_item": so_item.name,
					"item_code": so_item.item_code,
					"old_returned_qty": flt(
						so_item.returned_qty
					),
					"new_returned_qty": (
						target_returned_qty
					),
				}
			)

		# Standard ERPNext path:
		# SalesOrder.update_reserved_qty()
		# -> get_reserved_qty()
		# -> update_bin_qty(..., {"reserved_qty": ...})
		sales_order.update_reserved_qty(
			so_detail_names
		)
		result["recalculated_returned_qty"] = True
		sales_order.reload()
	else:
		# Even without the custom financial-return option, recalculate Bin values
		# from the current standard Sales Order quantities.
		sales_order.update_reserved_qty(
			so_detail_names
		)
		sales_order.reload()

	item_codes = sorted(
		{
			row.item_code
			for row in sales_order.items
			if row.item_code
		}
	)

	for item_code in item_codes:
		_enqueue_stock_update(item_code)

	result["stock_items_queued"] = item_codes

	# "Check refund" on every reconciliation. A job is queued only when a
	# positive unsynchronized refund quantity or amount exists.
	if sales_order_has_pending_refund(sales_order):
		_enqueue_refund(sales_order.name)
		result["refund_queued"] = True

	if result["capped_rows"]:
		frappe.log_error(
			title=(
				"WooCommerce Return Quantity Capped: "
				f"{sales_order.name}"
			),
			message=frappe.as_json(
				result["capped_rows"],
				indent=2,
			),
		)

	return result


def get_total_returned_qty_map(
	sales_order_name: str,
) -> dict[str, float]:
	"""
	Return cumulative submitted return quantity by Sales Order Item.

	Sources:
	1. Return Delivery Note Items.
	2. Return Sales Invoice Items with Update Stock enabled.
	3. Financial Return Sales Invoice Items with Update Stock disabled.

	A Return Sales Invoice linked to a submitted Return Delivery Note is excluded
	from the Sales Invoice portion because the physical Return Delivery Note has
	already contributed the same quantity.
	"""
	rows = frappe.db.sql(
		"""
		SELECT
			returns.so_detail,
			SUM(returns.returned_qty) AS returned_qty
		FROM (
			SELECT
				dni.so_detail,
				GREATEST(-1 * dni.qty, 0)
					AS returned_qty
			FROM `tabDelivery Note Item` dni
			INNER JOIN `tabDelivery Note` dn
				ON dn.name = dni.parent
			WHERE
				dn.docstatus = 1
				AND dn.is_return = 1
				AND IFNULL(dni.so_detail, '') != ''
				AND dni.so_detail IN (
					SELECT name
					FROM `tabSales Order Item`
					WHERE parent = %(sales_order)s
				)

			UNION ALL

			SELECT
				sii.so_detail,
				GREATEST(-1 * sii.qty, 0)
					AS returned_qty
			FROM `tabSales Invoice Item` sii
			INNER JOIN `tabSales Invoice` si
				ON si.name = sii.parent
			WHERE
				si.docstatus = 1
				AND si.is_return = 1
				AND IFNULL(sii.so_detail, '') != ''
				AND sii.so_detail IN (
					SELECT name
					FROM `tabSales Order Item`
					WHERE parent = %(sales_order)s
				)
				AND NOT EXISTS (
					SELECT 1
					FROM `tabDelivery Note` linked_dn
					WHERE
						linked_dn.name = sii.delivery_note
						AND linked_dn.docstatus = 1
						AND linked_dn.is_return = 1
				)
				AND NOT EXISTS (
					SELECT 1
					FROM `tabDelivery Note Item` linked_dni
					INNER JOIN `tabDelivery Note` linked_dn
						ON linked_dn.name = linked_dni.parent
					WHERE
						linked_dni.name = sii.dn_detail
						AND linked_dn.docstatus = 1
						AND linked_dn.is_return = 1
				)
		) returns
		GROUP BY returns.so_detail
		""",
		{"sales_order": sales_order_name},
		as_dict=True,
	)

	return {
		row.so_detail: flt(row.returned_qty)
		for row in rows
	}


def _enqueue_stock_update(item_code: str):
	"""Queue one deduplicated WooCommerce stock update per Item."""
	frappe.enqueue(
		"woocommerce_fusion.tasks.stock_update."
		"update_stock_levels_on_woocommerce_site",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"woocommerce-stock-sync::{item_code}",
		deduplicate=True,
		item_code=item_code,
	)

def _enqueue_refund(sales_order_name: str):
	frappe.enqueue(
		"woocommerce_fusion.tasks.order_enhancements.sync_sales_order_refund",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"woocommerce-refund::{sales_order_name}",
		deduplicate=True,
		sales_order_name=sales_order_name,
	)


@frappe.whitelist()
def sync_sales_order_refund(sales_order_name: str):
	"""
	Create the pending WooCommerce refund for one ERPNext Sales Order.

	A Redis lock serializes concurrent events for the same Sales Order. The actual
	refund is also protected by a deterministic metadata key stored in WooCommerce.
	"""
	lock_name = f"woocommerce_fusion:refund:{sales_order_name}"

	with frappe.cache.lock(lock_name, timeout=900):
		sales_order = frappe.get_doc("Sales Order", sales_order_name)
		return _sync_sales_order_refund(sales_order)


def _sync_sales_order_refund(sales_order):
	if (
		sales_order.docstatus != 1
		or not sales_order.woocommerce_id
		or not sales_order.woocommerce_server
	):
		return None

	wc_server = frappe.get_cached_doc(
		"WooCommerce Server",
		sales_order.woocommerce_server,
	)
	if not wc_server.enable_sync:
		return None

	target_quantities = _get_target_refund_quantities(
		sales_order=sales_order,
		wc_server=wc_server,
	)
	quantity_source = _get_refund_quantity_source_label(wc_server)

	wc_api = _get_woocommerce_order_api(
		sales_order.woocommerce_server
	)
	order_id = int(sales_order.woocommerce_id)

	order_response = wc_api.api.get(f"orders/{order_id}")
	if order_response.status_code != 200:
		log_and_raise_error(
			error_text="get WooCommerce order before refund failed",
			response=order_response,
		)

	remote_order = order_response.json()
	line_items = remote_order.get("line_items", [])
	precision = sales_order.precision("grand_total") or 2
	used_line_ids: set[int] = set()
	pending_rows = []

	for so_item in sales_order.items:
		target_qty = flt(target_quantities.get(so_item.name))
		if target_qty <= 0:
			continue

		target_amount = max(
			flt(so_item.net_amount, precision)
			- flt(so_item.billed_amt, precision),
			0,
		)
		synced_qty = flt(so_item.get(REFUNDED_QTY_FIELD))
		synced_amount = flt(
			so_item.get(REFUNDED_AMOUNT_FIELD),
			precision,
		)

		if target_qty < synced_qty or target_amount < synced_amount:
			frappe.log_error(
				title=(
					"WooCommerce Refund State Decreased: "
					f"{sales_order.name} / row {so_item.idx}"
				),
				message=(
					f"Current target quantity={target_qty}; "
					f"quantity source={quantity_source}; "
					f"already synchronized quantity={synced_qty}; "
					f"current refund amount={target_amount}; "
					f"already synchronized amount={synced_amount}. "
					"A WooCommerce refund cannot be reversed automatically."
				),
			)
			continue

		delta_qty = flt(target_qty - synced_qty)
		delta_amount = flt(
			target_amount - synced_amount,
			precision,
		)

		if delta_qty <= 0 and delta_amount <= 0:
			continue

		if delta_qty > 0 and abs(delta_qty - round(delta_qty)) > 0.000001:
			frappe.throw(
				_(
					"WooCommerce requires an integer refund quantity. "
					"Sales Order {0}, row {1}, pending quantity: {2}."
				).format(
					sales_order.name,
					so_item.idx,
					delta_qty,
				)
			)

		remote_line = _match_woocommerce_order_line(
			so_item=so_item,
			line_items=line_items,
			woocommerce_server=sales_order.woocommerce_server,
			used_line_ids=used_line_ids,
		)
		remote_line_id = int(remote_line["id"])
		used_line_ids.add(remote_line_id)

		pending_rows.append(
			{
				"so_item": so_item,
				"line_id": remote_line_id,
				"target_qty": target_qty,
				"target_amount": target_amount,
				"delta_qty": int(round(delta_qty)),
				"delta_amount": delta_amount,
			}
		)

	if not pending_rows:
		return None

	sync_key = _build_refund_sync_key(
		sales_order=sales_order,
		pending_rows=pending_rows,
	)

	existing_refund = _find_refund_by_sync_key(
		wc_api=wc_api,
		order_id=order_id,
		sync_key=sync_key,
	)

	if existing_refund:
		refund_id = existing_refund.get("id")
	else:
		refund_amount = flt(
			sum(row["delta_amount"] for row in pending_rows),
			precision,
		)

		payload = {
			"amount": f"{refund_amount:.{precision}f}",
			"reason": _(
				"ERPNext Sales Order {0} return synchronisation"
			).format(sales_order.name),
			"api_refund": False,
			"api_restock": False,
			"line_items": [
				{
					"id": row["line_id"],
					"quantity": row["delta_qty"],
					"refund_total": row["delta_amount"],
				}
				for row in pending_rows
			],
			"meta_data": [
				{
					"key": REFUND_SALES_ORDER_META,
					"value": sales_order.name,
				},
				{
					"key": REFUND_SYNC_KEY_META,
					"value": sync_key,
				},
			],
		}

		response = wc_api.api.post(
			f"orders/{order_id}/refunds",
			data=payload,
		)
		if response.status_code != 201:
			log_and_raise_error(
				error_text="create WooCommerce order refund failed",
				response=response,
			)

		refund_id = response.json().get("id")

	for row in pending_rows:
		frappe.db.set_value(
			"Sales Order Item",
			row["so_item"].name,
			{
				ORDER_LINE_ID_FIELD: cstr(row["line_id"]),
				REFUNDED_QTY_FIELD: row["target_qty"],
				REFUNDED_AMOUNT_FIELD: row["target_amount"],
				LAST_REFUND_ID_FIELD: cstr(refund_id),
			},
			update_modified=False,
		)

	return {
		"sales_order": sales_order.name,
		"woocommerce_order_id": order_id,
		"woocommerce_refund_id": refund_id,
		"items": len(pending_rows),
		"quantity_source": quantity_source,
	}


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


def _get_woocommerce_refunds(wc_api, order_id: int) -> list[dict]:
	refunds = []
	page = 1

	while True:
		response = wc_api.api.get(
			f"orders/{order_id}/refunds",
			params={"per_page": 100, "page": page},
		)
		if response.status_code != 200:
			log_and_raise_error(
				error_text="get WooCommerce order refunds failed",
				response=response,
			)

		page_results = response.json()
		if not isinstance(page_results, list):
			frappe.throw(
				_("Unexpected WooCommerce refund response for order {0}.").format(
					order_id
				)
			)

		refunds.extend(page_results)

		if len(page_results) < 100:
			break

		page += 1

	return refunds


def _find_refund_by_sync_key(wc_api, order_id: int, sync_key: str):
	for refund in _get_woocommerce_refunds(wc_api, order_id):
		for metadata in refund.get("meta_data", []):
			if (
				metadata.get("key") == REFUND_SYNC_KEY_META
				and cstr(metadata.get("value")) == sync_key
			):
				return refund

	return None


def _build_refund_sync_key(sales_order, pending_rows) -> str:
	sync_state = "|".join(
		[
			f"{row['so_item'].name}:"
			f"{row['target_qty']}:"
			f"{row['target_amount']}"
			for row in sorted(
				pending_rows,
				key=lambda value: value["so_item"].name,
			)
		]
	)

	return hashlib.sha256(
		(
			f"{sales_order.woocommerce_server}|"
			f"{sales_order.woocommerce_id}|"
			f"{sales_order.name}|"
			f"{sync_state}"
		).encode("utf-8")
	).hexdigest()


def _match_woocommerce_order_line(
	so_item,
	line_items: list[dict],
	woocommerce_server: str,
	used_line_ids: set[int],
):
	stored_line_id = cstr(so_item.get(ORDER_LINE_ID_FIELD))

	if stored_line_id:
		stored_match = next(
			(
				line
				for line in line_items
				if cstr(line.get("id")) == stored_line_id
			),
			None,
		)
		if stored_match:
			return stored_match

	woocommerce_product_id = frappe.db.get_value(
		"Item WooCommerce Server",
		{
			"parent": so_item.item_code,
			"woocommerce_server": woocommerce_server,
		},
		"woocommerce_id",
	)

	if not woocommerce_product_id:
		frappe.throw(
			_(
				"Item {0} has no WooCommerce ID for server {1}."
			).format(
				so_item.item_code,
				woocommerce_server,
			)
		)

	product_matches = [
		line
		for line in line_items
		if cstr(
			line.get("variation_id") or line.get("product_id")
		)
		== cstr(woocommerce_product_id)
		and int(line.get("id")) not in used_line_ids
	]

	if len(product_matches) == 1:
		return product_matches[0]

	if 0 < so_item.idx <= len(line_items):
		index_match = line_items[so_item.idx - 1]
		if (
			cstr(
				index_match.get("variation_id")
				or index_match.get("product_id")
			)
			== cstr(woocommerce_product_id)
			and int(index_match.get("id")) not in used_line_ids
		):
			return index_match

	frappe.throw(
		_(
			"Could not uniquely match Sales Order Item row {0} "
			"to a WooCommerce order line."
		).format(so_item.idx)
	)

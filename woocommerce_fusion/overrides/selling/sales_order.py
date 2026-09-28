import json

import frappe
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import get_default_naming_series, make_autoname

from woocommerce_fusion.tasks.sync_sales_orders import run_sales_order_sync
from woocommerce_fusion.woocommerce.woocommerce_api import (
    generate_woocommerce_record_name_from_domain_and_id,
)
from frappe.utils import add_days, cint, cstr, flt, get_link_to_form, getdate, nowdate, strip_html

from woocommerce_fusion.tasks.order_status_sync import (
    sales_order_status_sync_required,
    sync_sales_order_status_and_reconciliation,
)


class CustomSalesOrder(SalesOrder):
    """
    This class extends ERPNext's Sales Order doctype to override the autoname method
    This allows us to name the Sales Order conditionally.

    We also add logic to set the WooCommerce Status field on validate.
    """

    def autoname(self):
        """
        If this is a WooCommerce-linked order, use the naming series defined in "WooCommerce Server"
        or default to WEB[WooCommerce Order ID], e.g. WEB012142.
        Else, name it normally.
        """
        if self.woocommerce_id and self.woocommerce_server:
            wc_server = frappe.get_cached_doc("WooCommerce Server", self.woocommerce_server)
            if wc_server.sales_order_series:
                self.name = make_autoname(key=wc_server.sales_order_series)
            else:
                if wc_server.custom_naming_series_prefix:
                    self.name = f"{wc_server.custom_naming_series_prefix}-{int(self.woocommerce_id):06}"
                else:
                    # Get idx of site
                    wc_servers = frappe.get_all("WooCommerce Server", fields=["name", "creation"])
                    sorted_list = sorted(wc_servers, key=lambda server: server.creation)
                    idx = next(
                        (index for (index, d) in enumerate(sorted_list) if d["name"] == self.woocommerce_server),
                        None,
                    )
                    self.name = f"WEB{idx + 1}-{int(self.woocommerce_id):06}"  # Format with leading zeros to make it 6 digits
        else:
            naming_series = get_default_naming_series("Sales Order")
            self.name = make_autoname(key=naming_series)

    def on_change(self):
        """
        Handle ERPNext -> WooCommerce Sales Order status synchronization.

        Important:
        woocommerce_status represents the LAST CONFIRMED WooCommerce
        status. Never write the intended status before WooCommerce
        confirms it.
        """
        if (
            self.flags.get("created_by_sync")
            or not self.woocommerce_id
            or not self.woocommerce_server
        ):
            return

        wc_server = frappe.get_cached_doc(
            "WooCommerce Server",
            self.woocommerce_server,
        )

        if not wc_server.enable_sync:
            return

        status_required = sales_order_status_sync_required(self)

        if not status_required:
            return

        frappe.enqueue(
            sync_sales_order_status_and_reconciliation,
            queue="long",
            enqueue_after_commit=True,
            sales_order_name=self.name,
            run_reconciliation_after=(self.docstatus == 1),
        )

        # Used by the doc_event handler below so it does not queue
        # a competing reconciliation job for this same change.
        self.flags.woocommerce_status_sync_queued = True

    def _get_return_adjusted_progress(self):
        """
        Calculate Sales Order progress after treating returned_qty as a
        permanent reduction of the quantity/amount that still needs to be
        delivered and billed.

        This keeps ERPNext's standard status engine. We only correct the
        progress values used by that engine when the Sales Order contains
        returned quantities.
        """

        has_returns = False

        total_original_qty = 0.0
        total_effective_qty = 0.0
        total_delivered_qty = 0.0

        total_original_amount = 0.0
        total_effective_amount = 0.0
        total_billed_amount = 0.0

        for item in self.items:
            qty = abs(
                flt(
                    item.qty,
                    item.precision("qty"),
                )
            )

            returned_qty = abs(
                flt(
                    item.returned_qty,
                    item.precision("returned_qty"),
                )
            )

            delivered_qty = abs(
                flt(
                    item.delivered_qty,
                    item.precision("delivered_qty"),
                )
            )

            amount = abs(
                flt(
                    item.amount,
                    item.precision("amount"),
                )
            )

            billed_amt = abs(
                flt(
                    item.billed_amt,
                    item.precision("billed_amt"),
                )
            )

            if returned_qty > 0:
                has_returns = True

            accepted_return_qty = min(
                returned_qty,
                qty,
            )

            effective_qty = max(
                qty - accepted_return_qty,
                0.0,
            )

            total_original_qty += qty
            total_effective_qty += effective_qty

            total_delivered_qty += min(
                delivered_qty,
                effective_qty,
            )

            total_original_amount += amount

            if qty > 0:
                effective_amount = (
                    amount
                    * effective_qty
                    / qty
                )
            else:
                effective_amount = amount

            total_effective_amount += effective_amount

            total_billed_amount += min(
                billed_amt,
                effective_amount,
            )

        if not has_returns:
            return None

        if total_effective_qty > 0:
            per_delivered = (
                total_delivered_qty
                / total_effective_qty
                * 100
            )
        elif total_original_qty > 0:
            per_delivered = 100.0
        else:
            per_delivered = flt(
                self.per_delivered
            )

        if total_effective_amount > 0:
            per_billed = (
                total_billed_amount
                / total_effective_amount
                * 100
            )
        elif total_original_amount > 0:
            per_billed = 100.0
        else:
            per_billed = flt(
                self.per_billed
            )

        return {
            "per_delivered": min(
                max(
                    round(
                        per_delivered,
                        6,
                    ),
                    0.0,
                ),
                100.0,
            ),
            "per_billed": min(
                max(
                    round(
                        per_billed,
                        6,
                    ),
                    0.0,
                ),
                100.0,
            ),
        }

    def set_status(
        self,
        update=False,
        status=None,
        update_modified=True,
    ):
        """
        Preserve ERPNext's standard Sales Order status calculation while
        making per_delivered and per_billed return-aware.

        Orders without returned_qty use ERPNext behavior unchanged.
        Draft and Cancelled documents use ERPNext behavior unchanged.
        ERPNext's standard set_status() still determines the actual status.
        """

        progress = None

        if self.docstatus == 1:
            progress = (
                self._get_return_adjusted_progress()
            )

        if progress:
            per_delivered = progress[
                "per_delivered"
            ]

            per_billed = progress[
                "per_billed"
            ]

            if self.skip_delivery_note:
                delivery_status = (
                    self.delivery_status
                    or "Not Applicable"
                )
            elif per_delivered < 0.001:
                delivery_status = (
                    "Not Delivered"
                )
            elif per_delivered >= 99.999999:
                delivery_status = (
                    "Fully Delivered"
                )
            else:
                delivery_status = (
                    "Partly Delivered"
                )

            if per_billed < 0.001:
                billing_status = (
                    "Not Billed"
                )
            elif per_billed >= 99.999999:
                billing_status = (
                    "Fully Billed"
                )
            else:
                billing_status = (
                    "Partly Billed"
                )

            self.per_delivered = (
                per_delivered
            )
            self.per_billed = (
                per_billed
            )
            self.delivery_status = (
                delivery_status
            )
            self.billing_status = (
                billing_status
            )

            if (
                update
                and not self.is_new()
            ):
                frappe.db.set_value(
                    "Sales Order",
                    self.name,
                    {
                        "per_delivered": (
                            per_delivered
                        ),
                        "per_billed": (
                            per_billed
                        ),
                        "delivery_status": (
                            delivery_status
                        ),
                        "billing_status": (
                            billing_status
                        ),
                    },
                    update_modified=False,
                )

        return super().set_status(
            update=update,
            status=status,
            update_modified=(
                update_modified
            ),
        )


@frappe.whitelist()
def get_woocommerce_order_shipment_trackings(doc: str):
	"""
	Fetches shipment tracking details from a WooCommerce order.
	"""
	doc = frappe._dict(json.loads(doc))
	if doc.woocommerce_server and doc.woocommerce_id:
		wc_order = get_woocommerce_order(doc.woocommerce_server, doc.woocommerce_id)
		if wc_order.shipment_trackings:
			return json.loads(wc_order.shipment_trackings)

	return []


@frappe.whitelist()
def update_woocommerce_order_shipment_trackings(doc: str, shipment_trackings: list):
	"""
	Updates the shipment tracking details of a specific WooCommerce order.
	"""
	doc = frappe._dict(json.loads(doc))
	if doc.woocommerce_server and doc.woocommerce_id:
		wc_order = get_woocommerce_order(doc.woocommerce_server, doc.woocommerce_id)
	wc_order.shipment_trackings = json.dumps(shipment_trackings)
	wc_order.save()
	return wc_order.shipment_trackings


def get_woocommerce_order(woocommerce_server, woocommerce_id):
	"""
	Retrieves a specific WooCommerce order based on its site and ID.
	"""
	# First verify if the WooCommerce site exits, and it sync is enabled
	wc_order_name = generate_woocommerce_record_name_from_domain_and_id(woocommerce_server, woocommerce_id)
	wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)

	if not wc_server:
		frappe.throw(
			_(
				"This Sales Order is linked to WooCommerce site '{0}', but this site can not be found in 'WooCommerce Servers'"
			).format(woocommerce_server)
		)

	if not wc_server.enable_sync:
		frappe.throw(
			_(
				"This Sales Order is linked to WooCommerce site '{0}', but Synchronisation for this site is disabled in 'WooCommerce Server'"
			).format(woocommerce_server)
		)

	wc_order = frappe.get_doc({"doctype": "WooCommerce Order", "name": wc_order_name})
	wc_order.load_from_db()
	return wc_order


@frappe.whitelist()
def make_delivery_note(source_name, target_doc=None, kwargs=None):
	from erpnext.stock.doctype.packed_item.packed_item import make_packing_list
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_sre_details_for_voucher,
		get_sre_reserved_qty_details_for_voucher,
		get_ssb_bundle_for_voucher,
	)
	from frappe.contacts.doctype.address.address import get_company_address
	from frappe.model.utils import get_fetch_values
	from erpnext.stock.doctype.item.item import get_item_defaults
	from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
	from frappe.model.mapper import get_mapped_doc

	if not kwargs:
		kwargs = {
			"for_reserved_stock": frappe.flags.args and frappe.flags.args.for_reserved_stock,
			"skip_item_mapping": frappe.flags.args and frappe.flags.args.skip_item_mapping,
		}

	kwargs = frappe._dict(kwargs)

	sre_details = {}
	if kwargs.for_reserved_stock:
		sre_details = get_sre_reserved_qty_details_for_voucher("Sales Order", source_name)

	mapper = {
		"Sales Order": {"doctype": "Delivery Note", "validation": {"docstatus": ["=", 1]}},
		"Sales Taxes and Charges": {"doctype": "Sales Taxes and Charges", "reset_value": True},
		"Sales Team": {"doctype": "Sales Team", "add_if_empty": True},
	}

	# 0 qty is accepted, as the qty is uncertain for some items
	has_unit_price_items = frappe.db.get_value("Sales Order", source_name, "has_unit_price_items")

	def is_unit_price_row(source):
		return has_unit_price_items and source.qty == 0

	def select_item(d):
		filtered_items = kwargs.get("filtered_children", [])
		child_filter = d.name in filtered_items if filtered_items else True
		return child_filter

	def set_missing_values(source, target):
		if kwargs.get("ignore_pricing_rule"):
			# Skip pricing rule when the dn is creating from the pick list
			target.ignore_pricing_rule = 1

		target.run_method("set_missing_values")
		target.run_method("set_po_nos")
		target.run_method("calculate_taxes_and_totals")
		target.run_method("set_use_serial_batch_fields")

		if source.company_address:
			target.update({"company_address": source.company_address})
		else:
			# set company address
			target.update(get_company_address(target.company))

		if target.company_address:
			target.update(get_fetch_values("Delivery Note", "company_address", target.company_address))

		# if invoked in bulk creation, validations are ignored and thus this method is nerver invoked
		if frappe.flags.bulk_transaction:
			# set target items names to ensure proper linking with packed_items
			target.set_new_name()

		make_packing_list(target)

	def condition(doc):
		if doc.name in sre_details:
			del sre_details[doc.name]
			return False

		# make_mapped_doc sets js `args` into `frappe.flags.args`
		if frappe.flags.args and frappe.flags.args.delivery_dates:
			if cstr(doc.delivery_date) not in frappe.flags.args.delivery_dates:
				return False

		return (
			((abs(doc.delivered_qty) + abs(doc.delivered_qty)) < abs(doc.qty)) or is_unit_price_row(doc)
		) and doc.delivered_by_supplier != 1

	def update_item(source, target, source_parent):
		qty = flt(source.qty)
		net_qty = min(qty, max(qty - flt(source.returned_qty), flt(source.delivered_qty)))

		#target.base_amount = (flt(source.qty) - flt(source.delivered_qty)) * flt(source.base_rate)
		#target.amount = (flt(source.qty) - flt(source.delivered_qty)) * flt(source.rate)
		#target.qty = (
		#	flt(source.qty) if is_unit_price_row(source) else flt(source.qty) - flt(source.delivered_qty)
		#)

		target.base_amount = net_qty * flt(source.base_rate)
		target.amount = net_qty * flt(source.rate)
		target.qty = (
			flt(source.qty) if is_unit_price_row(source) else net_qty
		)

		item = get_item_defaults(target.item_code, source_parent.company)
		item_group = get_item_group_defaults(target.item_code, source_parent.company)

		if item:
			target.cost_center = (
				frappe.db.get_value("Project", source_parent.project, "cost_center")
				or item.get("buying_cost_center")
				or item_group.get("buying_cost_center")
			)

	if not kwargs.skip_item_mapping:
		mapper["Sales Order Item"] = {
			"doctype": "Delivery Note Item",
			"field_map": {
				"rate": "rate",
				"name": "so_detail",
				"parent": "against_sales_order",
			},
			"condition": lambda d: condition(d) and select_item(d),
			"postprocess": update_item,
		}

	so = frappe.get_doc("Sales Order", source_name)
	target_doc = get_mapped_doc("Sales Order", so.name, mapper, target_doc)

	if not kwargs.skip_item_mapping and kwargs.for_reserved_stock:
		sre_list = get_sre_details_for_voucher("Sales Order", source_name)

		if sre_list:

			def update_dn_item(source, target, source_parent):
				update_item(source, target, so)

			so_items = {d.name: d for d in so.items if d.stock_reserved_qty}

			for sre in sre_list:
				if not condition(so_items[sre.voucher_detail_no]):
					continue

				dn_item = get_mapped_doc(
					"Sales Order Item",
					sre.voucher_detail_no,
					{
						"Sales Order Item": {
							"doctype": "Delivery Note Item",
							"field_map": {
								"rate": "rate",
								"name": "so_detail",
								"parent": "against_sales_order",
							},
							"postprocess": update_dn_item,
						}
					},
					ignore_permissions=True,
				)

				dn_item.qty = flt(sre.reserved_qty) / flt(dn_item.get("conversion_factor", 1))

				if sre.reservation_based_on == "Serial and Batch" and (sre.has_serial_no or sre.has_batch_no):
					dn_item.serial_and_batch_bundle = get_ssb_bundle_for_voucher(sre)

				target_doc.append("items", dn_item)
			else:
				# Correct rows index.
				for idx, item in enumerate(target_doc.items):
					item.idx = idx + 1

	# Should be called after mapping items.
	set_missing_values(so, target_doc)

	return target_doc
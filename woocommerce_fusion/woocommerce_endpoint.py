import base64
import hashlib
import hmac
import json
from http import HTTPStatus

import frappe
from frappe import _
from werkzeug.wrappers import Response

from woocommerce_fusion.tasks.sync_sales_orders import run_sales_order_sync
from woocommerce_fusion.woocommerce.woocommerce_api import (
	WC_RESOURCE_DELIMITER,
	parse_domain_from_url,
)


def validate_request() -> tuple[bool, HTTPStatus | None, str | None]:
	# Get relevant WooCommerce Server
	webhook_source_url = frappe.get_request_header("x-wc-webhook-source", "")
	if not webhook_source_url:
		return False, HTTPStatus.BAD_REQUEST, _("Missing Header: x-wc-webhook-source")

	try:
		wc_server = frappe.get_doc("WooCommerce Server", parse_domain_from_url(webhook_source_url))
	except Exception:
		# parse_domain_from_url may raise if header is malformed
		frappe.log_error(f"Invalid webhook source header: {webhook_source_url}", "WooCommerce Webhook")
		return False, HTTPStatus.BAD_REQUEST, _("Invalid webhook source")

	# Validate secret / signature
	received_sig = frappe.get_request_header("x-wc-webhook-signature", "")
	if not received_sig:
		return False, HTTPStatus.BAD_REQUEST, _("Missing Header: x-wc-webhook-signature")

	# request.data is usually bytes; ensure we use bytes for HMAC
	body = frappe.request.data or b""
	if isinstance(body, str):
		body = body.encode("utf8")

	expected_sig = base64.b64encode(
		hmac.new(wc_server.secret.encode("utf8"), body, hashlib.sha256).digest()
	).decode("utf8")

	# Use compare_digest for timing-attack-resistant comparison
	if not hmac.compare_digest(expected_sig, received_sig.strip()):
		# Log details for debugging (do NOT log secrets in production)
		frappe.log_error(
			f"WooCommerce webhook signature mismatch. expected={expected_sig} received={received_sig}",
			"WooCommerce Webhook Signature"
		)
		return False, HTTPStatus.UNAUTHORIZED, _("Unauthorized")

	frappe.set_user(wc_server.creation_user)  # nosemgrep
	return True, None, None


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def order_created(*args, **kwargs):
	"""
	Accepts payload data from WooCommerce "Order Created" webhook
	"""
	valid, status, msg = validate_request()
	if not valid:
		return Response(response=msg, status=status)

	if frappe.request and frappe.request.data:
		try:
			order = json.loads(frappe.request.data)
		except ValueError:
			# woocommerce returns 'webhook_id=value' for the first request which is not JSON
			order = frappe.request.data
		event = frappe.get_request_header("x-wc-webhook-event")
	else:
		return Response(response=_("Missing Header"), status=HTTPStatus.BAD_REQUEST)

	if event == "created":
		webhook_source_url = frappe.get_request_header("x-wc-webhook-source", "")
		woocommerce_order_name = (
			f"{parse_domain_from_url(webhook_source_url)}{WC_RESOURCE_DELIMITER}{order['id']}"
		)
		frappe.enqueue(run_sales_order_sync, queue="long", woocommerce_order_name=woocommerce_order_name)
		return Response(status=HTTPStatus.OK)
	else:
		return Response(response=_("Event not supported"), status=HTTPStatus.BAD_REQUEST)
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import time

import frappe
from frappe.utils import now_datetime

from woocommerce_fusion.sarea_wallet.api import SareaWalletAPI, SareaWalletAPIError


ACTIVE_REMOTE_STATUSES = {"pending", "processing"}
CUSTOMER_WORDPRESS_USER_ID_FIELD = "custom_sarea_wordpress_user_id"


def _money(value):
    if value in (None, ""):
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _get_customer_by_sarea_user_id(sarea_user_id):
    """
    Match Sarea Wallet user_id with:
        Customer.custom_sarea_wordpress_user_id

    Return the Customer name only when exactly one Customer matches.
    If there is no match or more than one match, return an empty value.
    """
    if sarea_user_id in (None, ""):
        return ""

    customer_field = frappe.get_meta("Customer").get_field(
        CUSTOMER_WORDPRESS_USER_ID_FIELD
    )

    if not customer_field:
        frappe.throw(
            f"Customer field {CUSTOMER_WORDPRESS_USER_ID_FIELD} does not exist."
        )

    if customer_field.fieldtype in {"Int", "Check"}:
        filter_value = int(sarea_user_id)
    else:
        filter_value = str(sarea_user_id).strip()

    customers = frappe.get_all(
        "Customer",
        filters={
            CUSTOMER_WORDPRESS_USER_ID_FIELD: filter_value,
        },
        pluck="name",
        limit_page_length=2,
    )

    if len(customers) == 1:
        return customers[0]

    # Do not guess when there is no match or duplicate mappings.
    return ""


def _clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _get_received_payment_method(row):
    """
    Support both the new API key `payment_method` and the existing/legacy
    key `method` currently used by Sarea Wallet responses.
    """
    if "payment_method" in row and row.get("payment_method") not in (None, ""):
        return _clean_text(row.get("payment_method"))

    return _clean_text(row.get("method"))


def _get_received_currency(row):
    """
    Wallet movements are currently USD, but use a received currency when the
    API provides it. USD remains the backward-compatible fallback.
    """
    return _clean_text(row.get("currency")) or "USD"


def _get_mode_of_payment_from_mapping(payment_method, currency):
    """
    Match:
        Woo Payment Method + Currency
    from Sarea Wallet Settings > Payment Methods Mapping.

    Matching is normalized for surrounding spaces/case only.
    """
    payment_method_key = _clean_text(payment_method).casefold()
    currency_key = _clean_text(currency).upper()

    if not payment_method_key or not currency_key:
        return ""

    settings = frappe.get_cached_doc("Sarea Wallet Settings")

    matches = []

    for row in settings.get("payment_method_mappings") or []:
        configured_method = _clean_text(row.woo_payment_method).casefold()
        configured_currency = _clean_text(row.currency).upper()

        if (
            configured_method == payment_method_key
            and configured_currency == currency_key
            and row.mode_of_payment
        ):
            matches.append(row.mode_of_payment)

    # Settings validation prevents duplicates, but remain defensive here.
    unique_matches = list(dict.fromkeys(matches))

    return unique_matches[0] if len(unique_matches) == 1 else ""


def upsert_charge_request(row):
    remote_id = int(row["id"])

    name = frappe.db.get_value(
        "Sarea Wallet Charge Request",
        {"charge_request_id": remote_id},
        "name",
    )

    sarea_user_id = row.get("user_id")
    payment_method = _get_received_payment_method(row)
    currency = _get_received_currency(row)

    original_currency = _clean_text(
        row.get("original_currency")
    )

    mapping_currency = (
        original_currency
        or currency
    )

    mapped_mode_of_payment = (
        _get_mode_of_payment_from_mapping(
            payment_method,
            mapping_currency,
        )
    )

    values = {
        "charge_request_id": remote_id,
        "sarea_user_id": sarea_user_id,
        "customer": _get_customer_by_sarea_user_id(
            sarea_user_id
        ),
        "amount_usd": _money(
            row.get("amount")
        ),
        "currency": currency,
        "payment_method": payment_method,
        "reference": row.get("reference"),
        "status": row.get("status"),
        "blog_id": row.get("blog_id"),
        "sarea_created_at": row.get(
            "created_at"
        ),
        "last_sync_on": now_datetime(),
    }

    # These fields are optional in the API. Only update ERPNext when the
    # response actually contains them, so an older/list response cannot erase
    # richer data previously received from the detail endpoint.
    if "original_currency" in row:
        values["original_currency"] = (
            original_currency
        )

    if "original_amount" in row:
        original_amount = _money(
            row.get("original_amount")
        )
        if original_amount is not None:
            values["original_amount"] = (
                original_amount
            )

    if "exchange_rate_snapshot" in row:
        exchange_rate_snapshot = _money(
            row.get("exchange_rate_snapshot")
        )
        if exchange_rate_snapshot is not None:
            values[
                "exchange_rate_snapshot"
            ] = exchange_rate_snapshot

    if "notes" in row:
        values["notes"] = (
            row.get("notes") or ""
        )

    if name:
        doc = frappe.get_doc(
            "Sarea Wallet Charge Request",
            name,
        )

        # Mapping supplies a default only. Preserve a user-edited Mode of
        # Payment during later scheduler/detail syncs.
        if (
            mapped_mode_of_payment
            and not doc.mode_of_payment
        ):
            values[
                "mode_of_payment"
            ] = mapped_mode_of_payment

        doc.update(values)
        doc.flags.from_sarea_wallet_sync = True
        doc.save(ignore_permissions=True)

    else:
        if mapped_mode_of_payment:
            values[
                "mode_of_payment"
            ] = mapped_mode_of_payment

        doc = frappe.get_doc(
            {
                "doctype": (
                    "Sarea Wallet Charge Request"
                ),
                **values,
            }
        )
        doc.flags.from_sarea_wallet_sync = True
        doc.insert(ignore_permissions=True)

    return doc.name


def _sync_status(api, status, per_page=100):
    page = 1
    seen = set()
    count = 0

    while True:
        _, payload, _ = api.list_charge_requests(
            page=page,
            per_page=per_page,
            status=status,
        )

        rows = (
            payload.get("data", [])
            if isinstance(payload, dict)
            else []
        )

        pagination = (
            payload.get("pagination", {})
            if isinstance(payload, dict)
            else {}
        )

        for row in rows:
            upsert_charge_request(row)
            seen.add(int(row["id"]))
            count += 1

        total_pages = int(
            pagination.get("total_pages") or 1
        )

        if page >= total_pages:
            break

        page += 1

    return seen, count


def _sync_charge_requests(status="active"):
    """
    Internal sync implementation.

    This is separated from the whitelisted Desk method so it can also be
    called safely by the scheduler without user-role checks.
    """
    api = SareaWalletAPI()

    settings = frappe.get_single(
        "Sarea Wallet Settings"
    )

    per_page = int(
        settings.sync_page_size or 100
    )

    statuses = (
        ["pending", "processing"]
        if status == "active"
        else [status]
    )

    seen_active = set()
    total = 0

    for remote_status in statuses:
        seen, count = _sync_status(
            api,
            remote_status,
            per_page,
        )

        total += count

        if remote_status in ACTIVE_REMOTE_STATUSES:
            seen_active.update(seen)

    if status == "active":
        local_active = frappe.get_all(
            "Sarea Wallet Charge Request",
            filters={
                "status": [
                    "in",
                    list(ACTIVE_REMOTE_STATUSES),
                ]
            },
            fields=[
                "name",
                "charge_request_id",
            ],
            limit_page_length=1000,
        )

        for row in local_active:
            remote_id = int(
                row.charge_request_id
            )

            if remote_id in seen_active:
                continue

            try:
                _, payload, _ = (
                    api.get_charge_request(
                        remote_id
                    )
                )

                if isinstance(payload, dict):
                    upsert_charge_request(
                        payload
                    )

            except SareaWalletAPIError as exc:
                if exc.status_code == 404:
                    frappe.db.set_value(
                        "Sarea Wallet Charge Request",
                        row.name,
                        {
                            "last_sync_on": now_datetime(),
                            "last_error": (
                                "Remote charge request was not found "
                                "during reconciliation."
                            ),
                        },
                        update_modified=False,
                    )
                else:
                    raise

    frappe.db.commit()

    return {
        "ok": True,
        "synced": total,
        "status": status,
    }


@frappe.whitelist()
def sync_charge_requests(status="active"):
    frappe.only_for(
        (
            "System Manager",
            "Accounts Manager",
        )
    )

    return _sync_charge_requests(
        status=status
    )


@frappe.whitelist()
def sync_one_charge_request(name):
    frappe.only_for(
        (
            "System Manager",
            "Accounts Manager",
        )
    )

    doc = frappe.get_doc(
        "Sarea Wallet Charge Request",
        name,
    )

    api = SareaWalletAPI()

    _, payload, _ = api.get_charge_request(
        int(doc.charge_request_id)
    )

    upsert_charge_request(payload)

    frappe.db.commit()

    return {
        "ok": True,
        "name": name,
    }


def _refresh_pending_charge_requests():
    """
    Refresh every locally synced charge request that is still marked pending.

    Unlike the list endpoint sync, this performs the same individual remote
    read as sync_one_charge_request():

        GET /charge-requests/{charge_request_id}

    This is important because a request that was pending when ERPNext last
    saw it may later become processed/rejected outside ERPNext and therefore
    disappear from GET /charge-requests?status=pending.

    The wallet API contract limits authenticated traffic to 60 requests per
    60 seconds. Individual reads are therefore spaced slightly apart so this
    job does not create an API burst.
    """
    pending_rows = frappe.get_all(
        "Sarea Wallet Charge Request",
        filters={"status": "pending"},
        fields=["name", "charge_request_id"],
        order_by="sarea_created_at asc, creation asc",
        limit_page_length=0,
    )

    if not pending_rows:
        return {
            "ok": True,
            "checked": 0,
            "updated": 0,
            "not_found": 0,
            "errors": 0,
        }

    api = SareaWalletAPI()

    checked = 0
    updated = 0
    not_found = 0
    errors = 0

    # ~54 requests/minute, leaving some headroom below the documented
    # 60 authenticated requests / 60 seconds key limit.
    delay_between_requests = 1.25

    for idx, row in enumerate(pending_rows):
        old_status = frappe.db.get_value(
            "Sarea Wallet Charge Request",
            row.name,
            "status",
        )

        try:
            _, payload, _ = api.get_charge_request(
                int(row.charge_request_id)
            )
            checked += 1

            if isinstance(payload, dict):
                upsert_charge_request(payload)

                new_status = payload.get("status")
                if new_status != old_status:
                    updated += 1

        except SareaWalletAPIError as exc:
            checked += 1

            if exc.status_code == 404:
                not_found += 1
                frappe.db.set_value(
                    "Sarea Wallet Charge Request",
                    row.name,
                    {
                        "last_sync_on": now_datetime(),
                        "last_error": (
                            "Remote charge request was not found "
                            "during pending refresh."
                        ),
                    },
                    update_modified=False,
                )
            else:
                errors += 1

                frappe.db.set_value(
                    "Sarea Wallet Charge Request",
                    row.name,
                    {
                        "last_sync_on": now_datetime(),
                        "last_error": str(exc),
                    },
                    update_modified=False,
                )

                # Stop immediately on rate limiting. Continuing would only
                # generate more 429 responses.
                if exc.status_code == 429:
                    break

        # Commit each processed row so a long scheduler run preserves
        # progress even if a later API request fails.
        frappe.db.commit()

        if idx < len(pending_rows) - 1:
            time.sleep(delay_between_requests)

    return {
        "ok": errors == 0,
        "checked": checked,
        "updated": updated,
        "not_found": not_found,
        "errors": errors,
    }


@frappe.whitelist()
def refresh_pending_charge_requests():
    """
    Desk/list-view action: individually refresh all local pending requests.
    """
    frappe.only_for(
        (
            "System Manager",
            "Accounts Manager",
        )
    )

    lock_name = (
        "woocommerce_fusion:"
        "sarea_wallet:"
        "charge_request_scheduler"
    )

    with frappe.cache.lock(
        lock_name,
        timeout=900,
    ):
        return _refresh_pending_charge_requests()


def scheduled_refresh_pending_charge_requests():
    """
    Scheduler entry point for individual Sync From Sarea refreshes.

    Recommended hooks.py cron:
        "*/5 * * * *"

    This does NOT discover new requests. It refreshes every request already
    synced into ERPNext whose local Sarea status is still pending.
    """
    settings = frappe.get_single(
        "Sarea Wallet Settings"
    )

    if not settings.enabled:
        return

    lock_name = (
        "woocommerce_fusion:"
        "sarea_wallet:"
        "charge_request_scheduler"
    )

    with frappe.cache.lock(
        lock_name,
        timeout=900,
    ):
        return _refresh_pending_charge_requests()


def scheduled_sync_pending_charge_requests():
    """
    Scheduler entry point.

    Intended hooks.py cron:
        "*/5 * * * *"

    Only pending charge requests are polled by this scheduled task.
    Manual "active" synchronization continues to include both pending and
    processing requests.
    """
    settings = frappe.get_single(
        "Sarea Wallet Settings"
    )

    if not settings.enabled:
        return

    lock_name = (
        "woocommerce_fusion:"
        "sarea_wallet:"
        "charge_request_scheduler"
    )

    # Prevent overlapping five-minute scheduler runs.
    with frappe.cache.lock(
        lock_name,
        timeout=300,
    ):
        return _sync_charge_requests(
            status="pending"
        )

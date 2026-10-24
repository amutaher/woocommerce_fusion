from __future__ import annotations

from decimal import Decimal, InvalidOperation

import frappe
from frappe.utils import getdate, nowdate

from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account
from erpnext.accounts.party import get_party_account


CHARGE_REQUEST_DTYPE = "Sarea Wallet Charge Request"
PAYMENT_ENTRY_LINK_FIELD = "custom_sarea_wallet_charge_request"

DEFAULT_REMARKS_TEMPLATE = (
    "Received payment for Sarea Wallet Charge Request {{ name }} "
    "(ID {{ charge_request_id }}) from {{ customer }}. "
    "Amount: {{ amount_usd }} {{ currency }}. "
    "Reference: {{ reference or '-' }}. Notes: {{ notes or '-' }}"
)


def _decimal(value, label):
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        frappe.throw(f"{label} is invalid.")

    if amount <= 0:
        frappe.throw(f"{label} must be greater than zero.")

    return amount


def _get_settings(require_company=True):
    settings = frappe.get_single("Sarea Wallet Settings")

    if require_company and not settings.company:
        frappe.throw(
            "Please set Company in Sarea Wallet Settings before creating "
            "a Payment Entry."
        )

    return settings


def _lock_charge_request(name):
    frappe.db.sql(
        """
        select name
        from `tabSarea Wallet Charge Request`
        where name=%s
        for update
        """,
        (name,),
    )

    return frappe.get_doc(CHARGE_REQUEST_DTYPE, name)


def _reference_no(doc):
    """
    User-requested reference priority:
      1. Sarea Wallet voucher_number
      2. Charge Request ID
    """
    voucher_number = str(doc.voucher_number or "").strip()

    if voucher_number:
        return voucher_number

    return str(int(doc.charge_request_id))


def _payment_date(doc):
    if doc.sarea_created_at:
        return getdate(doc.sarea_created_at)

    return getdate(nowdate())


def _expected_amounts(doc, company):
    """
    Current deployment rule:
      Company currency  = USD
      Customer currency = USD

      paid_amount     = Sarea wallet amount (amount_usd)
      received_amount = original_amount if present, otherwise amount_usd
    """
    paid_amount = _decimal(
        doc.amount_usd,
        "Charge Request Amount",
    )

    received_amount = (
        _decimal(doc.original_amount, "Original Amount")
        if doc.original_amount
        else paid_amount
    )

    source_currency = str(doc.currency or "USD").strip()
    target_currency = (
        str(doc.original_currency or "").strip()
        or source_currency
    )

    company_currency = frappe.get_cached_value(
        "Company",
        company,
        "default_currency",
    )

    return {
        "paid_amount": paid_amount,
        "received_amount": received_amount,
        "source_currency": source_currency,
        "target_currency": target_currency,
        "company_currency": company_currency,
    }


def _payment_entry_rows(charge_request_name):
    return frappe.get_all(
        "Payment Entry",
        filters={
            PAYMENT_ENTRY_LINK_FIELD: charge_request_name,
            "docstatus": ["<", 2],
        },
        fields=[
            "name",
            "docstatus",
            "received_amount",
        ],
        order_by="creation asc",
        limit_page_length=2,
    )


def _get_existing_payment_entry(doc):
    """
    The reverse Payment Entry -> Charge Request link is authoritative.

    Draft and Submitted Payment Entries are considered active.
    Cancelled Payment Entries are ignored, allowing a replacement.
    """
    rows = _payment_entry_rows(doc.name)

    if len(rows) > 1:
        frappe.throw(
            f"Duplicate non-cancelled Payment Entries already exist for "
            f"Charge Request {doc.name}. Resolve them before continuing."
        )

    return rows[0] if rows else None


def _set_payment_entry_created(docname, value):
    frappe.db.set_value(
        CHARGE_REQUEST_DTYPE,
        docname,
        "payment_entry_created",
        1 if value else 0,
        update_modified=False,
    )


def _get_accounts(doc, company, amounts):
    if not doc.customer:
        frappe.throw(
            "ERPNext Customer is required before creating a Payment Entry."
        )

    if not doc.mode_of_payment:
        frappe.throw(
            "Mode of Payment is required before creating a Payment Entry."
        )

    paid_from = get_party_account(
        "Customer",
        doc.customer,
        company,
    )

    if not paid_from:
        frappe.throw(
            f"No Receivable account was found for Customer {doc.customer} "
            f"in Company {company}."
        )

    bank = get_bank_cash_account(
        doc.mode_of_payment,
        company,
    )

    paid_to = (
        bank.get("account")
        if bank
        else None
    )

    if not paid_to:
        frappe.throw(
            f"Mode of Payment {doc.mode_of_payment} does not have a "
            f"default account for Company {company}."
        )

    paid_from_currency = (
        frappe.db.get_value(
            "Account",
            paid_from,
            "account_currency",
        )
        or amounts["company_currency"]
    )

    paid_to_currency = (
        frappe.db.get_value(
            "Account",
            paid_to,
            "account_currency",
        )
        or amounts["company_currency"]
    )

    if paid_from_currency != amounts["source_currency"]:
        frappe.throw(
            f"Customer Receivable account currency is "
            f"{paid_from_currency}, but the Charge Request currency is "
            f"{amounts['source_currency']}."
        )

    if paid_from_currency != amounts["company_currency"]:
        frappe.throw(
            "This feature currently expects the Customer Receivable "
            "account currency to equal the Company currency."
        )

    if paid_to_currency != amounts["target_currency"]:
        frappe.throw(
            f"Mode of Payment account currency is {paid_to_currency}, "
            f"but the received payment currency is "
            f"{amounts['target_currency']}."
        )

    return {
        "paid_from": paid_from,
        "paid_to": paid_to,
        "paid_from_currency": paid_from_currency,
        "paid_to_currency": paid_to_currency,
    }


def _exchange_rates(amounts, accounts):
    """
    ERPNext exchange rates convert account currency to Company currency.

    Source side is the USD Customer/Company account, therefore rate = 1.

    If the receiving account is foreign currency, derive target rate from the
    two authoritative amounts:
        company-base amount / foreign received amount

    This avoids assuming whether the API's Exchange Rate Snapshot is quoted
    as USD/foreign or foreign/USD.
    """
    source_exchange_rate = Decimal("1")

    if accounts["paid_to_currency"] == amounts["company_currency"]:
        target_exchange_rate = Decimal("1")

        if amounts["received_amount"] != amounts["paid_amount"]:
            frappe.throw(
                "Received Amount must equal Paid Amount when the mapped "
                "Mode of Payment account uses the Company currency."
            )
    else:
        target_exchange_rate = (
            amounts["paid_amount"]
            / amounts["received_amount"]
        )

    return {
        "source_exchange_rate": source_exchange_rate,
        "target_exchange_rate": target_exchange_rate,
    }


def _render_remarks(doc, settings):
    """
    Render Payment Entry remarks using Frappe/Jinja.

    Context supports:
      - direct Charge Request fields:
            {{ amount_usd }}
            {% if payment_method == "bank_transfer" %}...{% endif %}
      - the Charge Request document:
            {{ doc.amount_usd }}
            {% if doc.payment_method == "bank_transfer" %}...{% endif %}
    """
    template = (
        settings.payment_entry_remarks_template
        or DEFAULT_REMARKS_TEMPLATE
    )

    context = doc.as_dict()
    context["doc"] = doc

    try:
        return frappe.render_template(
            template,
            context,
        ).strip()
    except Exception as exc:
        frappe.throw(
            f"Invalid Payment Entry Remarks Template: {exc}"
        )


def _validate_payment_entry_matches(doc, payment_entry_name):
    pe = frappe.get_doc(
        "Payment Entry",
        payment_entry_name,
    )

    settings = _get_settings()
    amounts = _expected_amounts(
        doc,
        settings.company,
    )

    if pe.docstatus == 2:
        frappe.throw(
            f"Payment Entry {pe.name} is cancelled."
        )

    errors = []

    if pe.get(PAYMENT_ENTRY_LINK_FIELD) != doc.name:
        errors.append(
            f"Sarea Wallet Charge Request must be {doc.name}"
        )

    if pe.payment_type != "Receive":
        errors.append(
            "Payment Type must be Receive"
        )

    if pe.company != settings.company:
        errors.append(
            f"Company must be {settings.company}"
        )

    if pe.party_type != "Customer":
        errors.append(
            "Party Type must be Customer"
        )

    if pe.party != doc.customer:
        errors.append(
            f"Customer must be {doc.customer}"
        )

    if pe.mode_of_payment != doc.mode_of_payment:
        errors.append(
            f"Mode of Payment must be {doc.mode_of_payment}"
        )

    if (
        Decimal(str(pe.paid_amount or 0))
        != amounts["paid_amount"]
    ):
        errors.append(
            f"Paid Amount must be "
            f"{amounts['paid_amount']}"
        )

    if (
        Decimal(str(pe.received_amount or 0))
        != amounts["received_amount"]
    ):
        errors.append(
            f"Received Amount must be "
            f"{amounts['received_amount']}"
        )

    if errors:
        frappe.throw(
            f"Payment Entry {pe.name} does not match Charge Request "
            f"{doc.name}:<br>- "
            + "<br>- ".join(errors)
        )

    return pe


def validate_payment_entry_before_approval(doc):
    """
    Approval gate.

    When Require Payment Entry Before Approve is enabled, one matching
    non-cancelled Payment Entry must exist. It may be Draft or Submitted.
    """
    settings = _get_settings(
        require_company=False,
    )

    if not settings.require_payment_entry_before_approve:
        return

    if not settings.company:
        frappe.throw(
            "Company is required in Sarea Wallet Settings because "
            "Require Payment Entry Before Approve is enabled."
        )

    existing = _get_existing_payment_entry(doc)

    if not existing:
        _set_payment_entry_created(
            doc.name,
            False,
        )
        frappe.throw(
            "A Payment Entry must be created before approving this "
            "Sarea Wallet Charge Request."
        )

    _validate_payment_entry_matches(
        doc,
        existing.name,
    )

    _set_payment_entry_created(
        doc.name,
        True,
    )


@frappe.whitelist()
def get_payment_entry_status(charge_request):
    """
    Lightweight form-refresh helper.

    The reverse Payment Entry link remains authoritative; this only keeps the
    Charge Request display checkbox synchronized.
    """
    frappe.only_for(
        (
            "Accounts User",
            "Accounts Manager",
            "System Manager",
        )
    )

    doc = frappe.get_doc(
        CHARGE_REQUEST_DTYPE,
        charge_request,
    )

    existing = _get_existing_payment_entry(
        doc
    )

    _set_payment_entry_created(
        doc.name,
        bool(existing),
    )

    frappe.db.commit()

    return {
        "exists": bool(existing),
        "payment_entry": (
            existing.name
            if existing
            else None
        ),
        "docstatus": (
            existing.docstatus
            if existing
            else None
        ),
        "received_amount": (
            existing.received_amount
            if existing
            else None
        ),
    }


@frappe.whitelist()
def create_payment_entry(charge_request):
    frappe.only_for(
        (
            "Accounts Manager",
            "System Manager",
        )
    )

    # Serializes duplicate check and creation for this Charge Request.
    doc = _lock_charge_request(
        charge_request
    )

    if doc.status in {
        "rejected",
        "failed",
    }:
        frappe.throw(
            f"Cannot create a Payment Entry for a Charge Request with "
            f"Sarea status {doc.status}."
        )

    if doc.status == "processing":
        frappe.throw(
            "The Sarea Wallet request is currently processing. Refresh the "
            "request before creating a Payment Entry."
        )

    existing = _get_existing_payment_entry(
        doc
    )

    if existing:
        _validate_payment_entry_matches(
            doc,
            existing.name,
        )

        _set_payment_entry_created(
            doc.name,
            True,
        )

        frappe.db.commit()

        return {
            "payment_entry": existing.name,
            "created": False,
        }

    settings = _get_settings()

    amounts = _expected_amounts(
        doc,
        settings.company,
    )

    accounts = _get_accounts(
        doc,
        settings.company,
        amounts,
    )

    rates = _exchange_rates(
        amounts,
        accounts,
    )

    pe = frappe.new_doc(
        "Payment Entry"
    )

    pe.payment_type = "Receive"
    pe.company = settings.company
    pe.posting_date = _payment_date(doc)

    pe.party_type = "Customer"
    pe.party = doc.customer
    pe.mode_of_payment = doc.mode_of_payment

    pe.paid_from = accounts[
        "paid_from"
    ]
    pe.paid_to = accounts[
        "paid_to"
    ]

    pe.paid_from_account_currency = (
        accounts[
            "paid_from_currency"
        ]
    )
    pe.paid_to_account_currency = (
        accounts[
            "paid_to_currency"
        ]
    )

    pe.paid_amount = float(
        amounts["paid_amount"]
    )
    pe.received_amount = float(
        amounts["received_amount"]
    )

    pe.source_exchange_rate = float(
        rates["source_exchange_rate"]
    )
    pe.target_exchange_rate = float(
        rates["target_exchange_rate"]
    )

    pe.reference_no = _reference_no(doc)
    pe.reference_date = _payment_date(doc)

    pe.set(
        PAYMENT_ENTRY_LINK_FIELD,
        doc.name,
    )

    # Make the generated remarks explicit rather than ERPNext's default.
    pe.custom_remarks = 1
    pe.remarks = _render_remarks(
        doc,
        settings,
    )

    # Draft by design. User can review and submit from Payment Entry.
    pe.insert()

    _set_payment_entry_created(
        doc.name,
        True,
    )

    frappe.db.commit()

    return {
        "payment_entry": pe.name,
        "created": True,
    }

import frappe
from frappe.model.document import Document
from frappe.utils.jinja import get_jenv
from jinja2 import meta

from woocommerce_fusion.sarea_wallet.api import SareaWalletAPI,SareaWalletAPIError


class SareaWalletSettings(Document):
    def validate(self):
        if self.base_url:
            self.base_url=self.base_url.strip().rstrip("/")

        if self.timeout_seconds and int(self.timeout_seconds)<1:
            frappe.throw("Timeout Seconds must be at least 1.")

        if self.sync_page_size:
            self.sync_page_size=min(max(int(self.sync_page_size),1),100)

        self._validate_payment_method_mappings()

        if self.require_payment_entry_before_approve and not self.company:
            frappe.throw(
                "Company is required when Require Payment Entry Before "
                "Approve is enabled."
            )

        self._validate_payment_entry_remarks_template()

    def _validate_payment_method_mappings(self):
        seen=set()

        for row in self.get("payment_method_mappings") or []:
            row.woo_payment_method=(row.woo_payment_method or "").strip()
            row.currency=(row.currency or "").strip()

            key=(
                row.woo_payment_method.casefold(),
                row.currency.upper(),
            )

            if key in seen:
                frappe.throw(
                    "Duplicate Payment Methods Mapping for "
                    f"Woo Payment Method {row.woo_payment_method} "
                    f"and Currency {row.currency}."
                )

            seen.add(key)


    def _validate_payment_entry_remarks_template(self):
        """
        Validate the stored Jinja template before it can be used to create
        Payment Entries.

        Allowed top-level variables:
        - every field on Sarea Wallet Charge Request
        - name
        - doc (the complete Sarea Wallet Charge Request document)

        Frappe/Jinja globals and filters remain available through Frappe's
        own Jinja environment.
        """
        template=(self.payment_entry_remarks_template or "").strip()

        if not template:
            return

        allowed={"name","doc"}

        allowed.update(
            field.fieldname
            for field in frappe.get_meta(
                "Sarea Wallet Charge Request"
            ).fields
            if field.fieldname
        )

        try:
            env=get_jenv()
            parsed=env.parse(template)
            variables=meta.find_undeclared_variables(parsed)
        except Exception as exc:
            frappe.throw(
                f"Invalid Payment Entry Remarks Template: {exc}"
            )

        unknown=sorted(
            variable
            for variable in variables
            if variable not in allowed
        )

        if unknown:
            frappe.throw(
                "Unknown variable(s) in Payment Entry Remarks Template: "
                + ", ".join(unknown)
                + ". Use Sarea Wallet Charge Request fieldnames directly "
                "or access them through doc.<fieldname>."
            )


@frappe.whitelist()
def test_connection():
    frappe.only_for("System Manager")
    try:
        status,data,_=SareaWalletAPI().ping()
        return {"ok":True,"http_status":status,"response":data}
    except SareaWalletAPIError as exc:
        return {"ok":False,"http_status":exc.status_code,"code":exc.code,
                "response":exc.response_data,"message":str(exc)}

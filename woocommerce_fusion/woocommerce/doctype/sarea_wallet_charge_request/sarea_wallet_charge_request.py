from __future__ import annotations
import json,uuid
import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime
from woocommerce_fusion.sarea_wallet.api import SareaWalletAPI,SareaWalletAPIError
from woocommerce_fusion.sarea_wallet.sync import upsert_charge_request
from woocommerce_fusion.sarea_wallet.payment_entry import validate_payment_entry_before_approval

FINAL_REMOTE_STATUSES={"processed","rejected"}
RETRYABLE_CODES={"request_processing","charge_request_processing","network_error"}
RETRYABLE_HTTP={429,503}

class SareaWalletChargeRequest(Document): pass

def _dump(v): return json.dumps(v,ensure_ascii=False,separators=(",",":"),sort_keys=True)

def _persist_pre_send_state(doc,decision,notes):
    payload_json=_dump({"decision":decision,"notes":notes})
    key=uuid.uuid4().hex
    frappe.db.set_value("Sarea Wallet Charge Request",doc.name,{
        "decision":decision,"decision_notes":notes,"decision_payload":payload_json,
        "decision_idempotency_key":key,"decision_state":"Sending",
        "decision_by":frappe.session.user,"decision_started_at":now_datetime(),
        "last_error":"","last_response_code":"","retry_after":""
    },update_modified=True)
    frappe.db.commit()
    return key,payload_json

def _record_success(name,http_status,data):
    vals={
        "decision_state":"Completed",
        "last_http_status":http_status,
        "last_response_code":"",
        "last_response_json":_dump(data),
        "idempotent_replay":1 if data.get("idempotent_replay") else 0,
        "decision_completed_at":now_datetime(),
        "last_error":"",
        "retry_after":""
    }

    if data.get("status"):
        vals["status"]=data.get("status")

    if data.get("transaction_id") is not None:
        vals["transaction_id"]=data.get("transaction_id")

    if data.get("voucher_number") is not None:
        vals["voucher_number"]=data.get("voucher_number")

    if data.get("balance_after") is not None:
        vals["balance_after"]=data.get("balance_after")

    frappe.db.set_value(
        "Sarea Wallet Charge Request",
        name,
        vals,
        update_modified=True
    )
    frappe.db.commit()

def _record_error(name,exc):
    retryable=exc.code in RETRYABLE_CODES or exc.status_code in RETRYABLE_HTTP or exc.status_code is None
    state="Retry Required" if retryable else "Error"
    if exc.code in {"idempotency_conflict","charge_request_already_decided"}: state="Conflict"
    frappe.db.set_value("Sarea Wallet Charge Request",name,{
        "decision_state":state,"last_http_status":exc.status_code,
        "last_response_code":exc.code or "","last_response_json":_dump(exc.response_data or {}),
        "last_error":str(exc),"retry_after":exc.retry_after or ""
    },update_modified=True)
    frappe.db.commit()
    return state

def _reconcile_already_decided(doc,decision,api):
    _,remote,_=api.get_charge_request(int(doc.charge_request_id))
    upsert_charge_request(remote)
    st=(remote or {}).get("status")
    matches=(decision=="approve" and st=="processed") or (decision=="reject" and st=="rejected")
    if matches:
        frappe.db.set_value("Sarea Wallet Charge Request",doc.name,{
            "decision_state":"Completed","decision_completed_at":now_datetime(),"last_error":""
        },update_modified=True); frappe.db.commit()
        return {"ok":True,"reconciled":True,"status":st,
                "message":"Remote request was already decided with the same result and has been reconciled."}
    return {"ok":False,"conflict":True,"status":st,
            "message":"Remote request was already decided with a different result. ERPNext was refreshed from Sarea Wallet."}

@frappe.whitelist()
def decide_charge_request(name,decision,notes):
    frappe.only_for(("System Manager","Accounts Manager"))
    decision=(decision or "").strip().lower(); notes=(notes or "").strip()
    if decision not in {"approve","reject"}: frappe.throw("Decision must be approve or reject.")
    if not notes: frappe.throw("Decision notes/reason are required.")
    if len(notes)>4000: frappe.throw("Decision notes cannot exceed 4000 characters.")
    # Serialize decision creation for this charge request so two ERPNext
    # users cannot create two different idempotency keys concurrently.
    frappe.db.sql(
        "select name from `tabSarea Wallet Charge Request` where name=%s for update",
        (name,),
    )
    doc=frappe.get_doc("Sarea Wallet Charge Request",name)
    if doc.decision_idempotency_key:
        frappe.throw("A persisted decision already exists. Use Retry Same Decision.")
    api=SareaWalletAPI()
    _,remote,_=api.get_charge_request(int(doc.charge_request_id))
    if isinstance(remote,dict):
        upsert_charge_request(remote); frappe.db.commit(); doc.reload()
    if doc.status != "pending":
        frappe.throw(
            f"Charge request {doc.charge_request_id} cannot be newly decided because "
            f"its current Sarea Wallet status is {doc.status or 'unknown'}."
        )

    if decision=="approve":
        validate_payment_entry_before_approval(doc)

    key,payload=_persist_pre_send_state(doc,decision,notes)
    return _send_persisted(name,key,payload,decision)

def _send_persisted(name,key,payload,decision):
    doc=frappe.get_doc("Sarea Wallet Charge Request",name)
    attempts=int(doc.decision_attempts or 0)+1
    frappe.db.set_value("Sarea Wallet Charge Request",name,{
        "decision_attempts":attempts,"decision_state":"Sending","last_error":""
    },update_modified=True); frappe.db.commit()
    api=SareaWalletAPI()
    try:
        status,data,_=api.decide_charge_request(int(doc.charge_request_id),payload,key)
        _record_success(name,status,data)
        return {"ok":True,"http_status":status,"data":data}
    except SareaWalletAPIError as exc:
        if exc.code=="charge_request_already_decided":
            _record_error(name,exc)
            return _reconcile_already_decided(frappe.get_doc("Sarea Wallet Charge Request",name),decision,api)
        state=_record_error(name,exc)
        return {"ok":False,"retryable":state=="Retry Required","state":state,
                "http_status":exc.status_code,"code":exc.code,"retry_after":exc.retry_after,"message":str(exc)}

@frappe.whitelist()
def retry_charge_request_decision(name):
    frappe.only_for(("System Manager","Accounts Manager"))
    doc=frappe.get_doc("Sarea Wallet Charge Request",name)
    if not doc.decision_idempotency_key or not doc.decision_payload:
        frappe.throw("No persisted decision exists to retry.")
    if doc.decision_state=="Completed": frappe.throw("This decision is already completed.")
    p=json.loads(doc.decision_payload or "{}"); decision=p.get("decision")
    if decision not in {"approve","reject"}: frappe.throw("Persisted decision payload is invalid.")
    return _send_persisted(name,doc.decision_idempotency_key,doc.decision_payload,decision)

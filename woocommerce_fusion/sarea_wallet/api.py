from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any
import frappe
import requests

@dataclass
class SareaWalletAPIError(Exception):
    message: str
    status_code: int | None = None
    code: str | None = None
    response_data: Any = None
    retry_after: str | None = None
    def __str__(self):
        parts=[self.message]
        if self.status_code: parts.append(f"HTTP {self.status_code}")
        if self.code: parts.append(f"code={self.code}")
        return " | ".join(parts)

class SareaWalletAPI:
    def __init__(self):
        s=frappe.get_single("Sarea Wallet Settings")
        if not s.enabled:
            frappe.throw("Sarea Wallet integration is disabled in Sarea Wallet Settings.")
        self.base_url=(s.base_url or "").strip().rstrip("/")
        self.timeout=int(s.timeout_seconds or 30)
        self.verify_ssl=bool(s.verify_ssl)
        if not self.base_url:
            frappe.throw("Sarea Wallet Settings: Base URL is required.")
        key=s.get_password("consumer_key")
        secret=s.get_password("consumer_secret")
        if not key or not secret:
            frappe.throw("Sarea Wallet Settings: Consumer Key and Consumer Secret are required.")
        self.session=requests.Session()
        self.session.headers.update({
            "X-Sarea-Consumer-Key":key,
            "X-Sarea-Consumer-Secret":secret,
            "Accept":"application/json",
            "Content-Type":"application/json",
            "User-Agent":"ERPNext-Sarea-Wallet/1.0",
        })
    def _url(self,path): return f"{self.base_url}/{path.lstrip('/')}"
    def _decode(self,r):
        try: return r.json()
        except Exception: return {"raw":r.text[:4000]}
    def _request(self,method,path,params=None,payload_json=None,idempotency_key=None):
        method=method.upper().strip()
        if method not in {"GET","POST"}:
            raise ValueError(f"Unsupported Sarea Wallet HTTP method: {method}")
        headers={}
        if idempotency_key: headers["Idempotency-Key"]=idempotency_key
        try:
            r=self.session.request(method,self._url(path),params=params,
                data=payload_json.encode("utf-8") if payload_json is not None else None,
                headers=headers,timeout=self.timeout,verify=self.verify_ssl)
        except (requests.Timeout,requests.ConnectionError) as exc:
            raise SareaWalletAPIError(
                "Network outcome is unknown. Retry the same operation with the same idempotency key and payload.",
                code="network_error",response_data={"exception":exc.__class__.__name__}) from exc
        except requests.RequestException as exc:
            raise SareaWalletAPIError("Sarea Wallet request failed before a valid response was received.",
                code="request_error",response_data={"exception":exc.__class__.__name__}) from exc
        data=self._decode(r)
        if 200 <= r.status_code < 300:
            return r.status_code,data,r.headers
        code=data.get("code") if isinstance(data,dict) else None
        msg=(data.get("message") if isinstance(data,dict) else None) or "Sarea Wallet API returned an error."
        raise SareaWalletAPIError(msg,r.status_code,code,data,r.headers.get("Retry-After"))
    def ping(self): return self._request("GET","/ping")
    def list_charge_requests(self,page=1,per_page=100,status="pending",customer_id=None,blog_id=None,q=None):
        p={"page":int(page),"per_page":min(max(int(per_page),1),100),"status":status}
        if customer_id: p["customer_id"]=int(customer_id)
        if blog_id: p["blog_id"]=int(blog_id)
        if q: p["q"]=q
        return self._request("GET","/charge-requests",params=p)
    def get_charge_request(self,charge_request_id):
        return self._request("GET",f"/charge-requests/{int(charge_request_id)}")
    def decide_charge_request(self,charge_request_id,payload_json,idempotency_key):
        return self._request("POST",f"/charge-requests/{int(charge_request_id)}/decision",
            payload_json=payload_json,idempotency_key=idempotency_key)

frappe.ui.form.on("Sarea Wallet Charge Request",{
 refresh(frm){
  if(frm.is_new()) return;
  //frm.add_custom_button(__("Sync From Sarea"),()=>frappe.call({
  // method:"woocommerce_fusion.sarea_wallet.sync.sync_one_charge_request",
  // args:{name:frm.doc.name},freeze:true,callback(){frm.reload_doc();}
  //}));
  frappe.call({
   method:"woocommerce_fusion.sarea_wallet.payment_entry.get_payment_entry_status",
   args:{charge_request:frm.doc.name},
   callback(r){
    const x=r.message||{};
    frm.doc.payment_entry_created=x.exists?1:0;
    frm.refresh_field("payment_entry_created");
   }
  });

  frm.add_custom_button(__("Create / Open Payment Entry"),()=>frappe.call({
   method:"woocommerce_fusion.sarea_wallet.payment_entry.create_payment_entry",
   args:{charge_request:frm.doc.name},
   freeze:true,
   freeze_message:__("Preparing Payment Entry..."),
   callback(r){
    const x=r.message||{};
    if(x.payment_entry){
     frappe.set_route("Form","Payment Entry",x.payment_entry);
    }
   }
  }),__("Accounting"));
  if(frm.doc.status==="pending"&&!frm.doc.decision_idempotency_key){
   frm.add_custom_button(__("Approve"),()=>decision_dialog(frm,"approve"),__("Decision"));
   frm.add_custom_button(__("Reject"),()=>decision_dialog(frm,"reject"),__("Decision"));
  }
  if(frm.doc.decision_idempotency_key&&["Retry Required","Sending"].includes(frm.doc.decision_state)){
   frm.add_custom_button(__("Retry Same Decision"),()=>frappe.confirm(
    __("Retry using the exact same persisted Idempotency-Key and JSON payload?"),
    ()=>retry_decision(frm)),__("Decision"));
  }
 }
});
function decision_dialog(frm,decision){
 const label=decision==="approve"?__("Approve"):__("Reject");
 const d=new frappe.ui.Dialog({title:__("{0} Charge Request {1}",[label,frm.doc.charge_request_id]),
  fields:[
   {fieldtype:"HTML",options:`<div class="alert alert-warning">${__(
    "This changes the authoritative Sarea Wallet request. Amount, customer, store and payment method are not sent back."
   )}</div>`},
   {
      fieldname: "notes",
      fieldtype: "Small Text",
      label: __("Decision Reason / Notes"),
      reqd: 1,
      default: decision === "approve" ? frm.doc.notes : ""
    }
  ],
  primary_action_label:label,
  primary_action(v){d.hide();frappe.call({
   method:"woocommerce_fusion.woocommerce.doctype.sarea_wallet_charge_request.sarea_wallet_charge_request.decide_charge_request",
   args:{name:frm.doc.name,decision:decision,notes:v.notes},freeze:true,
   callback(r){show_result(r.message||{});frm.reload_doc();}
  });}
 }); d.show();
}
function retry_decision(frm){
 frappe.call({
  method:"woocommerce_fusion.woocommerce.doctype.sarea_wallet_charge_request.sarea_wallet_charge_request.retry_charge_request_decision",
  args:{name:frm.doc.name},freeze:true,
  callback(r){show_result(r.message||{});frm.reload_doc();}
 });
}
function show_result(r){
 frappe.msgprint({title:r.ok?__("Wallet Decision Completed"):(r.retryable?__("Retry Required"):__("Wallet Decision Not Completed")),
  indicator:r.ok?"green":(r.retryable?"orange":"red"),
  message:r.ok?(r.message||__("The Sarea Wallet decision completed successfully.")):
   frappe.utils.escape_html(r.message||__("Unknown integration error"))});
}

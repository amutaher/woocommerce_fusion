frappe.ui.form.on("Sarea Wallet Settings",{
 refresh(frm){
  frm.add_custom_button(__("Test Connection"),()=>frappe.call({
   method:"woocommerce_fusion.woocommerce.doctype.sarea_wallet_settings.sarea_wallet_settings.test_connection",
   freeze:true,freeze_message:__("Testing Sarea Wallet API..."),
   callback(r){
    const x=r.message||{};
    frappe.msgprint({title:x.ok?__("Connection Successful"):__("Connection Failed"),
     indicator:x.ok?"green":"red",
     message:x.ok?__("Sarea Wallet API responded successfully."):
      frappe.utils.escape_html(x.message||__("Unknown API error"))});
   }
  }));
 }
});

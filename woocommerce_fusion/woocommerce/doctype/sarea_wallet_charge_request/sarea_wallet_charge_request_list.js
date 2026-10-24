frappe.listview_settings["Sarea Wallet Charge Request"]={
 add_fields:["status","decision_state","amount_usd","reference"],
 get_indicator(doc){
  if(doc.status==="pending") return [__("Pending"),"orange","status,=,pending"];
  if(doc.status==="processing") return [__("Processing"),"blue","status,=,processing"];
  if(doc.status==="processed") return [__("Processed"),"green","status,=,processed"];
  if(doc.status==="rejected") return [__("Rejected"),"red","status,=,rejected"];
  return [__(doc.status||"Unknown"),"gray",`status,=,${doc.status}`];
 },
 onload(listview){
  listview.page.add_inner_button(__("Sync Active Requests"),()=>run_sync(listview,"active"),__("Sarea Wallet"));
  listview.page.add_inner_button(__("Import / Sync All"),()=>frappe.confirm(
   __("Sync all charge-request statuses from Sarea Wallet?"),()=>run_sync(listview,"all")),__("Sarea Wallet"));
  
  listview.page.add_inner_button(__("Sync Pending From Sarea"),()=>refresh_pending_from_sarea(listview),__("Sarea Wallet"));
 }
};
function run_sync(listview,status){
 frappe.call({method:"woocommerce_fusion.sarea_wallet.sync.sync_charge_requests",args:{status:status},freeze:true,
  callback(r){frappe.show_alert({message:__("Synchronized {0} request rows.",[(r.message||{}).synced||0]),indicator:"green"});listview.refresh();}});
}

function refresh_pending_from_sarea(listview){
 frappe.call({
  method:"woocommerce_fusion.sarea_wallet.sync.refresh_pending_charge_requests",
  freeze:true,
  freeze_message:__("Refreshing pending charge requests from Sarea..."),
  callback(r){
   const result=r.message||{};
   frappe.show_alert({
    message:__(
     "Checked {0} pending requests; {1} status updates found.",
     [result.checked||0,result.updated||0]
    ),
    indicator:(result.errors||0)?"orange":"green"
   });
   listview.refresh();
  }
 });
}
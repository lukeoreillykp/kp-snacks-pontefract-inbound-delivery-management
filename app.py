import hashlib
import hmac
import secrets
import uuid
from io import BytesIO
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from delivery_features import arrival_page, supplier_performance_page, render_documents
from supabase import Client, create_client

st.set_page_config(page_title="KP Snacks | Pontefract Inbound", page_icon="📦", layout="wide")
TZ = ZoneInfo("Europe/London")
KP_RED = "#C8102E"
ROLES = ["supervisor", "material_controller", "warehouse_manager", "administrator"]
ROLE_LABELS = {"supervisor":"Team Captain","material_controller":"Material Controller","warehouse_manager":"Warehouse Manager","administrator":"Administrator"}
PAGES = ["Dashboard","Delivery calendar","Booking management","Delivery arrivals","Orders & Excel upload","Warehouse resources","Products & delivery rules","Suppliers","Reports","User & role management"]
ROLE_PAGES = {
    "supervisor": set(PAGES[:4]),
    "material_controller": set(PAGES[:4] + ["Orders & Excel upload","Products & delivery rules","Reports"]),
    "warehouse_manager": set(PAGES[:4] + ["Warehouse resources","Reports"]),
    "administrator": set(PAGES),
}
ACTIVE_BOOKING = ["requested","alternative_proposed","confirmed"]

@st.cache_resource
def db():
    c = st.secrets["supabase"]
    return create_client(c["url"], c["key"])

def rows(table, select="*"):
    return db().table(table).select(select).execute().data or []

def one(table, **filters):
    q = db().table(table).select("*")
    for k,v in filters.items(): q=q.eq(k,v)
    data=q.limit(1).execute().data or []
    return data[0] if data else None

def pw_hash(value):
    salt=secrets.token_bytes(16)
    digest=hashlib.scrypt(value.encode(),salt=salt,n=2**14,r=8,p=1,dklen=64)
    return "scrypt$16384$8$1$"+salt.hex()+"$"+digest.hex()

def pw_ok(value, encoded):
    try:
        alg,n,r,p,salt,digest=encoded.split("$")
        if alg!="scrypt": return False
        actual=hashlib.scrypt(value.encode(),salt=bytes.fromhex(salt),n=int(n),r=int(r),p=int(p),dklen=len(bytes.fromhex(digest))).hex()
        return hmac.compare_digest(actual,digest)
    except Exception: return False

def credential(role):
    return one("internal_role_credentials", role=role)

def audit_password(role, action):
    db().table("internal_role_password_events").insert({"role":role,"action":action,"changed_by_role":"administrator"}).execute()

def log_event(booking_id, event, old=None, new=None, reason=None, details=None):
    db().table("booking_events").insert({"booking_id":booking_id,"event_type":event,"previous_status":old,"new_status":new,"actor_label":st.session_state.get("auth_role","supplier"),"reason":reason,"details":details or {}}).execute()

def queue_email(supplier, subject, template, payload, booking_id=None):
    address=supplier.get("contact_email")
    if address:
        db().table("email_outbox").insert({"booking_id":booking_id,"recipient_email":address,"subject":subject,"template_key":template,"payload":payload}).execute()

def queue_internal_email(subject, template, payload, booking_id=None, change_request_id=None):
    setting=one('app_settings',setting_key='internal_notification_emails')
    raw=setting.get('setting_value') if setting else None
    if not raw:
        legacy=one('app_settings',setting_key='internal_notification_email')
        raw=legacy.get('setting_value') if legacy else None
    if isinstance(raw,dict): raw=raw.get('emails') or raw.get('email')
    recipients=raw if isinstance(raw,list) else str(raw or '').replace(';',',').split(',')
    for recipient in sorted({str(x).strip() for x in recipients if str(x).strip()}):
        db().table('email_outbox').insert({'booking_id':booking_id,'change_request_id':change_request_id,'recipient_email':recipient,'subject':subject,'template_key':template,'payload':payload}).execute()

def df(table, select="*"):
    return pd.DataFrame(rows(table,select))

def local_now():
    return datetime.now(TZ)

def fmt(dt):
    try: return datetime.fromisoformat(str(dt).replace("Z","+00:00")).astimezone(TZ).strftime("%d/%m/%Y %H:%M")
    except Exception: return str(dt)

def get_rule(product_id):
    return one("product_rules",product_id=product_id)

def area_for_rule(rule):
    return one("delivery_areas",id=rule["delivery_area_id"]) if rule else None

def overlap(start,end,other_start,other_end):
    return start < other_end and end > other_start

def capacity_check(area_id, start, duration, buffer, ignore_id=None):
    end=start+timedelta(minutes=duration+buffer)
    existing=rows("bookings")
    for b in existing:
        if b.get("status") not in ACTIVE_BOOKING or b.get("delivery_area_id")!=area_id or b.get("id")==ignore_id: continue
        try:
            bstart=datetime.fromisoformat(b["scheduled_start"].replace("Z","+00:00")).astimezone(TZ)
            bend=datetime.fromisoformat(b["scheduled_end"].replace("Z","+00:00")).astimezone(TZ)+timedelta(minutes=int(b.get("buffer_minutes") or 0))
            if overlap(start,end,bstart,bend):
                return False, f"Area conflict with booking {b.get('booking_reference','')}, {fmt(bstart)}–{fmt(bend)}."
        except Exception: continue
    return True, "Area is available. Area and staffing are checked again transactionally when saved."

def create_booking(supplier_id, product_id, start, duration, quantity=None, unit=None, po_id=None, source="manual", status="requested", notes="", override=False, override_reason=""):
    rule=get_rule(product_id)
    if not rule or not rule.get("active"): raise ValueError("This product has no active delivery rule.")
    product=one("products",id=product_id)
    if not product or not product.get("active"): raise ValueError("This product is inactive.")
    if quantity is not None and product.get("max_quantity") is not None and float(quantity)>float(product["max_quantity"]):
        raise ValueError(f"Maximum quantity for this product is {product['max_quantity']} {product.get('quantity_unit') or ''}.")
    area=area_for_rule(rule)
    if not area or not area.get("active"): raise ValueError("The delivery area is not active.")
    buffer=int(rule.get("buffer_minutes") if rule.get("buffer_minutes") is not None else area.get("buffer_minutes",20))
    end=start+timedelta(minutes=duration)
    reference="IN-"+datetime.now(TZ).strftime("%y%m%d")+"-"+secrets.token_hex(3).upper()
    payload={"booking_reference":reference,"supplier_id":supplier_id,"purchase_order_id":po_id,"product_id":product_id,"delivery_area_id":area["id"],"status":status,"source":source,"scheduled_start":start.isoformat(),"scheduled_end":end.isoformat(),"duration_minutes":int(duration),"buffer_minutes":buffer,"quantity":quantity,"quantity_unit":unit,"operators_required":int(rule.get("operators_required",1)),"capacity_override":bool(override),"capacity_override_reason":override_reason or None,"supplier_notes":notes or None}
    result=db().rpc("create_inbound_booking",{"p_booking":payload}).execute().data
    b=result if isinstance(result,dict) else (result[0] if result else None)
    if not b: raise RuntimeError("Booking could not be saved.")
    return b

def enqueue_status_email(booking, status):
    supplier=one("suppliers",id=booking["supplier_id"])
    if supplier:
        queue_email(supplier,f"Inbound delivery {booking['booking_reference']} – {status.replace('_',' ').title()}","booking_status",{"booking_reference":booking["booking_reference"],"status":status,"scheduled_start":booking["scheduled_start"]},booking["id"])

def save_role_access():
    st.subheader("Shared role access")
    admin_rec=credential("administrator") or {}
    with st.expander("Change Administrator password"):
        with st.form("admin_password_change"):
            admin_p1=st.text_input("New Administrator password (minimum 12 characters)",type="password")
            admin_p2=st.text_input("Confirm Administrator password",type="password")
            admin_save=st.form_submit_button("Update Administrator password")
        if admin_save:
            if len(admin_p1)<12 or admin_p1!=admin_p2: st.error("Passwords must match and be at least 12 characters.")
            else:
                db().table("internal_role_credentials").update({"password_hash":pw_hash(admin_p1),"password_changed_at":local_now().isoformat(),"updated_at":local_now().isoformat()}).eq("role","administrator").execute()
                audit_password("administrator","password_changed")
                st.success("Administrator password updated."); st.rerun()
    for role in ROLES:
        if role=="administrator": continue
        rec=credential(role) or {"enabled":False,"password_hash":None}
        with st.expander(ROLE_LABELS[role],expanded=not bool(rec.get("password_hash"))):
            st.write("Set or change the shared password for everyone using this role.")
            enabled=st.toggle("Role enabled",value=bool(rec.get("enabled")),key=f"enable_{role}")
            with st.form(f"pw_{role}"):
                p1=st.text_input("New password (minimum 12 characters)",type="password")
                p2=st.text_input("Confirm new password",type="password")
                save=st.form_submit_button("Save role access")
            if save or enabled != bool(rec.get("enabled")):
                if save and p1:
                    if len(p1)<12 or p1!=p2: st.error("Use at least 12 characters and make both entries match.")
                    else:
                        db().table("internal_role_credentials").upsert({"role":role,"password_hash":pw_hash(p1),"enabled":enabled,"password_changed_at":local_now().isoformat(),"updated_at":local_now().isoformat()},on_conflict="role").execute()
                        audit_password(role,"password_changed" if rec.get("password_hash") else "password_set")
                        st.success(f"{ROLE_LABELS[role]} access saved."); st.rerun()
                else:
                    db().table("internal_role_credentials").upsert({"role":role,"password_hash":rec.get("password_hash"),"enabled":enabled,"updated_at":local_now().isoformat()},on_conflict="role").execute()
                    audit_password(role,"role_enabled" if enabled else "role_disabled")
                    st.success("Role setting updated."); st.rerun()

def sign_in():
    st.subheader("Internal team sign-in")
    with st.form("internal_login"):
        role=st.selectbox("Access level",ROLES,format_func=lambda x:ROLE_LABELS[x])
        password=st.text_input("Shared role password",type="password")
        go=st.form_submit_button("Sign in",type="primary")
    if go:
        rec=credential(role)
        if rec and rec.get("enabled") and rec.get("password_hash") and pw_ok(password,rec["password_hash"]):
            st.session_state.pop("supplier_id",None)
            st.session_state["auth_role"]=role; st.rerun()
        else: st.error("Sign-in failed. Check the password or contact the Administrator.")

def bootstrap():
    try: token=st.secrets["bootstrap"]["token"]
    except Exception:
        st.warning("Administrator setup is not enabled. Contact the app owner.")
        return
    st.subheader("First-time administrator setup")
    st.write("Enter the one-time setup token, then choose an Administrator password of at least 12 characters.")
    with st.form("bootstrap"):
        supplied=st.text_input("One-time setup token",type="password")
        p1=st.text_input("Choose Administrator password",type="password")
        p2=st.text_input("Confirm Administrator password",type="password")
        go=st.form_submit_button("Create Administrator access",type="primary")
    if go:
        if not hmac.compare_digest(supplied,str(token)): st.error("The setup token is incorrect.")
        elif len(p1)<12 or p1!=p2: st.error("Passwords must match and be at least 12 characters.")
        else:
            db().table("internal_role_credentials").insert({"role":"administrator","password_hash":pw_hash(p1),"enabled":True}).execute()
            audit_password("administrator","password_set")
            st.session_state["auth_role"]="administrator"; st.success("Administrator access created."); st.rerun()

def dashboard(public=False):
    today=local_now().date()
    bookings=rows("bookings")
    due=[b for b in bookings if str(b.get("scheduled_start",""))[:10]==today.isoformat()]
    pending=[b for b in bookings if b.get("status") in ("requested","alternative_proposed")]
    a,b,c,d=st.columns(4)
    a.metric("Expected today",len([x for x in due if x.get("status") in ACTIVE_BOOKING]))
    b.metric("Pending approvals",len(pending) if not public else "—")
    c.metric("Confirmed today",len([x for x in due if x.get("status")=="confirmed"]))
    d.metric("Exceptions",len([x for x in due if x.get("capacity_override")]))
    st.markdown("### Warehouse capacity")
    for area in rows("delivery_areas"):
        active=[x for x in due if x.get("delivery_area_id")==area["id"] and x.get("status") in ACTIVE_BOOKING]
        st.write(f"**{area['name']}** · {len(active)} scheduled delivery slot(s) · one delivery at a time")
    st.markdown("### Today's deliveries")
    safe=[]
    for x in due:
        product=one("products",id=x["product_id"])
        safe.append({"Time":fmt(x["scheduled_start"]),"Product":product["name"] if product else "Delivery","Status":x["status"].replace("_"," ").title()})
    if safe: st.dataframe(pd.DataFrame(safe),use_container_width=True,hide_index=True)
    else: st.info("No deliveries are currently recorded for today.")
    if not public:
        st.markdown("### Approval queue")
        st.dataframe(pd.DataFrame([{"Reference":x["booking_reference"],"Status":x["status"],"Start":fmt(x["scheduled_start"])} for x in pending]),use_container_width=True,hide_index=True)

def calendar_page(public=False):
    st.subheader("Delivery calendar")
    start=st.date_input("Week commencing",value=local_now().date()-timedelta(days=local_now().weekday()))
    end=start+timedelta(days=6)
    data=[]
    for x in rows("bookings"):
        try: dt=datetime.fromisoformat(x["scheduled_start"].replace("Z","+00:00")).astimezone(TZ)
        except Exception: continue
        if start<=dt.date()<=end and x.get("status") in ACTIVE_BOOKING+["completed"]:
            product=one("products",id=x["product_id"]); area=one("delivery_areas",id=x["delivery_area_id"])
            item={"Date":dt.strftime("%a %d %b"),"Start":dt.strftime("%H:%M"),"End":fmt(x["scheduled_end"])[11:],"Area":area["name"] if area else "","Product":product["name"] if product else "","Status":x["status"].replace("_"," ").title()}
            if not public: item["Reference"]=x["booking_reference"]
            data.append(item)
    if data: st.dataframe(pd.DataFrame(data).sort_values(["Date","Start"]),use_container_width=True,hide_index=True)
    else: st.info("No deliveries scheduled for this week.")

def booking_management():
    st.subheader("Booking management")
    pending=[b for b in rows("bookings") if b.get("status")=="requested"]
    if not pending: st.info("There are no booking requests awaiting a decision.")
    for b in pending:
        product=one("products",id=b["product_id"]); supplier=one("suppliers",id=b["supplier_id"])
        with st.container(border=True):
            st.markdown(f"**{b['booking_reference']} · {supplier['name'] if supplier else 'Supplier'} · {product['name'] if product else 'Product'}**")
            st.write(f"{fmt(b['scheduled_start'])} · {b['duration_minutes']} minutes · {b['status'].replace('_',' ').title()}")
            adjusted_duration=st.number_input("Approved unloading duration (minutes)",15,600,int(b["duration_minutes"]),step=5,key=f"approval_duration_{b['id']}")
            c1,c2,c3=st.columns(3)
            if c1.button("Approve / confirm",key=f"approve_{b['id']}"):
                try:
                    updated=db().rpc("approve_inbound_booking",{"p_booking_id":b["id"],"p_duration_minutes":int(adjusted_duration)}).execute().data
                    updated=updated if isinstance(updated,dict) else (updated[0] if updated else b)
                    log_event(updated["id"],"booking_approved_by_role","requested","confirmed",None,{"role":st.session_state.get("auth_role"),"duration_minutes":int(adjusted_duration)})
                    enqueue_status_email(updated,"confirmed"); st.success("Booking confirmed."); st.rerun()
                except Exception as e: st.error(f"Booking could not be approved: {e}")
            if c2.button("Reject",key=f"reject_{b['id']}"):
                st.session_state["reject_id"]=b["id"]
            if c3.button("Propose alternative",key=f"alt_{b['id']}"):
                st.session_state["alt_id"]=b["id"]
    if st.session_state.get("reject_id"):
        b=one("bookings",id=st.session_state["reject_id"])
        reason=st.text_input("Reason for rejection",key="reject_reason")
        if st.button("Confirm rejection"):
            if not reason.strip(): st.error("Enter a reason.")
            else:
                db().table("bookings").update({"status":"rejected","internal_notes":reason,"updated_at":local_now().isoformat()}).eq("id",b["id"]).execute()
                log_event(b["id"],"booking_rejected",b["status"],"rejected",reason); enqueue_status_email(b,"rejected")
                st.session_state.pop("reject_id"); st.rerun()
    if st.session_state.get("alt_id"):
        b=one("bookings",id=st.session_state["alt_id"])
        with st.form("alternative"):
            d=st.date_input("Alternative date",value=local_now().date()+timedelta(days=7))
            t=st.time_input("Alternative start",value=time(8,0))
            duration=st.number_input("Alternative unloading duration (minutes)",15,600,int(b["duration_minutes"]),step=5)
            reason=st.text_input("Message to supplier")
            go=st.form_submit_button("Send alternative proposal")
        if go:
            start=datetime.combine(d,t,TZ)
            try:
                updated=db().rpc("propose_inbound_alternative",{"p_booking_id":b["id"],"p_new_start":start.isoformat(),"p_duration_minutes":int(duration),"p_message":reason}).execute().data
                updated=updated if isinstance(updated,dict) else (updated[0] if updated else b)
                log_event(updated["id"],"alternative_proposed_by_role",b["status"],"alternative_proposed",reason,{"role":st.session_state.get("auth_role"),"duration_minutes":int(duration)})
                enqueue_status_email(updated,"alternative_proposed")
                st.success("Alternative sent to supplier."); st.session_state.pop("alt_id"); st.rerun()
            except Exception as e: st.error(f"Alternative could not be proposed: {e}")
    st.markdown("### Supplier amendments and cancellation requests")
    changes=[x for x in rows("booking_change_requests") if x.get("status") in ("pending","alternative_proposed")]
    for ch in changes:
        b=one("bookings",id=ch["booking_id"])
        if not b: continue
        supplier=one("suppliers",id=b["supplier_id"])
        with st.container(border=True):
            st.write(f"**{b['booking_reference']} · {supplier['name'] if supplier else 'Supplier'}**")
            st.write(ch.get("reason") or "Change request")
            st.write(f"Requested time: {fmt(ch['proposed_start'])}")
            x1,x2=st.columns(2)
            if x1.button("Approve change request",key=f"change_ok_{ch['id']}"):
                try:
                    updated=db().rpc("approve_inbound_change",{"p_change_id":ch["id"]}).execute().data
                    updated=updated if isinstance(updated,dict) else (updated[0] if updated else b)
                    log_event(updated["id"],"supplier_change_approved_by_role",updated["status"],updated["status"],None,{"role":st.session_state.get("auth_role")})
                    enqueue_status_email(updated,updated["status"])
                    st.success("Change request approved."); st.rerun()
                except Exception as e:
                    st.error(f"Change request could not be approved: {e}")
            if x2.button("Reject change request",key=f"change_no_{ch['id']}"):
                db().table("booking_change_requests").update({"status":"rejected","reviewed_at":local_now().isoformat(),"manager_response":"Rejected by site team"}).eq("id",ch["id"]).execute()
                log_event(b["id"],"supplier_change_rejected",b["status"],b["status"],"Supplier change request rejected")
                enqueue_status_email(b,"confirmed"); st.rerun()
    st.divider()
    st.markdown("### Create manual booking")
    suppliers=rows("suppliers"); products=rows("products")
    if not suppliers or not products: st.info("Add suppliers and products before creating a booking."); return
    with st.form("manual_booking"):
        supplier=st.selectbox("Supplier",suppliers,format_func=lambda x:x["name"])
        product=st.selectbox("Product",products,format_func=lambda x:x["name"])
        day=st.date_input("Delivery date",value=local_now().date()+timedelta(days=1))
        tm=st.time_input("Start time",value=time(8,0))
        rule=get_rule(product["id"]); duration=st.number_input("Duration (minutes)",min_value=15,max_value=600,value=int(rule["standard_duration_minutes"]) if rule else 60,step=5)
        quantity=st.number_input("Quantity",min_value=0.0,value=0.0)
        note=st.text_input("Internal / supplier notes")
        override=st.checkbox("Administrator / Warehouse Manager capacity override")
        reason=st.text_input("Override reason (mandatory if overriding)")
        go=st.form_submit_button("Create confirmed manual booking")
    if go:
        try:
            if override and st.session_state.get("auth_role") not in ("administrator","warehouse_manager"): raise ValueError("Only an Administrator or Warehouse Manager can override capacity.")
            if override and not reason.strip(): raise ValueError("Enter a reason for the capacity override.")
            b=create_booking(supplier["id"],product["id"],datetime.combine(day,tm,TZ),duration,quantity,product.get("quantity_unit"),source="manual",status="confirmed",notes=note,override=override,override_reason=reason)
            log_event(b["id"],"manual_booking_created_by_role",None,"confirmed",reason if override else None,{"role":st.session_state.get("auth_role"),"duration_minutes":duration})
            enqueue_status_email(b,"confirmed")
            st.success(f"Created {b['booking_reference']}"); st.rerun()
        except Exception as e: st.error(str(e))

def orders_page():
    st.subheader("Orders & Excel upload")
    st.write("Upload the weekly PO workbook. The system previews changes and flags missing POs or quantity conflicts before anything is updated.")
    template=pd.DataFrame(columns=["supplier_code","po_number","product","delivery_type","quantity","quantity_unit","due_date","latest_delivery_date","booking_permitted"])
    buffer=BytesIO()
    with pd.ExcelWriter(buffer,engine="openpyxl") as writer:
        template.to_excel(writer,index=False,sheet_name="Purchase Orders")
    st.download_button("Download Excel import template",buffer.getvalue(),"inbound-po-import-template.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    up=st.file_uploader("Purchase order workbook",type=["xlsx","xls"])
    if up:
        try:
            incoming=pd.read_excel(up)
            st.dataframe(incoming,use_container_width=True)
            required={"supplier_code","po_number","product"}
            missing=required-set(incoming.columns)
            if missing:
                st.error("Missing columns: "+", ".join(sorted(missing)))
            elif st.button("Compare workbook with current open POs",type="primary"):
                suppliers={x["supplier_code"]:x for x in rows("suppliers")}
                products={x["name"].lower():x for x in rows("products")}
                existing=rows("purchase_orders")
                existing_map={(x["supplier_id"],x["po_number"],x["product_id"]):x for x in existing}
                candidates=[]; incoming_keys=set(); row_issues=[]
                batch=db().table("po_import_batches").insert({"file_name":up.name,"uploaded_by_role":st.session_state.get("auth_role","material_controller"),"status":"preview","rows_total":len(incoming)}).execute().data
                batch_id=batch[0]["id"] if batch else None
                for idx,row in incoming.iterrows():
                    code=str(row.get("supplier_code","")).strip()
                    po=str(row.get("po_number","")).strip()
                    prod=products.get(str(row.get("product","")).strip().lower())
                    sup=suppliers.get(code)
                    if not sup or not po or not prod:
                        row_issues.append({"row":int(idx)+2,"issue":"Supplier, PO number or product could not be matched."}); continue
                    def val(k):
                        v=row.get(k)
                        if v is None or pd.isna(v): return None
                        if k in ("due_date","latest_delivery_date"): return pd.to_datetime(v).date().isoformat()
                        return v
                    key=(sup["id"],po,prod["id"]); incoming_keys.add(key)
                    old=existing_map.get(key)
                    payload={"supplier_id":sup["id"],"po_number":po,"product_id":prod["id"],"delivery_type":None if val("delivery_type") is None else str(val("delivery_type")),"quantity":val("quantity"),"quantity_unit":val("quantity_unit") or prod.get("quantity_unit"),"due_date":val("due_date"),"latest_delivery_date":val("latest_delivery_date"),"booking_permitted":True if val("booking_permitted") is None else bool(val("booking_permitted")),"source_file_name":up.name,"status":old.get("status","open") if old else "open"}
                    conflict=None
                    if old and old.get("quantity") is not None and payload["quantity"] is not None and float(payload["quantity"])<float(old["quantity"]):
                        linked=[b for b in rows("bookings") if b.get("purchase_order_id")==old["id"] and b.get("status") in ("requested","alternative_proposed","confirmed","completed")]
                        committed=sum(float(b.get("quantity") or 0) for b in linked)
                        if float(payload["quantity"])<committed:
                            conflict={"po_number":po,"supplier":sup["name"],"new_quantity":payload["quantity"],"committed_quantity":committed,"existing_quantity":old["quantity"],"purchase_order_id":old["id"],"supplier_id":sup["id"]}
                            payload["quantity"]=old["quantity"]
                    candidates.append({"payload":payload,"old":old,"conflict":conflict,"key":key})
                missing_pos=[x for x in existing if x.get("status")=="open" and (x["supplier_id"],x["po_number"],x["product_id"]) not in incoming_keys]
                st.session_state["po_import_preview"]={"batch_id":batch_id,"file_name":up.name,"candidates":candidates,"missing":missing_pos,"row_issues":row_issues}
                st.rerun()
        except Exception as e:
            st.error(f"Could not read workbook: {e}")
    preview=st.session_state.get("po_import_preview")
    if preview:
        st.markdown("### Import review")
        st.write(f'Workbook: **{preview["file_name"]}** · {len(preview["candidates"])} matched row(s)')
        if preview["row_issues"]:
            st.warning("Rows needing correction: "+ " · ".join(f'Row {x["row"]}: {x["issue"]}' for x in preview["row_issues"]))
        conflicts=[x for x in preview["candidates"] if x["conflict"]]
        if conflicts:
            st.warning("Quantity reductions below already booked or delivered quantities have been detected. The existing PO quantity will be retained for those rows; the lower figure will not be applied.")
            for item in conflicts:
                c=item["conflict"]
                st.write(f'**{c["supplier"]} · PO {c["po_number"]}** — uploaded {c["new_quantity"]}; already committed {c["committed_quantity"]}; existing PO quantity {c["existing_quantity"]}.')
        if preview["missing"]:
            st.markdown("### Open POs missing from this upload")
            st.caption("Nothing will be closed automatically. Choose what should happen to each missing PO.")
            suppliers={x["id"]:x for x in rows("suppliers")}
            products={x["id"]:x for x in rows("products")}
            for item in preview["missing"]:
                label=f'{suppliers.get(item["supplier_id"],{}).get("name","Supplier")} · {item["po_number"]} · {products.get(item["product_id"],{}).get("name","Product")} · outstanding PO quantity {item.get("quantity")}'
                st.selectbox(label,["Keep open","Close PO","Investigate"],key=f'po_missing_{item["id"]}')
        if st.button("Confirm PO updates",type="primary"):
            created=updated=0
            for item in preview["candidates"]:
                payload=item["payload"]
                try:
                    db().table("purchase_orders").upsert(payload,on_conflict="supplier_id,po_number,product_id").execute()
                    if item["old"]: updated+=1
                    else: created+=1
                    if item["conflict"]:
                        c=item["conflict"]
                        db().table("po_import_issues").insert({"batch_id":preview["batch_id"],"purchase_order_id":c["purchase_order_id"],"supplier_id":c["supplier_id"],"po_number":c["po_number"],"issue_type":"quantity_below_committed","details":c,"resolution":"kept_existing_quantity","resolution_reason":"Uploaded quantity was below booked/delivered quantity; existing PO quantity retained.","resolved_by_role":st.session_state.get("auth_role"),"resolved_at":local_now().isoformat()}).execute()
                except Exception as ex:
                    st.error(f'Could not update PO {payload["po_number"]}: {type(ex).__name__}')
            for item in preview["missing"]:
                decision=st.session_state.get(f'po_missing_{item["id"]}',"Keep open")
                resolution={"Keep open":"kept_open","Close PO":"closed_by_material_controller","Investigate":"investigation_required"}[decision]
                if decision=="Close PO":
                    db().table("purchase_orders").update({"status":"closed","booking_permitted":False,"updated_at":local_now().isoformat()}).eq("id",item["id"]).eq("status","open").execute()
                supplier=one("suppliers",id=item["supplier_id"])
                db().table("po_import_issues").insert({"batch_id":preview["batch_id"],"purchase_order_id":item["id"],"supplier_id":item["supplier_id"],"po_number":item["po_number"],"issue_type":"missing_from_weekly_upload","details":{"supplier":supplier["name"] if supplier else ""},"resolution":resolution,"resolved_by_role":st.session_state.get("auth_role"),"resolved_at":local_now().isoformat() if decision!="Investigate" else None}).execute()
            db().table("po_import_batches").update({"status":"applied","rows_created":created,"rows_updated":updated,"rows_flagged":len(conflicts)+len(preview["missing"])+len(preview["row_issues"]),"applied_at":local_now().isoformat()}).eq("id",preview["batch_id"]).execute()
            st.session_state.pop("po_import_preview",None)
            st.success(f"PO import complete: {created} new, {updated} updated. Missing POs were handled using your decisions; existing booking history was preserved.")
            st.rerun()
        if st.button("Discard preview"):
            st.session_state.pop("po_import_preview",None)
            st.rerun()
    st.markdown("### Current purchase orders")
    orders=rows("purchase_orders")
    out=[]
    for x in orders:
        s=one("suppliers",id=x["supplier_id"]); p=one("products",id=x["product_id"])
        out.append({"Supplier":s["name"] if s else "","PO":x["po_number"],"Product":p["name"] if p else "","Quantity":x.get("quantity"),"Due":x.get("due_date"),"Latest":x.get("latest_delivery_date"),"Status":x.get("status"),"Booking allowed":x.get("booking_permitted")})
    if out: st.dataframe(pd.DataFrame(out),use_container_width=True,hide_index=True)

def warehouse_page():
    st.subheader("Warehouse resources & staffing")
    shifts=rows("shift_definitions")
    day=st.date_input("Staffing plan date",value=local_now().date())
    current=[x for x in rows("staffing_plans") if x["plan_date"]==day.isoformat()]
    for shift in shifts:
        existing=next((x for x in current if x["shift_id"]==shift["id"]),None)
        with st.form(f"staff_{shift['shift_code']}"):
            st.markdown(f"**{shift['display_name']}**")
            defaults={"days":4,"afternoons":3,"nights":4}
            head=st.number_input("Main team headcount",0,30,int(existing["main_team_headcount"]) if existing else defaults[shift["shift_code"]])
            holiday=st.number_input("People on holiday (maximum 1 per shift)",0,1,int(existing["people_on_holiday"]) if existing else 0)
            extra=st.checkbox("Additional operator available",bool(existing["additional_operator_available"]) if existing else False)
            bulk=st.checkbox("Dedicated bulk operator available",bool(existing["bulk_operator_available"]) if existing else (shift["shift_code"]=="days"),disabled=shift["shift_code"]!="days")
            if shift["shift_code"]!="days": bulk=False
            note=st.text_input("Notes",value=existing.get("notes","") if existing else "")
            save=st.form_submit_button("Save shift plan")
        if save:
            payload={"plan_date":day.isoformat(),"shift_id":shift["id"],"main_team_headcount":head,"people_on_holiday":holiday,"additional_operator_available":extra,"additional_operator_count":1 if extra else 0,"bulk_operator_available":bulk,"protected_outbound_headcount":2,"notes":note}
            db().table("staffing_plans").upsert(payload,on_conflict="plan_date,shift_id").execute()
            st.success(f"{shift['display_name']} staffing saved."); st.rerun()
    st.caption("Capacity planning protects two people for outbound/palletisation. The absence register is a record; update the shift plan's headcount and holiday count so the capacity calculation reflects planned cover. The weekly rotation anchor is not configured, so staffing plans are entered by date rather than auto-generated.")
    st.markdown("### Staff / absence register")
    with st.form("staff_add"):
        name=st.text_input("Staff display name")
        group=st.selectbox("Team",["main_team","additional_operator","dedicated_bulk"])
        add=st.form_submit_button("Add staff member")
    if add and name.strip():
        db().table("staff_members").insert({"display_name":name.strip(),"staff_group":group,"active":True}).execute(); st.success("Staff member added."); st.rerun()
    staff=rows("staff_members")
    if staff: st.dataframe(pd.DataFrame([{"Name":x["display_name"],"Group":x["staff_group"],"Active":x["active"]} for x in staff]),use_container_width=True,hide_index=True)
    if staff:
        selected=st.selectbox("Record holiday / sickness / training for",staff,format_func=lambda x:x["display_name"])
        selected_shift=st.selectbox("Shift",shifts,format_func=lambda x:x["display_name"])
        with st.form("absence"):
            typ=st.selectbox("Assignment type",["holiday","absence","training","other"])
            start=st.date_input("From",value=day); end=st.date_input("To",value=day)
            note=st.text_input("Notes")
            save=st.form_submit_button("Save absence")
        if save:
            if end<start: st.error("End date must be on or after start date.")
            else:
                existing_holidays=[x for x in rows("staff_shift_assignments") if x.get("assignment_type")=="holiday" and x.get("shift_id")==selected_shift["id"] and x.get("staff_member_id")!=selected["id"] and str(x["starts_at"])[:10]<=(end+timedelta(days=1)).isoformat() and str(x["ends_at"])[:10]>start.isoformat()]
                if typ=="holiday" and existing_holidays:
                    st.error("Only one person can be on holiday from this shift at a time.")
                else:
                    db().table("staff_shift_assignments").insert({"staff_member_id":selected["id"],"shift_id":selected_shift["id"],"starts_at":datetime.combine(start,time(0),TZ).isoformat(),"ends_at":datetime.combine(end+timedelta(days=1),time(0),TZ).isoformat(),"assignment_type":typ,"notes":note}).execute()
                    st.success("Absence record saved."); st.rerun()
    st.markdown("### Assign operators to deliveries")
    active_bookings=[b for b in rows("bookings") if b.get("status") in ACTIVE_BOOKING]
    active_staff=[x for x in rows("staff_members") if x.get("active")]
    if active_bookings and active_staff:
        with st.form("operator_assignment"):
            booking=st.selectbox("Delivery booking",active_bookings,format_func=lambda x:f"{x['booking_reference']} · {fmt(x['scheduled_start'])}")
            operator=st.selectbox("Operator",active_staff,format_func=lambda x:f"{x['display_name']} · {x['staff_group']}")
            assign=st.form_submit_button("Assign operator")
        if assign:
            already=[x for x in rows("booking_operator_assignments") if x["booking_id"]==booking["id"]]
            if len(already)>=int(booking.get("operators_required",1)):
                st.error("This booking already has the required number of operators.")
            else:
                conflicts=[]
                for x in rows("booking_operator_assignments"):
                    if x["staff_member_id"]!=operator["id"]: continue
                    if str(x["starts_at"])<booking["scheduled_end"] and str(x["ends_at"])>booking["scheduled_start"]: conflicts.append(x)
                absences=[x for x in rows("staff_shift_assignments") if x["staff_member_id"]==operator["id"] and x["assignment_type"] in ("holiday","absence") and str(x["starts_at"])<booking["scheduled_end"] and str(x["ends_at"])>booking["scheduled_start"]]
                rule=get_rule(booking["product_id"])
                if conflicts: st.error("This operator is already assigned to an overlapping delivery.")
                elif absences: st.error("This operator has a holiday or absence recorded during this delivery.")
                elif rule and rule.get("requires_dedicated_bulk_operator") and operator["staff_group"]!="dedicated_bulk": st.error("This product requires the dedicated bulk operator.")
                else:
                    db().table("booking_operator_assignments").insert({"booking_id":booking["id"],"staff_member_id":operator["id"],"starts_at":booking["scheduled_start"],"ends_at":booking["scheduled_end"]}).execute()
                    log_event(booking["id"],"operator_assigned",booking["status"],booking["status"],None,{"staff_member":operator["display_name"]})
                    st.success("Operator assigned."); st.rerun()
    elif not active_staff: st.info("Add staff members before assigning operators.")
    if active_bookings:
        assignments=rows("booking_operator_assignments")
        display=[]
        for a in assignments:
            b=one("bookings",id=a["booking_id"]); person=one("staff_members",id=a["staff_member_id"])
            if b and person: display.append({"Booking":b["booking_reference"],"Operator":person["display_name"],"Start":fmt(a["starts_at"]),"End":fmt(a["ends_at"])})
        if display: st.dataframe(pd.DataFrame(display),use_container_width=True,hide_index=True)

def rules_page():
    st.subheader("Products & delivery rules")
    products=rows("products"); areas=rows("delivery_areas")
    with st.expander("Add a new product to the catalogue"):
        with st.form("new_product"):
            new_name=st.text_input("Product name")
            category=st.selectbox("Category",["ingredient","packaging","sundries"])
            method=st.selectbox("Delivery method",["tanker","one_tonne_totes","ibc","palletised"])
            unit=st.text_input("Quantity unit",value="pallets")
            area=st.selectbox("Delivery area",areas,format_func=lambda x:x["name"])
            duration=st.number_input("Standard duration (minutes)",15,600,45,step=5)
            min_duration=st.number_input("Minimum duration",15,600,45,step=5)
            max_duration=st.number_input("Maximum duration",15,600,70,step=5)
            create=st.form_submit_button("Add product")
        if create:
            if not new_name.strip(): st.error("Enter a product name.")
            elif min_duration>max_duration: st.error("Minimum duration cannot exceed maximum.")
            else:
                inserted=db().table("products").insert({"name":new_name.strip(),"category":category,"delivery_method":method,"quantity_unit":unit.strip() or None,"max_quantity":max_quantity if max_quantity>0 else None,"active":True}).execute().data
                if inserted:
                    db().table("product_rules").insert({"product_id":inserted[0]["id"],"delivery_area_id":area["id"],"standard_duration_minutes":duration,"min_duration_minutes":min_duration,"max_duration_minutes":max_duration,"duration_mode":"manager_adjustable" if min_duration!=max_duration else "fixed","operators_required":1,"buffer_minutes":None,"permitted_shifts":permitted,"requires_dedicated_bulk_operator":dedicated,"active":True}).execute()
                    st.success("Product added to catalogue and delivery rules."); st.rerun()
    for p in products:
        rule=get_rule(p["id"])
        if not rule: continue
        with st.expander(p["name"]):
            with st.form(f"rule_{p['id']}"):
                active=st.checkbox("Product active",p["active"])
                category=st.selectbox("Category",["ingredient","packaging","sundries"],index=["ingredient","packaging","sundries"].index(p["category"]))
                method=st.selectbox("Delivery method",["tanker","one_tonne_totes","ibc","palletised"],index=["tanker","one_tonne_totes","ibc","palletised"].index(p["delivery_method"]))
                unit=st.text_input("Quantity unit",value=p.get("quantity_unit") or "")
                max_quantity=st.number_input("Maximum quantity (0 = no limit)",0.0,1000.0,float(p["max_quantity"]) if p.get("max_quantity") is not None else 0.0)
                duration=st.number_input("Standard duration (minutes)",15,600,int(rule["standard_duration_minutes"]),step=5)
                minimum=st.number_input("Minimum duration",15,600,int(rule["min_duration_minutes"]),step=5)
                maximum=st.number_input("Maximum duration",15,600,int(rule["max_duration_minutes"]),step=5)
                area=st.selectbox("Delivery area",areas,index=next((i for i,a in enumerate(areas) if a["id"]==rule["delivery_area_id"]),0),format_func=lambda x:x["name"])
                operators=st.number_input("Operators required",1,10,int(rule["operators_required"]))
                permitted=st.multiselect("Permitted shifts",["days","afternoons","nights"],default=rule.get("permitted_shifts") or ["days","afternoons","nights"])
                dedicated=st.checkbox("Requires dedicated bulk inbound operator",bool(rule.get("requires_dedicated_bulk_operator")))
                save=st.form_submit_button("Save product rule")
            if save:
                if minimum>maximum: st.error("Minimum duration cannot exceed maximum.")
                else:
                    db().table("products").update({"active":active,"category":category,"delivery_method":method,"quantity_unit":unit.strip() or None,"max_quantity":max_quantity if max_quantity>0 else None,"updated_at":local_now().isoformat()}).eq("id",p["id"]).execute()
                    db().table("product_rules").update({"standard_duration_minutes":duration,"min_duration_minutes":minimum,"max_duration_minutes":maximum,"duration_mode":"manager_adjustable" if minimum!=maximum else "fixed","delivery_area_id":area["id"],"buffer_minutes":None,"operators_required":operators,"permitted_shifts":permitted,"requires_dedicated_bulk_operator":dedicated,"updated_at":local_now().isoformat()}).eq("id",rule["id"]).execute()
                    st.success("Rule saved."); st.rerun()
    st.markdown("### Delivery area buffers")
    for a in areas:
        with st.form(f"area_{a['id']}"):
            st.write(a["name"])
            buf=st.number_input("Buffer minutes",0,180,int(a["buffer_minutes"]),key=f"buf_{a['id']}")
            save=st.form_submit_button("Save buffer")
        if save: db().table("delivery_areas").update({"buffer_minutes":buf}).eq("id",a["id"]).execute(); st.success("Buffer saved."); st.rerun()

def suppliers_page():
    st.subheader("Supplier management")
    setting=one("app_settings",setting_key="internal_notification_email")
    current_email=setting.get("setting_value") if setting else ""
    if isinstance(current_email,dict): current_email=current_email.get("email","")
    with st.form("internal_notification_email"):
        site_email=st.text_input("Internal booking notification email",value=str(current_email or ""))
        save_email=st.form_submit_button("Save notification address")
    if save_email:
        db().table("app_settings").upsert({"setting_key":"internal_notification_email","setting_value":site_email.strip(),"description":"Recipient for inbound supplier booking and change request notifications."},on_conflict="setting_key").execute()
        st.success("Internal notification address saved."); st.rerun()
    with st.form("supplier_new"):
        code=st.text_input("Supplier code"); name=st.text_input("Supplier name"); email=st.text_input("Contact email"); phone=st.text_input("Phone")
        add=st.form_submit_button("Add supplier")
    if add:
        if not code.strip() or not name.strip(): st.error("Supplier code and name are required.")
        else:
            db().table("suppliers").insert({"supplier_code":code.strip(),"name":name.strip(),"contact_email":email.strip() or None,"phone":phone.strip() or None}).execute()
            st.success("Supplier added."); st.rerun()
    for s in rows("suppliers"):
        with st.expander(f"{s['supplier_code']} · {s['name']}"):
            st.write(f"Contact: {s.get('contact_email') or 'Not set'} · Portal: {'Enabled' if s.get('shared_login_enabled') else 'Disabled'}")
            with st.form(f"supplier_{s['id']}"):
                email=st.text_input("Contact email",s.get("contact_email") or "")
                active=st.checkbox("Supplier active",bool(s.get("active")))
                enabled=st.checkbox("Supplier portal login enabled",bool(s.get("shared_login_enabled")))
                password=st.text_input("Set / reset supplier password (12+ characters)",type="password")
                save=st.form_submit_button("Save supplier")
            if save:
                patch={"contact_email":email.strip() or None,"active":active,"shared_login_enabled":enabled and active,"updated_at":local_now().isoformat()}
                if password:
                    if len(password)<12: st.error("Password must be at least 12 characters."); continue
                    patch["supplier_password_hash"]=pw_hash(password); patch["supplier_password_changed_at"]=local_now().isoformat()
                db().table("suppliers").update(patch).eq("id",s["id"]).execute(); st.success("Supplier saved. Historical POs and bookings are retained."); st.rerun()

def supplier_portal():
    st.subheader("Supplier portal")
    if not st.session_state.get("supplier_id"):
        with st.form("supplier_signin"):
            code=st.text_input("Supplier code")
            password=st.text_input("Supplier portal password",type="password")
            go=st.form_submit_button("Sign in",type="primary")
        if go:
            s=one("suppliers",supplier_code=code.strip())
            if s and s.get("active") and s.get("shared_login_enabled") and s.get("supplier_password_hash") and pw_ok(password,s["supplier_password_hash"]):
                st.session_state.pop("auth_role",None)
                st.session_state["supplier_id"]=s["id"]; st.rerun()
            else: st.error("Supplier sign-in failed. Contact the Pontefract site team.")
        return
    s=one("suppliers",id=st.session_state["supplier_id"])
    if not s or not s.get("active") or not s.get("shared_login_enabled"):
        st.session_state.pop("supplier_id",None); st.error("Supplier access is no longer enabled."); st.rerun()
    st.write(f"Signed in as **{s['name']}**")
    if st.button("Supplier sign out"):
        st.session_state.pop("supplier_id",None); st.session_state.pop("auth_role",None); st.rerun()
    tabs=st.tabs(["Book a delivery","My bookings","Purchase orders"])
    with tabs[0]:
        orders=[x for x in rows("purchase_orders") if x["supplier_id"]==s["id"] and x.get("booking_permitted") and x.get("status")=="open"]
        if not orders: st.info("There are no open purchase orders available to book.")
        else:
            with st.form("supplier_request"):
                po=st.selectbox("Purchase order",orders,format_func=lambda x:x["po_number"])
                product=one("products",id=po["product_id"])
                st.write(f"Product: {product['name'] if product else '—'} · Quantity on PO: {po.get('quantity') or 'Not specified'}")
                day=st.date_input("Requested delivery date",value=max(local_now().date()+timedelta(days=7),date.fromisoformat(po["due_date"]) if po.get("due_date") else local_now().date()+timedelta(days=7)))
                tm=st.time_input("Preferred arrival time",value=time(8,0))
                qty=st.number_input("Delivery quantity",min_value=0.0,value=float(po.get("quantity") or 0))
                note=st.text_input("Additional information")
                go=st.form_submit_button("Request delivery booking",type="primary")
            if go:
                start=datetime.combine(day,tm,TZ)
                if start<local_now()+timedelta(days=7): st.error("Supplier bookings must be requested at least seven days in advance.")
                elif po.get("latest_delivery_date") and day>date.fromisoformat(po["latest_delivery_date"]): st.error("Requested date is after the latest permitted delivery date.")
                elif product is None or not product["active"]: st.error("This product is not currently bookable.")
                else:
                    try:
                        b=create_booking(s["id"],product["id"],start,int(get_rule(product["id"])["standard_duration_minutes"]),qty,product.get("quantity_unit"),po["id"],"supplier_portal","requested",note)
                        queue_internal_email("Inbound booking request awaiting approval","booking_request",{"booking_reference":b["booking_reference"],"supplier":s["name"],"product":product["name"],"scheduled_start":b["scheduled_start"]},booking_id=b["id"])
                        st.success(f"Request {b['booking_reference']} submitted. It is pending site approval.")
                    except Exception as e: st.error(str(e))
    with tabs[1]:
        mine=[x for x in rows("bookings") if x["supplier_id"]==s["id"]]
        if mine:
            out=[]
            for b in mine:
                p=one("products",id=b["product_id"])
                po=one("purchase_orders",id=b["purchase_order_id"]) if b.get("purchase_order_id") else None
                out.append({"Reference":b["booking_reference"],"PO":po["po_number"] if po else "Manual","Product":p["name"] if p else "","Arrival":fmt(b["scheduled_start"]),"Status":b["status"].replace("_"," ").title()})
            st.dataframe(pd.DataFrame(out),use_container_width=True,hide_index=True)
            for proposed in [x for x in mine if x.get("status")=="alternative_proposed"]:
                with st.container(border=True):
                    st.write(f"**Alternative proposed for {proposed['booking_reference']}** · {fmt(proposed['scheduled_start'])}")
                    accept_col,decline_col=st.columns(2)
                    if accept_col.button("Accept alternative",key=f"supplier_accept_{proposed['id']}"):
                        db().table("bookings").update({"status":"confirmed","confirmed_at":local_now().isoformat(),"updated_at":local_now().isoformat()}).eq("id",proposed["id"]).eq("supplier_id",s["id"]).eq("status","alternative_proposed").execute()
                        log_event(proposed["id"],"supplier_accepted_alternative","alternative_proposed","confirmed")
                        queue_internal_email("Supplier accepted proposed delivery time","alternative_accepted",{"booking_reference":proposed["booking_reference"],"supplier":s["name"]},booking_id=proposed["id"])
                        st.success("Alternative accepted; booking confirmed."); st.rerun()
                    if decline_col.button("Decline alternative",key=f"supplier_decline_{proposed['id']}"):
                        db().table("bookings").update({"status":"rejected","updated_at":local_now().isoformat()}).eq("id",proposed["id"]).eq("supplier_id",s["id"]).eq("status","alternative_proposed").execute()
                        log_event(proposed["id"],"supplier_declined_alternative","alternative_proposed","rejected")
                        queue_internal_email("Supplier declined proposed delivery time","alternative_declined",{"booking_reference":proposed["booking_reference"],"supplier":s["name"]},booking_id=proposed["id"])
                        st.success("Alternative declined; request closed."); st.rerun()
            eligible=[]
            for b in mine:
                try: future=datetime.fromisoformat(b["scheduled_start"].replace("Z","+00:00")).astimezone(TZ)>local_now()+timedelta(hours=48)
                except Exception: future=False
                if future and b["status"]=="confirmed": eligible.append(b)
            if eligible:
                with st.form("supplier_change_request"):
                    chosen=st.selectbox("Booking to amend or cancel",eligible,format_func=lambda x:f"{x['booking_reference']} · {fmt(x['scheduled_start'])}")
                    action=st.radio("Request type",["Change arrival time","Cancel booking"])
                    new_day=st.date_input("New date",value=local_now().date()+timedelta(days=7))
                    new_time=st.time_input("New arrival time",value=time(8,0))
                    reason=st.text_input("Reason / message for site team")
                    submit=st.form_submit_button("Submit request")
                if submit:
                    if not reason.strip(): st.error("Please enter a reason.")
                    elif action=="Change arrival time" and datetime.combine(new_day,new_time,TZ)<local_now()+timedelta(days=7): st.error("Amended bookings must still be at least seven days ahead.")
                    else:
                        if action=="Cancel booking":
                            proposed_start=chosen["scheduled_start"]; proposed_end=chosen["scheduled_end"]; proposed_duration=chosen["duration_minutes"]
                            reason_text="CANCEL_REQUEST: "+reason
                        else:
                            proposed_start=datetime.combine(new_day,new_time,TZ).isoformat()
                            proposed_end=(datetime.fromisoformat(proposed_start)+timedelta(minutes=chosen["duration_minutes"])).isoformat()
                            proposed_duration=chosen["duration_minutes"]; reason_text=reason
                        change=db().table("booking_change_requests").insert({"booking_id":chosen["id"],"status":"pending","proposed_start":proposed_start,"proposed_end":proposed_end,"proposed_duration_minutes":proposed_duration,"reason":reason_text}).execute().data
                        queue_internal_email("Supplier booking change awaiting approval","booking_change_request",{"booking_reference":chosen["booking_reference"],"supplier":s["name"],"reason":reason_text,"proposed_start":proposed_start},booking_id=chosen["id"],change_request_id=change[0]["id"] if change else None)
                        st.success("Request sent to the site team. Your existing confirmed booking remains in place until approved.")
            else: st.caption("Only confirmed bookings more than 48 hours away can be amended or cancelled here.")
        else: st.info("No bookings yet.")
    with tabs[2]:
        own=[x for x in rows("purchase_orders") if x["supplier_id"]==s["id"]]
        st.dataframe(pd.DataFrame([{"PO":x["po_number"],"Product":(one("products",id=x["product_id"]) or {}).get("name"),"Quantity":x.get("quantity"),"Due":x.get("due_date"),"Latest":x.get("latest_delivery_date"),"Bookable":x.get("booking_permitted")} for x in own]),use_container_width=True,hide_index=True)

def reports_page():
    st.subheader("Reports & audit")
    bookings=rows("bookings")
    if bookings:
        out=[]
        for b in bookings:
            s=one("suppliers",id=b["supplier_id"]); p=one("products",id=b["product_id"])
            out.append({"Reference":b["booking_reference"],"Supplier":s["name"] if s else "","Product":p["name"] if p else "","Start":fmt(b["scheduled_start"]),"Status":b["status"],"Source":b["source"],"Override":b["capacity_override"]})
        frame=pd.DataFrame(out); st.dataframe(frame,use_container_width=True,hide_index=True)
        st.download_button("Download booking report CSV",frame.to_csv(index=False).encode("utf-8"),"inbound-bookings.csv","text/csv")
    events=rows("booking_events")
    if events:
        st.markdown("### Booking event history")
        st.dataframe(pd.DataFrame(events).sort_values("created_at",ascending=False),use_container_width=True,hide_index=True)
    outbox=rows("email_outbox")
    st.markdown("### Supplier email notification queue")
    if outbox:
        st.dataframe(pd.DataFrame([{"To":x["recipient_email"],"Subject":x["subject"],"Template":x["template_key"],"Status":x["status"],"Attempts":x["attempt_count"],"Created":fmt(x["created_at"])} for x in outbox]),use_container_width=True,hide_index=True)
        st.caption("Messages are queued in Supabase. Delivery is not active until the inbound app's Gmail sending credentials / workflow are connected.")
    else: st.info("No supplier notification emails have been queued yet.")

def main():
    st.markdown(f"""<style>
    /* Consistent high-contrast page and card styling */
    .stApp,[data-testid="stAppViewContainer"]{{background:#f3f4f6;color:#1f2937}}
    [data-testid="stMain"]{{color:#1f2937}}
    [data-testid="stMarkdownContainer"],[data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li,[data-testid="stText"],label,
    [data-testid="stCaptionContainer"]{{color:#1f2937}}
    [data-testid="stVerticalBlockBorderWrapper"],[data-testid="stExpander"],
    [data-testid="stForm"],[data-testid="stMetric"]{{background:#fff!important;color:#1f2937!important;border:1px solid #d1d5db!important;border-radius:.65rem}}
    [data-testid="stVerticalBlockBorderWrapper"]{{padding:.35rem}}
    [data-testid="stExpander"] details,[data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary p{{color:#1f2937!important}}
    [data-testid="stMetric"] label,[data-testid="stMetricValue"],[data-testid="stMetricDelta"]{{color:#111827!important}}
    [data-baseweb="input"],[data-baseweb="select"],[data-baseweb="textarea"],
    [data-testid="stDateInput"] input,[data-testid="stTimeInput"] input{{background:#fff!important;color:#111827!important}}
    [data-testid="stDataFrame"]{{border:1px solid #d1d5db;border-radius:.5rem}}
    [data-testid="stSidebar"]{{background:#252525}}
    [data-testid="stSidebar"] *{{color:#fff!important}}
    [data-testid="stSidebar"] pre,[data-testid="stSidebar"] code,
    [data-testid="stSidebar"] [data-testid="stCode"]{{display:none!important}}
    [data-testid="stSidebar"] [data-testid="stRadio"] label{{color:#fff!important}}
    .kp-header{{background:{KP_RED};color:#fff;padding:1.2rem 1.5rem;border-radius:.5rem;margin-bottom:1.2rem}}
    .kp-header h1{{color:#fff;margin:0;font-size:1.8rem}} .kp-header p{{color:#fff;margin:.35rem 0 0}}
    </style>""",unsafe_allow_html=True)
    st.markdown('<div class="kp-header"><h1>KP Snacks Pontefract Inbound Delivery Management</h1><p>Supplier bookings · Warehouse capacity · Purchase orders</p></div>',unsafe_allow_html=True)
    try:
        db().table("app_settings").select("setting_key").limit(1).execute()
        connected=True
    except Exception as e:
        connected=False; st.error(f"Inbound database connection failed ({type(e).__name__}). Check Streamlit Secrets.")
    role=st.session_state.get("auth_role")
    with st.sidebar:
        st.markdown("## KP Snacks")
        st.markdown("#### NAVIGATION")
        st.caption("● Database connected" if connected else "● Database disconnected")
        if role:
            st.markdown(f"#### {ROLE_LABELS.get(role,role)} ACCESS")
            if st.button("Sign out",use_container_width=True):
                st.session_state.pop("auth_role",None); st.session_state.pop("supplier_id",None); st.rerun()
            choices=[x for x in PAGES if x in ROLE_PAGES.get(role,set())]
        else: choices=["Dashboard","Delivery calendar"]
        page=st.radio("Navigation",choices)
    if not connected: return
    if page=="Dashboard": dashboard(public=not bool(role))
    elif page=="Delivery calendar": calendar_page(public=not bool(role))
    elif not role:
        st.info("Sign in below for internal operational access.")
        if credential("administrator"): sign_in()
        else: bootstrap()
    elif page=="Booking management": booking_management()
    elif page=="Delivery arrivals": arrival_page(db(), role)
    elif page=="Orders & Excel upload": orders_page()
    elif page=="Warehouse resources": warehouse_page()
    elif page=="Products & delivery rules": rules_page()
    elif page=="Suppliers": suppliers_page()
    elif page=="Reports":
        reports_page()
        st.divider()
        supplier_performance_page(db())
    elif page=="User & role management":
        if role!="administrator": st.error("Administrator access required.")
        else: save_role_access()
    if not role and page in ("Dashboard","Delivery calendar"):
        st.divider()
        if credential("administrator"): sign_in()
        else: bootstrap()
    st.divider()
    st.caption("KP Snacks Pontefract · Inbound Delivery Management")

if __name__=="__main__":
    main()

import hashlib
import hmac
import secrets
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from supabase import Client, create_client

st.set_page_config(page_title="KP Snacks | Pontefract Inbound", page_icon="📦", layout="wide")
TZ = ZoneInfo("Europe/London")
KP_RED = "#C8102E"
ROLES = ["supervisor", "material_controller", "warehouse_manager", "administrator"]
ROLE_LABELS = {"supervisor":"Supervisor","material_controller":"Material Controller","warehouse_manager":"Warehouse Manager","administrator":"Administrator"}
PAGES = ["Dashboard","Delivery calendar","Booking management","Orders & Excel upload","Warehouse resources","Products & delivery rules","Suppliers","Reports","User & role management"]
ROLE_PAGES = {
    "supervisor": set(PAGES[:3]),
    "material_controller": set(PAGES[:3] + ["Orders & Excel upload","Products & delivery rules"]),
    "warehouse_manager": set(PAGES[:3] + ["Warehouse resources"]),
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
    return True, "Area is available. Staffing must still be checked by the shift manager."

def create_booking(supplier_id, product_id, start, duration, quantity=None, unit=None, po_id=None, source="manual", status="requested", notes="", override=False, override_reason=""):
    rule=get_rule(product_id)
    if not rule: raise ValueError("This product has no active delivery rule.")
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
    pending=[b for b in rows("bookings") if b.get("status") in ("requested","alternative_proposed")]
    if not pending: st.info("There are no booking requests awaiting a decision.")
    for b in pending:
        product=one("products",id=b["product_id"]); supplier=one("suppliers",id=b["supplier_id"])
        with st.container(border=True):
            st.markdown(f"**{b['booking_reference']} · {supplier['name'] if supplier else 'Supplier'} · {product['name'] if product else 'Product'}**")
            st.write(f"{fmt(b['scheduled_start'])} · {b['duration_minutes']} minutes · {b['status'].replace('_',' ').title()}")
            c1,c2,c3=st.columns(3)
            if c1.button("Approve / confirm",key=f"approve_{b['id']}"):
                db().table("bookings").update({"status":"confirmed","confirmed_at":local_now().isoformat(),"updated_at":local_now().isoformat()}).eq("id",b["id"]).execute()
                log_event(b["id"],"booking_confirmed",b["status"],"confirmed")
                enqueue_status_email(b,"confirmed"); st.rerun()
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
            reason=st.text_input("Message to supplier")
            go=st.form_submit_button("Send alternative proposal")
        if go:
            start=datetime.combine(d,t,TZ)
            db().table("bookings").update({"status":"alternative_proposed","scheduled_start":start.isoformat(),"scheduled_end":(start+timedelta(minutes=b["duration_minutes"])).isoformat(),"internal_notes":reason,"updated_at":local_now().isoformat()}).eq("id",b["id"]).execute()
            log_event(b["id"],"alternative_proposed",b["status"],"alternative_proposed",reason); enqueue_status_email(b,"alternative_proposed")
            st.session_state.pop("alt_id"); st.rerun()
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
                is_cancel=(ch.get("reason") or "").startswith("CANCEL_REQUEST:")
                if is_cancel:
                    db().table("bookings").update({"status":"cancelled","cancelled_at":local_now().isoformat(),"updated_at":local_now().isoformat()}).eq("id",b["id"]).execute()
                    log_event(b["id"],"supplier_cancellation_approved",b["status"],"cancelled",ch.get("reason"))
                    enqueue_status_email(b,"cancelled")
                else:
                    area=one("delivery_areas",id=b["delivery_area_id"])
                    proposed=datetime.fromisoformat(ch["proposed_start"].replace("Z","+00:00")).astimezone(TZ)
                    ok,msg=capacity_check(b["delivery_area_id"],proposed,int(ch["proposed_duration_minutes"]),int(b.get("buffer_minutes") or (area or {}).get("buffer_minutes",20)),ignore_id=b["id"])
                    if not ok:
                        st.error("Cannot approve this amendment: "+msg)
                        continue
                    db().table("bookings").update({"scheduled_start":ch["proposed_start"],"scheduled_end":ch["proposed_end"],"duration_minutes":ch["proposed_duration_minutes"],"updated_at":local_now().isoformat()}).eq("id",b["id"]).execute()
                    log_event(b["id"],"supplier_amendment_approved",b["status"],b["status"],ch.get("reason"))
                    enqueue_status_email(b,"confirmed")
                db().table("booking_change_requests").update({"status":"approved","reviewed_at":local_now().isoformat(),"manager_response":"Approved"}).eq("id",ch["id"]).execute()
                st.rerun()
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
            st.success(f"Created {b['booking_reference']}"); st.rerun()
        except Exception as e: st.error(str(e))

def orders_page():
    st.subheader("Orders & Excel upload")
    st.write("Upload an Excel workbook with supplier_code, po_number, product, delivery_type, quantity, due_date, latest_delivery_date and booking_permitted.")
    up=st.file_uploader("Purchase order workbook",type=["xlsx","xls"])
    if up:
        try:
            incoming=pd.read_excel(up)
            st.dataframe(incoming,use_container_width=True)
            required={"supplier_code","po_number","product"}
            missing=required-set(incoming.columns)
            if missing: st.error("Missing columns: "+", ".join(sorted(missing)))
            elif st.button("Validate and import purchase orders",type="primary"):
                suppliers={x["supplier_code"]:x for x in rows("suppliers")}
                products={x["name"].lower():x for x in rows("products")}
                imported=0; issues=[]
                for idx,row in incoming.iterrows():
                    code=str(row.get("supplier_code","")).strip()
                    po=str(row.get("po_number","")).strip()
                    prod=products.get(str(row.get("product","")).strip().lower())
                    sup=suppliers.get(code)
                    if not sup or not po or not prod:
                        issues.append(f"Row {idx+2}: supplier, PO number or product could not be matched."); continue
                    def val(k):
                        v=row.get(k)
                        if pd.isna(v): return None
                        if k in ("due_date","latest_delivery_date"): return pd.to_datetime(v).date().isoformat()
                        return v
                    payload={"supplier_id":sup["id"],"po_number":po,"product_id":prod["id"],"delivery_type":None if pd.isna(row.get("delivery_type")) else str(row.get("delivery_type")),"quantity":val("quantity"),"quantity_unit":prod.get("quantity_unit"),"due_date":val("due_date"),"latest_delivery_date":val("latest_delivery_date"),"booking_permitted":bool(row.get("booking_permitted",True)),"source_file_name":up.name,"status":"open"}
                    try:
                        db().table("purchase_orders").upsert(payload,on_conflict="supplier_id,po_number,product_id").execute(); imported+=1
                    except Exception as ex: issues.append(f"Row {idx+2}: {type(ex).__name__}")
                st.success(f"Imported or updated {imported} purchase orders.")
                if issues: st.warning("\\n".join(issues))
        except Exception as e: st.error(f"Could not read workbook: {e}")
    st.markdown("### Current purchase orders")
    orders=rows("purchase_orders")
    out=[]
    for x in orders:
        s=one("suppliers",id=x["supplier_id"]); p=one("products",id=x["product_id"])
        out.append({"Supplier":s["name"] if s else "","PO":x["po_number"],"Product":p["name"] if p else "","Quantity":x.get("quantity"),"Due":x.get("due_date"),"Latest":x.get("latest_delivery_date"),"Booking allowed":x.get("booking_permitted")})
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
            holiday=st.number_input("People on holiday / absent",0,30,int(existing["people_on_holiday"]) if existing else 0)
            extra=st.checkbox("Additional operator available",bool(existing["additional_operator_available"]) if existing else False)
            bulk=st.checkbox("Dedicated bulk operator available",bool(existing["bulk_operator_available"]) if existing else (shift["shift_code"]=="days"))
            note=st.text_input("Notes",value=existing.get("notes","") if existing else "")
            save=st.form_submit_button("Save shift plan")
        if save:
            payload={"plan_date":day.isoformat(),"shift_id":shift["id"],"main_team_headcount":head,"people_on_holiday":holiday,"additional_operator_available":extra,"additional_operator_count":1 if extra else 0,"bulk_operator_available":bulk,"protected_outbound_headcount":2,"notes":note}
            db().table("staffing_plans").upsert(payload,on_conflict="plan_date,shift_id").execute()
            st.success(f"{shift['display_name']} staffing saved."); st.rerun()
    st.caption("Capacity planning protects two people for outbound/palletisation. The weekly rotation anchor and individual staff names still need to be configured before automatic operator allocation is enabled.")
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
        with st.form("absence"):
            typ=st.selectbox("Assignment type",["holiday","absence","training","other"])
            start=st.date_input("From",value=day); end=st.date_input("To",value=day)
            note=st.text_input("Notes")
            save=st.form_submit_button("Save absence")
        if save:
            if end<start: st.error("End date must be on or after start date.")
            else:
                shift=shifts[0]
                db().table("staff_shift_assignments").insert({"staff_member_id":selected["id"],"shift_id":shift["id"],"starts_at":datetime.combine(start,time(0),TZ).isoformat(),"ends_at":datetime.combine(end+timedelta(days=1),time(0),TZ).isoformat(),"assignment_type":typ,"notes":note}).execute()
                st.success("Absence record saved."); st.rerun()

def rules_page():
    st.subheader("Products & delivery rules")
    products=rows("products"); areas=rows("delivery_areas")
    for p in products:
        rule=get_rule(p["id"])
        if not rule: continue
        with st.expander(p["name"]):
            with st.form(f"rule_{p['id']}"):
                active=st.checkbox("Product active",p["active"])
                duration=st.number_input("Standard duration (minutes)",15,600,int(rule["standard_duration_minutes"]),step=5)
                minimum=st.number_input("Minimum duration",15,600,int(rule["min_duration_minutes"]),step=5)
                maximum=st.number_input("Maximum duration",15,600,int(rule["max_duration_minutes"]),step=5)
                area=st.selectbox("Delivery area",areas,index=next((i for i,a in enumerate(areas) if a["id"]==rule["delivery_area_id"]),0),format_func=lambda x:x["name"])
                buffer=st.number_input("Buffer (minutes)",0,180,int(rule["buffer_minutes"] if rule["buffer_minutes"] is not None else 20),step=5)
                operators=st.number_input("Operators required",1,10,int(rule["operators_required"]))
                save=st.form_submit_button("Save product rule")
            if save:
                if minimum>maximum: st.error("Minimum duration cannot exceed maximum.")
                else:
                    db().table("products").update({"active":active,"updated_at":local_now().isoformat()}).eq("id",p["id"]).execute()
                    db().table("product_rules").update({"standard_duration_minutes":duration,"min_duration_minutes":minimum,"max_duration_minutes":maximum,"duration_mode":"manager_adjustable" if minimum!=maximum else "fixed","delivery_area_id":area["id"],"buffer_minutes":buffer,"operators_required":operators,"updated_at":local_now().isoformat()}).eq("id",rule["id"]).execute()
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
                enabled=st.checkbox("Supplier portal enabled",bool(s.get("shared_login_enabled")))
                password=st.text_input("Set / reset supplier password (12+ characters)",type="password")
                save=st.form_submit_button("Save supplier")
            if save:
                patch={"contact_email":email.strip() or None,"shared_login_enabled":enabled}
                if password:
                    if len(password)<12: st.error("Password must be at least 12 characters."); continue
                    patch["supplier_password_hash"]=pw_hash(password); patch["supplier_password_changed_at"]=local_now().isoformat()
                db().table("suppliers").update(patch).eq("id",s["id"]).execute(); st.success("Supplier saved."); st.rerun()

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
                st.session_state["supplier_id"]=s["id"]; st.rerun()
            else: st.error("Supplier sign-in failed. Contact the Pontefract site team.")
        return
    s=one("suppliers",id=st.session_state["supplier_id"])
    if not s or not s.get("active") or not s.get("shared_login_enabled"):
        st.session_state.pop("supplier_id",None); st.error("Supplier access is no longer enabled."); st.rerun()
    st.write(f"Signed in as **{s['name']}**")
    if st.button("Supplier sign out"): st.session_state.pop("supplier_id",None); st.rerun()
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
                        db().table("booking_change_requests").insert({"booking_id":chosen["id"],"status":"pending","proposed_start":proposed_start,"proposed_end":proposed_end,"proposed_duration_minutes":proposed_duration,"reason":reason_text}).execute()
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

def main():
    st.markdown(f"""<style>
    .stApp{{background:#f5f5f5}} [data-testid="stSidebar"]{{background:#292929}}
    [data-testid="stSidebar"] *{{color:#fff!important}}
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
        st.caption("Pontefract site · Inbound")
        st.success("Database connected") if connected else st.error("Database disconnected")
        if role:
            st.write(f"Signed in: **{ROLE_LABELS.get(role,role)}**")
            if st.button("Sign out",use_container_width=True): st.session_state.pop("auth_role",None); st.rerun()
            choices=[x for x in PAGES if x in ROLE_PAGES.get(role,set())]
        else: choices=["Dashboard","Delivery calendar","Supplier portal"]
        page=st.radio("Navigation",choices)
    if not connected: return
    if page=="Dashboard": dashboard(public=not bool(role))
    elif page=="Delivery calendar": calendar_page(public=not bool(role))
    elif page=="Supplier portal": supplier_portal()
    elif not role:
        st.info("Sign in below for internal operational access.")
        if credential("administrator"): sign_in()
        else: bootstrap()
    elif page=="Booking management": booking_management()
    elif page=="Orders & Excel upload": orders_page()
    elif page=="Warehouse resources": warehouse_page()
    elif page=="Products & delivery rules": rules_page()
    elif page=="Suppliers": suppliers_page()
    elif page=="Reports": reports_page()
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

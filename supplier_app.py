import hashlib
import hmac
import smtplib
import ssl
from email.message import EmailMessage
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from delivery_features import render_documents
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from supabase import Client, create_client

st.set_page_config(page_title="KP Snacks | Supplier Inbound Portal",page_icon="📦",layout="wide")
TZ=ZoneInfo("Europe/London")
KP_RED="#C8102E"

@st.cache_resource
def db() -> Client:
    cfg=st.secrets["supabase"]
    return create_client(cfg["url"],cfg["key"])

def one(table,**filters):
    q=db().table(table).select("*")
    for k,v in filters.items(): q=q.eq(k,v)
    found=q.limit(1).execute().data or []
    return found[0] if found else None

def rows_for_supplier(table,supplier_id):
    return db().table(table).select("*").eq("supplier_id",supplier_id).execute().data or []

def password_ok(value,encoded):
    try:
        algorithm,n,r,p,salt,digest=encoded.split("$")
        if algorithm!="scrypt": return False
        actual=hashlib.scrypt(value.encode(),salt=bytes.fromhex(salt),n=int(n),r=int(r),p=int(p),dklen=len(bytes.fromhex(digest))).hex()
        return hmac.compare_digest(actual,digest)
    except Exception:
        return False

def now():
    return datetime.now(TZ)

def fmt(value):
    try: return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(TZ).strftime("%d/%m/%Y %H:%M")
    except Exception: return str(value)

def queue_internal(subject,template,payload,booking_id=None,change_request_id=None):
    setting=one("app_settings",setting_key="internal_notification_email")
    address=setting.get("setting_value") if setting else None
    if isinstance(address,dict): address=address.get("email")
    if address:
        db().table("email_outbox").insert({"booking_id":booking_id,"change_request_id":change_request_id,"recipient_email":str(address),"subject":subject,"template_key":template,"payload":payload}).execute()

def dispatch_email_outbox(limit=10):
    """Deliver queued notifications using the Gmail App Password in Streamlit secrets."""
    try:
        password=str(st.secrets["gmail_password"]).strip()
    except Exception:
        return 0
    if not password:
        return 0
    sender="Kp.ponte.csv@gmail.com"
    try:
        pending=db().table("email_outbox").select("*").eq("status","queued").order("created_at").limit(limit).execute().data or []
    except Exception:
        return 0
    sent=0
    for item in pending:
        message=EmailMessage()
        message["From"]=sender
        message["To"]=item["recipient_email"]
        message["Subject"]=item["subject"]
        payload=item.get("payload") or {}
        labels=(("booking_reference","Booking reference"),("supplier","Supplier"),("product","Product"),("status","Status"),("scheduled_start","Scheduled arrival"),("proposed_start","Proposed arrival"),("reason","Details"))
        details=[f"{label}: {payload[key]}" for key,label in labels if payload.get(key)]
        message.set_content("KP Snacks Pontefract inbound delivery notification\n\n"+("\n".join(details) if details else "There is an update in the inbound delivery management system.")+"\n\nThis is an automated notification. Please use the inbound delivery management app for the latest information.")
        try:
            with smtplib.SMTP("smtp.gmail.com",587,timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(sender,password)
                smtp.send_message(message)
            db().table("email_outbox").update({"status":"sent","sent_at":now().isoformat(),"attempt_count":int(item.get("attempt_count") or 0)+1,"last_error":None}).eq("id",item["id"]).eq("status","queued").execute()
            sent+=1
        except Exception as exc:
            db().table("email_outbox").update({"status":"failed","attempt_count":int(item.get("attempt_count") or 0)+1,"last_error":str(exc)[:1000]}).eq("id",item["id"]).eq("status","queued").execute()
    return sent

def log_supplier_event(booking_id,event,old_status,new_status,reason=None):
    db().table("booking_events").insert({"booking_id":booking_id,"event_type":event,"previous_status":old_status,"new_status":new_status,"actor_label":"supplier","reason":reason,"details":{}}).execute()

def request_booking(supplier,po,product,start,quantity,note):
    if start<now()+timedelta(days=7): raise ValueError("Bookings must be requested at least seven days in advance.")
    if po.get("latest_delivery_date") and start.date()>date.fromisoformat(po["latest_delivery_date"]):
        raise ValueError("The requested date is after the latest permitted delivery date on this purchase order.")
    if po.get("quantity") is not None:
        prior=rows_for_supplier("bookings",supplier["id"])
        used=sum(float(x.get("quantity") or 0) for x in prior if x.get("purchase_order_id")==po["id"] and x.get("status") in ("requested","alternative_proposed","confirmed","completed"))
        remaining=max(0.0,float(po["quantity"])-used)
        if quantity>remaining: raise ValueError(f"Only {remaining:g} {po.get('quantity_unit') or ''} remain available on this purchase order.")
    rule=one("product_rules",product_id=product["id"])
    if not rule or not rule.get("active"): raise ValueError("This product is not currently bookable.")
    area=one("delivery_areas",id=rule["delivery_area_id"])
    if not area or not area.get("active"): raise ValueError("The unloading area is unavailable.")
    if product.get("max_quantity") is not None and quantity>float(product["max_quantity"]):
        raise ValueError(f"Maximum quantity is {product['max_quantity']} {product.get('quantity_unit') or ''}.")
    duration=int(rule["standard_duration_minutes"])
    buffer=int(rule["buffer_minutes"] if rule.get("buffer_minutes") is not None else area.get("buffer_minutes",20))
    end=start+timedelta(minutes=duration)
    reference="IN-"+now().strftime("%y%m%d")+"-"+__import__("secrets").token_hex(3).upper()
    payload={"booking_reference":reference,"supplier_id":supplier["id"],"purchase_order_id":po["id"],"product_id":product["id"],"delivery_area_id":area["id"],"status":"requested","source":"supplier_portal","scheduled_start":start.isoformat(),"scheduled_end":end.isoformat(),"duration_minutes":duration,"buffer_minutes":buffer,"quantity":quantity,"quantity_unit":product.get("quantity_unit"),"operators_required":int(rule.get("operators_required",1)),"capacity_override":False,"supplier_notes":note or None}
    result=db().rpc("create_inbound_booking",{"p_booking":payload}).execute().data
    booking=result if isinstance(result,dict) else (result[0] if result else None)
    if not booking: raise RuntimeError("The booking request could not be saved.")
    queue_internal("Inbound booking request awaiting approval","booking_request",{"booking_reference":booking["booking_reference"],"supplier":supplier["name"],"product":product["name"],"scheduled_start":booking["scheduled_start"]},booking_id=booking["id"])
    return booking

def sign_in():
    st.caption('Use the shared supplier login by default. Individual contacts can sign in with their email when enabled by the site team.')
    with st.form('supplier_login'):
        code=st.text_input('Supplier code')
        email=st.text_input('Contact email (optional)')
        password=st.text_input('Supplier portal password',type='password')
        submit=st.form_submit_button('Sign in',type='primary')
    if submit:
        supplier=one('suppliers',supplier_code=code.strip())
        valid=False
        contacts=[]
        if supplier and supplier.get('active'):
            if email.strip():
                contacts=db().table('supplier_contacts').select('*').eq('supplier_id',supplier['id']).eq('email',email.strip()).eq('active',True).limit(1).execute().data or []
                valid=bool(contacts and contacts[0].get('password_hash') and password_ok(password,contacts[0]['password_hash']))
            elif supplier.get('shared_login_enabled') and supplier.get('supplier_password_hash'):
                valid=password_ok(password,supplier['supplier_password_hash'])
        if valid:
            st.session_state['supplier_id']=supplier['id']
            st.session_state['supplier_contact_id']=contacts[0]['id'] if email.strip() else None
            st.session_state['supplier_contact_name']=contacts[0]['full_name'] if email.strip() else supplier['name']
            st.rerun()
        else:
            st.error('Sign-in failed. Check your supplier code, email and password or contact the Pontefract site team.')

def portal(supplier):
    st.write(f"Signed in as **{supplier['name']}**")
    if st.button("Sign out"):
        st.session_state.pop("supplier_id",None)
        st.rerun()
    book_tab,bookings_tab,po_tab=st.tabs(["Book a delivery","My bookings","Purchase orders"])
    with book_tab:
        orders=[x for x in rows_for_supplier("purchase_orders",supplier["id"]) if x.get("booking_permitted") and x.get("status")=="open"]
        if not orders:
            st.info("There are no open purchase orders available to book. Contact the Pontefract site team if you think this is incorrect.")
        else:
            products={x["id"]:x for x in (db().table("products").select("*").eq("active",True).execute().data or [])}
            available=[x for x in orders if x["product_id"] in products]
            if not available:
                st.info("There are no currently bookable products on your open purchase orders.")
            else:
                with st.form("supplier_booking_request"):
                    po=st.selectbox("Purchase order",available,format_func=lambda x:x["po_number"])
                    product=products.get(po["product_id"])
                    st.write(f"**Product:** {product['name'] if product else '—'}")
                    selected_rule=one("product_rules",product_id=po["product_id"])
                    selected_area=one("delivery_areas",id=selected_rule["delivery_area_id"]) if selected_rule else None
                    unload_minutes=int(selected_rule.get("standard_duration_minutes",60)) if selected_rule else 60
                    buffer_minutes=int(selected_rule.get("buffer_minutes") if selected_rule and selected_rule.get("buffer_minutes") is not None else (selected_area or {}).get("buffer_minutes",20))
                    st.caption(f"Planned offload: {unload_minutes} minutes, plus {buffer_minutes} minutes clearance buffer. The full period must fit within staffed shift hours.")
                    st.write(f"**PO quantity:** {po.get('quantity') if po.get('quantity') is not None else 'Not specified'} {po.get('quantity_unit') or ''}")
                    earliest=now().date()+timedelta(days=7)
                    if po.get("due_date"):
                        earliest=max(earliest,date.fromisoformat(po["due_date"]))
                    delivery_day=st.date_input("Requested delivery date",value=earliest,min_value=now().date()+timedelta(days=7))
                    arrival=st.time_input("Preferred arrival time",value=time(8,0))
                    quantity=st.number_input("Delivery quantity",min_value=0.0,value=float(po.get("quantity") or 0.0))
                    note=st.text_area("Additional information for the site team",height=80)
                    submit=st.form_submit_button("Request delivery booking",type="primary")
                if submit:
                    start=datetime.combine(delivery_day,arrival,TZ)
                    if start<now()+timedelta(days=7):
                        st.error("Bookings must be requested at least seven days in advance.")
                    elif po.get("latest_delivery_date") and delivery_day>date.fromisoformat(po["latest_delivery_date"]):
                        st.error("That date is after the latest permitted delivery date on this purchase order.")
                    elif quantity<=0:
                        st.error("Enter a delivery quantity greater than zero.")
                    else:
                        try:
                            booking=request_booking(supplier,po,product,start,quantity,note)
                            st.success(f"Request {booking['booking_reference']} has been submitted. It is pending site approval.")
                        except Exception as exc:
                            st.error(str(exc))
    with bookings_tab:
        mine=rows_for_supplier("bookings",supplier["id"])
        if not mine:
            st.info("No bookings are recorded for your supplier account yet.")
        else:
            output=[]
            for booking in mine:
                product=one("products",id=booking["product_id"])
                po=one("purchase_orders",id=booking["purchase_order_id"]) if booking.get("purchase_order_id") else None
                output.append({"Reference":booking["booking_reference"],"Purchase order":po["po_number"] if po else "Manual","Product":product["name"] if product else "","Arrival":fmt(booking["scheduled_start"]),"Status":booking["status"].replace("_"," ").title()})
            st.dataframe(pd.DataFrame(output),use_container_width=True,hide_index=True)
            csv_frame=pd.DataFrame(output)
            st.download_button("Download my bookings (CSV)",csv_frame.to_csv(index=False).encode("utf-8"),"my-inbound-bookings.csv","text/csv")
            pdf_buffer=BytesIO()
            pdf=canvas.Canvas(pdf_buffer,pagesize=A4)
            width,height=A4
            y=height-48
            pdf.setTitle("My inbound bookings")
            pdf.setFont("Helvetica-Bold",15)
            pdf.drawString(42,y,"KP Snacks Pontefract — Supplier Bookings")
            y-=28
            pdf.setFont("Helvetica",9)
            pdf.drawString(42,y,"Supplier: "+supplier["name"])
            y-=24
            for item in output:
                line=str(item["Reference"])+" | PO: "+str(item["Purchase order"])+" | "+str(item["Product"])+" | "+str(item["Arrival"])+" | "+str(item["Status"])
                if y<55:
                    pdf.showPage()
                    y=height-48
                    pdf.setFont("Helvetica",9)
                pdf.drawString(42,y,line[:115])
                y-=16
            pdf.save()
            st.download_button("Download my bookings (PDF)",pdf_buffer.getvalue(),"my-inbound-bookings.pdf","application/pdf")
            st.markdown("### Delivery paperwork")
            for booking in mine:
                with st.expander(f'{booking["booking_reference"]} · {fmt(booking["scheduled_start"])}'):
                    render_documents(db(),booking,supplier["id"],can_upload=True)
            for proposed in [x for x in mine if x.get("status")=="alternative_proposed"]:
                with st.container(border=True):
                    st.write(f"**Alternative proposed for {proposed['booking_reference']}**")
                    st.write(f"Proposed arrival: {fmt(proposed['scheduled_start'])}")
                    accept,decline=st.columns(2)
                    if accept.button("Accept alternative",key=f"accept_{proposed['id']}"):
                        db().table("bookings").update({"status":"confirmed","confirmed_at":now().isoformat(),"updated_at":now().isoformat()}).eq("id",proposed["id"]).eq("supplier_id",supplier["id"]).eq("status","alternative_proposed").execute()
                        log_supplier_event(proposed["id"],"supplier_accepted_alternative","alternative_proposed","confirmed")
                        queue_internal("Supplier accepted proposed delivery time","alternative_accepted",{"booking_reference":proposed["booking_reference"],"supplier":supplier["name"]},booking_id=proposed["id"])
                        st.success("Alternative accepted. Your booking is now confirmed.")
                        st.rerun()
                    if decline.button("Decline alternative",key=f"decline_{proposed['id']}"):
                        db().table("bookings").update({"status":"rejected","updated_at":now().isoformat()}).eq("id",proposed["id"]).eq("supplier_id",supplier["id"]).eq("status","alternative_proposed").execute()
                        log_supplier_event(proposed["id"],"supplier_declined_alternative","alternative_proposed","rejected")
                        queue_internal("Supplier declined proposed delivery time","alternative_declined",{"booking_reference":proposed["booking_reference"],"supplier":supplier["name"]},booking_id=proposed["id"])
                        st.success("Alternative declined. The request has been closed.")
                        st.rerun()
            eligible=[]
            for booking in mine:
                try:
                    future=datetime.fromisoformat(booking["scheduled_start"].replace("Z","+00:00")).astimezone(TZ)>now()+timedelta(hours=48)
                except Exception:
                    future=False
                if future and booking.get("status")=="confirmed":
                    eligible.append(booking)
            if eligible:
                with st.form("supplier_change_request"):
                    selected=st.selectbox("Confirmed booking",eligible,format_func=lambda x:f"{x['booking_reference']} · {fmt(x['scheduled_start'])}")
                    action=st.radio("Request type",["Change arrival time","Cancel booking"])
                    new_day=st.date_input("New date",value=now().date()+timedelta(days=1),min_value=now().date())
                    new_time=st.time_input("New arrival time",value=time(8,0))
                    reason=st.text_input("Reason for request")
                    submit=st.form_submit_button("Submit request")
                if submit:
                    if not reason.strip():
                        st.error("Enter a reason for the request.")
                    elif action=="Change arrival time" and datetime.combine(new_day,new_time,TZ)<=now():
                        st.error("Choose a future date and time for the amended booking.")
                    elif action=="Change arrival time" and selected.get("purchase_order_id") and (one("purchase_orders",id=selected["purchase_order_id"]) or {}).get("latest_delivery_date") and new_day>date.fromisoformat(one("purchase_orders",id=selected["purchase_order_id"])["latest_delivery_date"]):
                        st.error("The amended date is after the latest permitted delivery date on this purchase order.")
                    else:
                        if action=="Cancel booking":
                            proposed_start=selected["scheduled_start"]; proposed_end=selected["scheduled_end"]; duration=selected["duration_minutes"]; reason_text="CANCEL_REQUEST: "+reason
                        else:
                            proposed_start=datetime.combine(new_day,new_time,TZ).isoformat()
                            duration=selected["duration_minutes"]
                            proposed_end=(datetime.fromisoformat(proposed_start)+timedelta(minutes=duration)).isoformat()
                            reason_text=reason
                        change=db().table("booking_change_requests").insert({"booking_id":selected["id"],"status":"pending","proposed_start":proposed_start,"proposed_end":proposed_end,"proposed_duration_minutes":duration,"reason":reason_text}).execute().data
                        queue_internal("Supplier booking change awaiting approval","booking_change_request",{"booking_reference":selected["booking_reference"],"supplier":supplier["name"],"reason":reason_text,"proposed_start":proposed_start},booking_id=selected["id"],change_request_id=change[0]["id"] if change else None)
                        st.success("Your request has been sent to the site team. The original confirmed booking remains reserved until the site team decides.")
            else:
                st.caption("Amendments and cancellation requests are available for confirmed bookings more than 48 hours away. Within 48 hours, contact the Pontefract site team directly.")
    with po_tab:
        own=rows_for_supplier("purchase_orders",supplier["id"])
        data=[]
        for po in own:
            product=one("products",id=po["product_id"])
            data.append({"PO":po["po_number"],"Product":product["name"] if product else "","Quantity":po.get("quantity"),"Due date":po.get("due_date"),"Latest delivery":po.get("latest_delivery_date"),"Booking permitted":po.get("booking_permitted")})
        if data:
            st.dataframe(pd.DataFrame(data),use_container_width=True,hide_index=True)
        else:
            st.info("No purchase orders are linked to this supplier account.")

def main():
    st.markdown(f"""<style>
    .stApp,[data-testid="stAppViewContainer"]{{background:#f3f4f6;color:#1f2937}}
    [data-testid="stMain"],[data-testid="stMarkdownContainer"],
    [data-testid="stMarkdownContainer"] p,[data-testid="stMarkdownContainer"] li,
    [data-testid="stText"],label,[data-testid="stCaptionContainer"]{{color:#1f2937}}
    [data-testid="stVerticalBlockBorderWrapper"],[data-testid="stExpander"],
    [data-testid="stForm"],[data-testid="stMetric"]{{background:#fff!important;color:#1f2937!important;border:1px solid #d1d5db!important;border-radius:.65rem}}
    [data-testid="stVerticalBlockBorderWrapper"]{{padding:.35rem}}
    [data-testid="stExpander"] details,[data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary p{{color:#1f2937!important}}
    [data-baseweb="input"],[data-baseweb="select"],[data-baseweb="textarea"],
    [data-testid="stDateInput"] input,[data-testid="stTimeInput"] input{{background:#fff!important;color:#111827!important}}
    /* High-contrast controls for light and dark browser themes. */
    [data-baseweb="input"] input,[data-baseweb="textarea"] textarea,
    [data-testid="stDateInput"] input,[data-testid="stTimeInput"] input{{background:#fff!important;color:#111827!important;-webkit-text-fill-color:#111827!important}}
    [data-baseweb="select"]>div{{background:#fff!important;color:#111827!important;border-color:#9ca3af!important}}
    [data-baseweb="select"] input{{color:#111827!important;-webkit-text-fill-color:#111827!important}}
    [role="option"],[role="listbox"],[data-baseweb="popover"],[data-baseweb="menu"]{{background:#fff!important;color:#111827!important}}
    [data-testid="stButton"] button,[data-testid="stFormSubmitButton"] button,
    [data-testid="stDownloadButton"] button{{background:#C8102E!important;color:#fff!important;border:1px solid #9f0c24!important}}
    [data-testid="stButton"] button *,[data-testid="stFormSubmitButton"] button *,
    [data-testid="stDownloadButton"] button *{{color:#fff!important}}
    input::placeholder,textarea::placeholder{{color:#6b7280!important;opacity:1!important}}
    [data-testid="stDataFrame"]{{border:1px solid #d1d5db;border-radius:.5rem}}
    .supplier-header{{background:{KP_RED};color:white;padding:1.2rem 1.5rem;border-radius:.5rem;margin-bottom:1.2rem}}
    .supplier-header h1{{color:white;margin:0;font-size:1.8rem}}
    .supplier-header p{{color:white;margin:.35rem 0 0}}
    </style>""",unsafe_allow_html=True)
    st.markdown('<div class="supplier-header"><h1>KP Snacks Pontefract Supplier Inbound Portal</h1><p>Purchase orders · Delivery requests · Booking history</p></div>',unsafe_allow_html=True)
    try:
        db().table("app_settings").select("setting_key").limit(1).execute()
    except Exception as exc:
        st.error(f"The supplier portal cannot connect to the inbound booking service ({type(exc).__name__}). Please contact the Pontefract site team.")
        return
    supplier_id=st.session_state.get("supplier_id")
    supplier=one("suppliers",id=supplier_id) if supplier_id else None
    if supplier and not supplier.get("active"):
        st.session_state.pop("supplier_id",None)
        st.session_state.pop("supplier_contact_id",None)
        st.session_state.pop("supplier_contact_name",None)
        supplier=None
    if supplier:
        portal(supplier)
    else:
        st.write("Sign in using the supplier code and password provided by the Pontefract site team.")
        sign_in()
    st.divider()
    st.caption("KP Snacks Pontefract · Supplier Inbound Portal")

if __name__=="__main__":
    main()

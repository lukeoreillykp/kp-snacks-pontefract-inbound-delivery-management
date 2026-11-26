from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st

TZ = ZoneInfo("Europe/London")
BUCKET = "supplier-delivery-documents"

def _rows(db, table, **filters):
    q = db.table(table).select("*")
    for key, value in filters.items():
        q = q.eq(key, value)
    return q.execute().data or []

def arrival_page(db, role):
    st.subheader("Delivery arrival confirmation")
    st.caption("Outstanding deliveries must be confirmed by the Team Captain / shift team currently on duty. Record the actual arrival time and whether the delivered quantity was correct.")
    if eligible:
        st.warning(f"{len(eligible)} delivery arrival confirmation(s) outstanding. Please record each arrival before the shift ends.")
        st.dataframe(pd.DataFrame([{"Booking":b.get("booking_reference"),"Expected arrival":b.get("scheduled_start"),"Status":"Awaiting arrival confirmation"} for b in eligible]),use_container_width=True,hide_index=True)
    else:
        st.success("All confirmed deliveries have an arrival confirmation recorded.")
    bookings = _rows(db, "bookings")
    eligible = [b for b in bookings if b.get("status") == "confirmed" and not b.get("actual_arrival_at")]
    eligible.sort(key=lambda b: b.get("scheduled_start") or "")
    if not eligible:
        st.info("There are no outstanding confirmed deliveries to record.")
        return
    suppliers = {x["id"]: x for x in _rows(db, "suppliers")}
    products = {x["id"]: x for x in _rows(db, "products")}
    selected = st.selectbox("Delivery booking", eligible, format_func=lambda b: f'{b["booking_reference"]} · {suppliers.get(b["supplier_id"],{}).get("name","Supplier")} · {b["scheduled_start"][:16]}')
    supplier = suppliers.get(selected["supplier_id"], {})
    product = products.get(selected["product_id"], {})
    st.write(f'**Supplier:** {supplier.get("name","")}  ·  **Product:** {product.get("name","")}  ·  **Expected quantity:** {selected.get("quantity") or "Not recorded"} {selected.get("quantity_unit") or ""}')
    existing = selected.get("actual_arrival_at")
    default_arrival = datetime.fromisoformat(existing.replace("Z","+00:00")).astimezone(TZ) if existing else datetime.now(TZ)
    with st.form("arrival_confirmation"):
        arrival_day = st.date_input("Actual arrival date", value=default_arrival.date())
        arrival_time = st.time_input("Actual arrival time", value=default_arrival.time().replace(tzinfo=None))
        correct = st.radio("Were the quantities correct?", ["Yes", "No"], horizontal=True, index=0 if selected.get("quantity_correct") is not False else 1)
        received = st.number_input("Quantity received (only if incorrect)", min_value=0.0, value=float(selected.get("quantity_received") or 0), disabled=correct=="Yes")
        notes = st.text_area("Discrepancy details (only if incorrect)", value=selected.get("quantity_discrepancy_notes") or "", disabled=correct=="Yes")
        save = st.form_submit_button("Save arrival confirmation", type="primary")
    if save:
        if correct == "No" and not notes.strip():
            st.error("Please describe the quantity discrepancy.")
        else:
            stamp = datetime.combine(arrival_day, arrival_time, TZ).isoformat()
            patch = {"actual_arrival_at": stamp, "quantity_correct": correct=="Yes", "quantity_received": None if correct=="Yes" else received, "quantity_discrepancy_notes": None if correct=="Yes" else notes.strip(), "updated_at": datetime.now(TZ).isoformat()}
            db.table("bookings").update(patch).eq("id", selected["id"]).execute()
            db.table("booking_events").insert({"booking_id":selected["id"],"event_type":"arrival_confirmed","actor_label":role,"reason":None if correct=="Yes" else notes.strip(),"details":{"actual_arrival_at":stamp,"quantity_correct":correct=="Yes","quantity_received":None if correct=="Yes" else received}}).execute()
            st.success("Arrival confirmation saved.")
            st.rerun()
    st.divider()
    render_documents(db, selected, selected["supplier_id"], can_upload=False)

def supplier_performance_page(db):
    st.subheader("Monthly supplier performance")
    bookings = _rows(db, "bookings")
    suppliers = {x["id"]: x for x in _rows(db, "suppliers")}
    if not bookings:
        st.info("No delivery records are available yet.")
        return
    months = sorted({str(b.get("scheduled_start",""))[:7] for b in bookings if b.get("scheduled_start")}, reverse=True)
    month = st.selectbox("Reporting month", months)
    filtered = [b for b in bookings if str(b.get("scheduled_start","")).startswith(month) and b.get("status") in ("confirmed","completed","no_show")]
    result=[]
    for sid, supplier in suppliers.items():
        own=[b for b in filtered if b.get("supplier_id")==sid]
        if not own: continue
        on_time=0; late=0; no_show=0; qty_bad=0
        for b in own:
            if b.get("status")=="no_show": no_show+=1
            elif b.get("actual_arrival_at") and b.get("scheduled_start"):
                try:
                    actual=datetime.fromisoformat(b["actual_arrival_at"].replace("Z","+00:00"))
                    planned=datetime.fromisoformat(b["scheduled_start"].replace("Z","+00:00"))
                    if actual <= planned: on_time+=1
                    else: late+=1
                except Exception: pass
            if b.get("quantity_correct") is False: qty_bad+=1
        arrivals=on_time+late
        checked=sum(1 for b in own if b.get("quantity_correct") is not None)
        correct=checked-qty_bad
        result.append({"Supplier":supplier.get("name",""),"Total deliveries":len(own),"On time":on_time,"Late":late,"No-shows":no_show,"Quantity discrepancies":qty_bad,"On-time %":round(100*on_time/arrivals,1) if arrivals else None,"Quantity accuracy %":round(100*correct/checked,1) if checked else None})
    frame=pd.DataFrame(result)
    if not frame.empty:
        st.dataframe(frame,use_container_width=True,hide_index=True)
        csv=frame.to_csv(index=False).encode("utf-8")
        st.download_button("Export supplier report (CSV)",csv,f"supplier-performance-{month}.csv","text/csv")
        from io import BytesIO
        out=BytesIO()
        with pd.ExcelWriter(out,engine="openpyxl") as writer:
            frame.to_excel(writer,index=False,sheet_name="Supplier performance")
        st.download_button("Export supplier report (Excel)",out.getvalue(),f"supplier-performance-{month}.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        st.info("No supplier deliveries were recorded for this month.")

def render_documents(db, booking, supplier_id, can_upload=False):
    docs=_rows(db,"supplier_documents",booking_id=booking["id"],supplier_id=supplier_id)
    st.markdown("**Delivery paperwork**")
    if docs:
        for doc in docs:
            col1,col2=st.columns([4,1])
            col1.write(f'{doc["document_type"]}: {doc["original_file_name"]}')
            try:
                signed=db.storage.from_(BUCKET).create_signed_url(doc["storage_path"],300)
                url=signed.get("signedURL") or signed.get("signedUrl")
                if url: col2.link_button("Download",url)
            except Exception:
                col2.caption("Download unavailable")
    else:
        st.caption("No paperwork uploaded for this booking.")
    if can_upload:
        with st.form(f'doc_upload_{booking["id"]}'):
            kind=st.selectbox("Document type",["Delivery note","Certificate","Other"],key=f'doc_type_{booking["id"]}')
            desc=st.text_input("Description",key=f'doc_desc_{booking["id"]}')
            file=st.file_uploader("Upload document (PDF, image, Word or Excel; max 20 MB)",type=["pdf","jpg","jpeg","png","doc","docx","xls","xlsx"],key=f'doc_file_{booking["id"]}')
            submit=st.form_submit_button("Upload paperwork")
        if submit:
            if not file:
                st.error("Choose a file first.")
            else:
                path=f'{supplier_id}/{booking["id"]}/{datetime.now(TZ).strftime("%Y%m%d%H%M%S")}_{file.name}'
                try:
                    db.storage.from_(BUCKET).upload(path,file.getvalue(),{"content-type":file.type or "application/octet-stream","upsert":"false"})
                    db.table("supplier_documents").insert({"booking_id":booking["id"],"supplier_id":supplier_id,"document_type":kind,"description":desc or None,"storage_path":path,"original_file_name":file.name,"content_type":file.type or "application/octet-stream","file_size_bytes":len(file.getvalue()),"uploaded_by_label":"supplier"}).execute()
                    st.success("Document uploaded.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Upload failed: {type(exc).__name__}: {exc}")

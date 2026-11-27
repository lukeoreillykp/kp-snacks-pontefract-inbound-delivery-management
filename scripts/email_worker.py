import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

SENDER = "Kp.ponte.csv@gmail.com"
TZ = ZoneInfo("Europe/London")
MAX_ATTEMPTS = 3  # One initial attempt plus two retries.


def request_json(url, key, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates" if method == "POST" and "on_conflict=" in url else "return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def build_message(item):
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = item["recipient_email"]
    message["Subject"] = item["subject"]
    if item.get("cc_email"):
        message["Cc"] = item["cc_email"]
    payload = item.get("payload") or {}
    details = []
    for key, label in (
        ("booking_reference", "Booking reference"),
        ("purchase_order", "Purchase order"),
        ("supplier", "Supplier"),
        ("product", "Product"),
        ("quantity", "Quantity"),
        ("quantity_unit", "Unit"),
        ("due_date", "Due date"),
        ("latest_delivery_date", "Latest delivery date"),
        ("status", "Status"),
        ("scheduled_start", "Scheduled arrival"),
        ("proposed_start", "Proposed arrival"),
        ("reason", "Details"),
        ("test_routing_note", "Original supplier recipient (test redirect)"),
    ):
        value = payload.get(key)
        if value:
            details.append(f"{label}: {value}")
    message.set_content(
        "KP Snacks Pontefract inbound delivery notification\n\n"
        + ("\n".join(details) if details else "There is an update in the inbound delivery management system.")
        + "\n\nThis is an automated notification. Please use the inbound delivery management app for the latest information."
    )
    return message



def get_rows(base, key, table, params):
    query = urllib.parse.urlencode(params)
    return request_json(f"{base}/rest/v1/{table}?{query}", key) or []


def setting_value(base, key, name, default=None):
    rows = get_rows(base, key, "app_settings", {"select":"setting_value","setting_key":f"eq.{name}","limit":"1"})
    return rows[0].get("setting_value", default) if rows else default


def uk_bank_holidays():
    url = "https://www.gov.uk/bank-holidays.json"
    with urllib.request.urlopen(url, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))
    return {item["date"] for item in data.get("england-and-wales",{}).get("events",[])}


def shift_now(base, key, now):
    weekday = now.isoweekday()
    clock = now.time().replace(tzinfo=None)
    shifts = get_rows(base,key,"shift_hours",{"select":"*","active":"eq.true"})
    definitions = {x["id"]:x for x in get_rows(base,key,"shift_definitions",{"select":"*","active":"eq.true"})}
    for h in shifts:
        shift=definitions.get(h["shift_id"])
        if not shift: continue
        day=int(h["iso_weekday"])
        start=datetime.strptime(h["start_time"],"%H:%M:%S").time()
        end=datetime.strptime(h["end_time"],"%H:%M:%S").time()
        if not h["ends_next_day"] and day==weekday and start<=clock<end:
            return shift["shift_code"], now.date()
        if h["ends_next_day"]:
            if day==weekday and clock>=start:
                return shift["shift_code"], now.date()
            previous=7 if weekday==1 else weekday-1
            if day==previous and clock<end:
                return shift["shift_code"], now.date()-timedelta(days=1)
    return None,None


def captain_for_shift(config, shift_code, shift_start_date):
    teams=(config or {}).get("teams") or {}
    anchor=(config or {}).get("anchor_week_start")
    if not anchor: return None
    weeks=(shift_start_date-date.fromisoformat(anchor)).days//7
    rotation=(config or {}).get("rotation") or {"days":"nights","nights":"afternoons","afternoons":"days"}
    for team,details in teams.items():
        current=details.get("shift")
        for _ in range(weeks % 3):
            current=rotation.get(current,current)
        if current==shift_code:
            return details.get("captain")
    return None


def process_arrival_reminders(base,key):
    now=datetime.now(ZoneInfo("Europe/London"))
    try:
        holiday_dates=uk_bank_holidays()
    except Exception as exc:
        print(f"Arrival reminders paused: could not verify UK bank holidays ({type(exc).__name__}).")
        return
    if now.weekday()>=5 or now.date().isoformat() in holiday_dates:
        print("Arrival reminders paused: weekend or England/Wales bank holiday.")
        return
    shift_code,shift_start_date=shift_now(base,key,now)
    if not shift_code:
        print("Arrival reminders paused: outside configured shift hours.")
        return
    if shift_start_date.isoformat() in holiday_dates:
        print("Arrival reminders paused: current overnight shift began on a bank holiday.")
        return
    rota=setting_value(base,key,"team_captain_rota",{})
    contacts=setting_value(base,key,"arrival_reminder_contacts",{})
    captain=captain_for_shift(rota,shift_code,shift_start_date)
    recipient=((contacts or {}).get("captains") or {}).get(captain or "")
    if not captain or not recipient:
        print(f"Arrival reminders paused: no email configured for current {shift_code} captain.")
        return
    manager=(contacts or {}).get("warehouse_manager")
    pending=get_rows(base,key,"bookings",{
        "select":"id,booking_reference,scheduled_start,scheduled_end,status,actual_arrival_at,supplier_id,product_id",
        "status":"in.(confirmed,completed)","actual_arrival_at":"is.null",
        "scheduled_start":f"lte.{now.isoformat()}",
        "order":"scheduled_start.asc","limit":"100"
    })
    if not pending: return
    states=get_rows(base,key,"arrival_reminder_state",{"select":"*","limit":"1000"})
    state_by_booking={x["booking_id"]:x for x in states}
    suppliers={x["id"]:x for x in get_rows(base,key,"suppliers",{"select":"id,name","limit":"1000"})}
    products={x["id"]:x for x in get_rows(base,key,"products",{"select":"id,name","limit":"1000"})}
    for booking in pending:
        state=state_by_booking.get(booking["id"],{})
        count=int(state.get("reminder_count") or 0)
        last=state.get("last_reminded_at")
        if last:
            last_dt=datetime.fromisoformat(last.replace("Z","+00:00")).astimezone(ZoneInfo("Europe/London"))
            if now-last_dt < timedelta(hours=2): continue
        supplier=suppliers.get(booking.get("supplier_id"),{}).get("name","Supplier")
        product=products.get(booking.get("product_id"),{}).get("name","Delivery")
        scheduled=datetime.fromisoformat(booking["scheduled_start"].replace("Z","+00:00")).astimezone(ZoneInfo("Europe/London"))
        next_count=count+1
        cc=manager if next_count>2 and manager else None
        subject=f"Arrival confirmation required – {booking['booking_reference']}"
        payload={
            "booking_reference":booking["booking_reference"],
            "supplier":supplier,"product":product,
            "scheduled_start":scheduled.strftime("%d/%m/%Y %H:%M"),
            "status":f"Reminder {next_count}: please confirm whether this delivery has arrived in the Delivery arrivals screen. This reminder is assigned to the {shift_code.title()} shift Team Captain ({captain})."
        }
        request_json(f"{base}/rest/v1/email_outbox",key,"POST",{
            "booking_id":booking["id"],"recipient_email":recipient,"cc_email":cc,
            "subject":subject,"template_key":"arrival_confirmation_reminder",
            "payload":payload,"status":"queued"
        })
        state_payload={
            "booking_id":booking["id"],"reminder_count":next_count,
            "last_reminded_at":now.isoformat(),"last_recipient_email":recipient,
            "last_shift_code":shift_code,"updated_at":now.isoformat()
        }
        request_json(f"{base}/rest/v1/arrival_reminder_state?on_conflict=booking_id",key,"POST",state_payload)
    print(f"Arrival reminders evaluated for {len(pending)} overdue confirmed booking(s).")


def main():
    base = os.environ["SUPABASE_URL"].strip().rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
    password = os.environ["GMAIL_APP_PASSWORD"].strip()
    query = urllib.parse.urlencode({
        "select": "*",
        "status": "eq.queued",
        "order": "created_at.asc",
        "limit": "10",
    })
    endpoint = f"{base}/rest/v1/email_outbox?{query}"
    process_arrival_reminders(base,key)
    pending = request_json(endpoint, key) or []
    email_test_mode = setting_value(base, key, "email_test_mode", {}) or {}
    supplier_redirect = (
        email_test_mode.get("enabled") is True
        and email_test_mode.get("redirect_supplier_emails") is True
        and bool(email_test_mode.get("recipient_email"))
    )
    supplier_templates = {"booking_status", "purchase_order_added"}
    if supplier_redirect:
        test_recipient = email_test_mode["recipient_email"].strip()
        for item in pending:
            if item.get("template_key") in supplier_templates:
                original_recipient = item.get("recipient_email", "")
                item["recipient_email"] = test_recipient
                item["cc_email"] = None
                item["subject"] = "[TEST ROUTED] " + item.get("subject", "")
                payload = item.get("payload") or {}
                payload["test_routing_note"] = original_recipient
                item["payload"] = payload
    if not pending:
        print("Email queue checked: no queued messages.")
        return

    sent = 0
    retrying = 0
    failed = 0
    for item in pending:
        attempt = int(item.get("attempt_count") or 0) + 1
        try:
            message = build_message(item)
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=25) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(SENDER, password)
                smtp.send_message(message)
            patch_url = f"{base}/rest/v1/email_outbox?id=eq.{urllib.parse.quote(str(item['id']))}&status=eq.queued"
            request_json(patch_url, key, "PATCH", {
                "status": "sent",
                "sent_at": datetime.now(TZ).isoformat(),
                "attempt_count": attempt,
                "last_error": None,
            })
            sent += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:1000]
            will_retry = attempt < MAX_ATTEMPTS
            next_status = "queued" if will_retry else "failed"
            patch_url = f"{base}/rest/v1/email_outbox?id=eq.{urllib.parse.quote(str(item['id']))}&status=eq.queued"
            try:
                request_json(patch_url, key, "PATCH", {
                    "status": next_status,
                    "attempt_count": attempt,
                    "last_error": error,
                })
            except Exception as patch_exc:
                print(f"Could not record failure for outbox item {item.get('id')}: {type(patch_exc).__name__}")
            if will_retry:
                retrying += 1
                print(
                    f"Email attempt {attempt}/{MAX_ATTEMPTS} failed for outbox item "
                    f"{item.get('id')}; it remains queued for the next worker run: {error}"
                )
            else:
                failed += 1
                print(
                    f"Email permanently failed after {attempt} attempts for outbox item "
                    f"{item.get('id')}: {error}"
                )

    print(
        f"Email queue checked: {sent} sent, {retrying} awaiting retry, "
        f"{failed} permanently failed."
    )


if __name__ == "__main__":
    main()

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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
            "Prefer": "return=representation",
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
    pending = request_json(endpoint, key) or []
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

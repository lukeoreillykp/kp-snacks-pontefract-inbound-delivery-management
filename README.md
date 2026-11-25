# KP Snacks Pontefract Inbound Delivery Management

A standalone Streamlit application for Pontefract inbound delivery booking and warehouse coordination. It uses a dedicated Supabase project and repository; it does not modify the existing outbound application.

## Included workflows

- Public view-only operational dashboard and weekly delivery calendar
- Internal shared-role access: Supervisor, Material Controller, Warehouse Manager and Administrator
- Administrator-managed role passwords and enable/disable controls
- Supplier portal with supplier-code/password sign-in, own purchase orders and own booking history
- Supplier booking requests, amendments and cancellations requiring site approval
- Internal booking approvals, rejection, alternative proposals and manual bookings
- Atomic database-side unloading-area and staffing checks, with audited capacity overrides
- Purchase-order Excel import/update, product and delivery-rule management
- Shift staffing plans, staff register, absence records and area buffers
- Booking history, CSV report export and queued supplier email notifications
- Background Gmail dispatcher using GitHub Actions, with one initial attempt and up to two automatic retries

## Deployment

Deploy `app.py` on Streamlit Community Cloud using `requirements.txt` for the internal portal.

Deploy a second Streamlit Community Cloud app from the same repository with **Main file path** set to `supplier_app.py` for the separate external supplier portal. Give that second app the same private `[supabase]` Secrets section shown below. The two apps use the same inbound database but have separate navigation and access surfaces.

Add these private Streamlit Secrets:

```toml
[supabase]
url = "YOUR_INBOUND_SUPABASE_PROJECT_URL"
key = "YOUR_INBOUND_SUPABASE_SECRET_KEY"
```

The first Administrator was created using a one-time `[bootstrap]` token. Keep that section removed after setup. Never commit Supabase secret keys or passwords to GitHub.

## Supplier onboarding

Give each supplier the URL of the separate `supplier_app.py` deployment, not the internal portal URL.

1. Add the supplier and contact email under **Suppliers**.
2. Enable the supplier portal and set a strong shared supplier password.
3. Import purchase orders with a matching supplier code and product name.
4. Give the supplier the portal URL, supplier code and password through an approved secure channel.

The supplier portal only displays records filtered to the signed-in supplier. Supplier sessions use the Streamlit server-side Supabase connection; database secret keys are not sent to the browser.

## Purchase-order workbook columns

Required: `supplier_code`, `po_number`, `product`.

Recommended: `delivery_type`, `quantity`, `due_date`, `latest_delivery_date`, `booking_permitted`.

Dates should be Excel dates or values pandas can parse. Existing purchase orders are updated by supplier, PO number and product; existing bookings are not deleted.

## Important operational configuration before live use

- Enter the weekly staffing plan for each date and shift. Defaults are 4 day, 3 afternoon and 4 night main-team staff, with 2 protected for outbound/palletisation. Planned absences and additional operator availability must be kept current.
- The main-team weekly rotation anchor and the additional operator's alternating-week anchor have not yet been supplied, so individual rosters are not auto-generated.
- The dedicated bulk operator must be marked available on the relevant day-shift staffing plan when required.
- Booking notifications are written to the Supabase email outbox and processed by the GitHub Actions background worker approximately every five minutes (GitHub may delay scheduled starts). The worker makes one initial send attempt and up to two retries on later runs before marking a message failed.
- The worker requires the GitHub Actions repository secrets `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `GMAIL_APP_PASSWORD`. Keep these values private and never commit them to the repository.
- An outbox status of `sent` means Gmail accepted the message via SMTP; it does not prove that the recipient's inbox received it. Review the queue and worker run logs when troubleshooting.
- Validate the app with site users and IT before inviting external suppliers. Confirm supplier account handling, retention, and public view-only fields with site policy.

## Security model

- Supabase tables have row-level security enabled. Direct `anon` and `authenticated` table grants remain revoked; the Streamlit server accesses Supabase using its private server-side secret.
- Database booking functions run as invoker (not security definer), are executable only by `service_role`, and use transaction-scoped advisory locks to prevent concurrent area/staffing overbooking.
- Passwords are stored as salted scrypt hashes.
- Shared role and supplier accounts identify the account/role in audit records, not the individual person using a shared password.
- Capacity overrides require a reason and are recorded in booking history.

## Automated validation

A GitHub Actions workflow compiles the Python source on pushes and pull requests. Database capacity and change-approval functions have been exercised inside rollback-only test transactions.

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

## Deployment

Deploy `app.py` on Streamlit Community Cloud using `requirements.txt`.

Add these private Streamlit Secrets:

```toml
[supabase]
url = "YOUR_INBOUND_SUPABASE_PROJECT_URL"
key = "YOUR_INBOUND_SUPABASE_SECRET_KEY"
```

The first Administrator was created using a one-time `[bootstrap]` token. Keep that section removed after setup. Never commit Supabase secret keys or passwords to GitHub.

## Supplier onboarding

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
- Gmail delivery is not yet connected for this inbound deployment. Booking outcomes are written to the Supabase email outbox; they are not sent until an approved Gmail sending integration is configured.
- Validate the app with site users and IT before inviting external suppliers. Confirm supplier account handling, retention, and public view-only fields with site policy.

## Security model

- Supabase tables have row-level security enabled. Direct `anon` and `authenticated` table grants remain revoked; the Streamlit server accesses Supabase using its private server-side secret.
- Database booking functions run as invoker (not security definer), are executable only by `service_role`, and use transaction-scoped advisory locks to prevent concurrent area/staffing overbooking.
- Passwords are stored as salted scrypt hashes.
- Shared role and supplier accounts identify the account/role in audit records, not the individual person using a shared password.
- Capacity overrides require a reason and are recorded in booking history.

## Validation

A GitHub Actions workflow compiles the Python source on pushes and pull requests. Database capacity and change-approval functions have been exercised inside rollback-only test transactions.

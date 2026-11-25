import hashlib
import hmac
import secrets

import streamlit as st
from supabase import Client, create_client

st.set_page_config(
    page_title="KP Snacks | Pontefract Inbound",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

KP_RED = "#C8102E"
DARK = "#292929"
INTERNAL_ROLES = [
    "supervisor",
    "material_controller",
    "warehouse_manager",
    "administrator",
]
ROLE_LABELS = {
    "supervisor": "Supervisor",
    "material_controller": "Material Controller",
    "warehouse_manager": "Warehouse Manager",
    "administrator": "Administrator",
}
PAGES = [
    "Dashboard",
    "Delivery calendar",
    "Booking management",
    "Orders & Excel upload",
    "Warehouse resources",
    "Products & delivery rules",
    "Suppliers",
    "Reports",
    "User & role management",
]
ROLE_PAGES = {
    "supervisor": {"Dashboard", "Delivery calendar", "Booking management"},
    "material_controller": {
        "Dashboard", "Delivery calendar", "Booking management",
        "Orders & Excel upload", "Products & delivery rules",
    },
    "warehouse_manager": {
        "Dashboard", "Delivery calendar", "Booking management",
        "Warehouse resources",
    },
    "administrator": set(PAGES),
}


@st.cache_resource
def get_supabase_client() -> Client:
    """Create the database client on the Streamlit server only."""
    config = st.secrets["supabase"]
    return create_client(config["url"], config["key"])


def password_hash(password: str) -> str:
    """Hash a password using scrypt; the original password is never stored."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64
    )
    return "scrypt$16384$8$1$" + salt.hex() + "$" + derived.hex()


def password_matches(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, hash_hex = stored_hash.split("$")
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def get_role_credential(role: str):
    response = (
        get_supabase_client()
        .table("internal_role_credentials")
        .select("role,password_hash,enabled")
        .eq("role", role)
        .maybe_single()
        .execute()
    )
    return response.data


def is_admin_configured() -> bool:
    record = get_role_credential("administrator")
    return bool(record and record.get("enabled") and record.get("password_hash"))


def bootstrap_admin():
    """One-time first administrator setup, protected by a token in Streamlit Secrets."""
    try:
        bootstrap_token = st.secrets["bootstrap"]["token"]
    except (KeyError, TypeError):
        st.warning(
            "First-time setup is not enabled yet. Add a private [bootstrap] "
            "token in Streamlit Settings → Secrets, then refresh this page."
        )
        st.code('[bootstrap]\ntoken = "YOUR-ONE-TIME-SETUP-TOKEN"')
        return

    st.subheader("First-time administrator setup")
    st.write(
        "Create the initial shared Administrator password. This setup is available "
        "only until the first administrator account has been created."
    )
    with st.form("initial_admin_setup"):
        supplied_token = st.text_input("One-time setup token", type="password")
        new_password = st.text_input("Choose Administrator password", type="password")
        confirm_password = st.text_input("Confirm Administrator password", type="password")
        submitted = st.form_submit_button("Create Administrator access", type="primary")

    if submitted:
        if not hmac.compare_digest(supplied_token, str(bootstrap_token)):
            st.error("The setup token is incorrect.")
        elif len(new_password) < 12:
            st.error("Use a password with at least 12 characters.")
        elif new_password != confirm_password:
            st.error("The passwords do not match.")
        else:
            try:
                client = get_supabase_client()
                client.table("internal_role_credentials").insert({
                    "role": "administrator",
                    "password_hash": password_hash(new_password),
                    "enabled": True,
                }).execute()
                client.table("internal_role_password_events").insert({
                    "role": "administrator",
                    "action": "password_set",
                    "changed_by_role": "administrator",
                }).execute()
                st.session_state["auth_role"] = "administrator"
                st.success("Administrator access created. You are now signed in.")
                st.rerun()
            except Exception as exc:
                st.error(
                    "Administrator setup could not be completed "
                    f"({type(exc).__name__}). Refresh and check the database connection."
                )


def login_panel():
    st.subheader("Internal team sign-in")
    with st.form("role_login"):
        role = st.selectbox(
            "Access level",
            INTERNAL_ROLES,
            format_func=lambda value: ROLE_LABELS[value],
        )
        password = st.text_input("Shared role password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            record = get_role_credential(role)
            if (
                record
                and record.get("enabled")
                and record.get("password_hash")
                and password_matches(password, record["password_hash"])
            ):
                st.session_state["auth_role"] = role
                st.rerun()
            else:
                st.error("Sign-in failed. Check the role password or contact an Administrator.")
        except Exception as exc:
            st.error(f"Sign-in could not be checked ({type(exc).__name__}).")


def check_database_connection():
    try:
        get_supabase_client().table("app_settings").select("setting_key").limit(1).execute()
        return True, "Connected to the inbound database."
    except KeyError:
        return False, "Supabase settings are missing. Add [supabase] url and key in Streamlit Secrets."
    except Exception as exc:
        return False, f"Database connection failed ({type(exc).__name__})."


st.markdown(
    f"""
    <style>
      .stApp {{ background: #f5f5f5; }}
      [data-testid="stSidebar"] {{ background: {DARK}; }}
      [data-testid="stSidebar"] * {{ color: #ffffff !important; }}
      .kp-header {{
        background: {KP_RED}; color: white; padding: 1.2rem 1.5rem;
        border-radius: 0.5rem; margin-bottom: 1.2rem;
      }}
      .kp-header h1 {{ color: white; margin: 0; font-size: 1.8rem; }}
      .kp-header p {{ color: white; margin: .35rem 0 0 0; opacity: .95; }}
      div[data-testid="stMetric"] {{
        background: white; padding: 1rem; border-radius: .5rem;
        border: 1px solid #e5e5e5;
      }}
    </style>
    """,
    unsafe_allow_html=True,
)

db_ok, db_message = check_database_connection()
auth_role = st.session_state.get("auth_role")

with st.sidebar:
    st.markdown("## KP Snacks")
    st.caption("Pontefract site")
    if db_ok:
        st.success("Inbound database connected")
    else:
        st.error("Inbound database not connected")
    st.divider()
    if auth_role:
        st.write(f"Signed in: **{ROLE_LABELS.get(auth_role, auth_role)}**")
        if st.button("Sign out", use_container_width=True):
            st.session_state.pop("auth_role", None)
            st.rerun()
        allowed_pages = ROLE_PAGES.get(auth_role, set())
    else:
        st.caption("View-only access")
        allowed_pages = {"Dashboard", "Delivery calendar"}
    page = st.radio("Navigation", [p for p in PAGES if p in allowed_pages])
    st.divider()
    st.caption("Inbound Delivery Management")

st.markdown(
    """
    <div class="kp-header">
      <h1>Inbound Delivery Management</h1>
      <p>Pontefract site · Internal operations portal</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not db_ok:
    st.error(db_message)

if page == "Dashboard":
    st.subheader("Operational dashboard")
    st.caption("Public view-only access is limited to approved, non-sensitive operational information.")
    a, b, c, d = st.columns(4)
    a.metric("Expected today", "—")
    b.metric("Pending approvals", "—")
    c.metric("Confirmed deliveries", "—")
    d.metric("Exceptions", "—")
    st.markdown("### Warehouse capacity")
    left, right = st.columns(2)
    with left:
        st.markdown("**Main inbound / bulk area**")
        st.info("Approved capacity information will appear here.")
    with right:
        st.markdown("**Goods-out packaging inbound**")
        st.info("Approved capacity information will appear here.")
    st.markdown("### Today's activity")
    st.info("Live dashboard figures will be connected after access controls and data visibility are completed.")

elif page == "Delivery calendar":
    st.subheader("Delivery calendar")
    st.info("The calendar will show only the delivery information permitted for the signed-in access level.")

elif page == "Booking management":
    st.subheader("Booking management")
    st.info("Review supplier requests, approve or reject changes, propose alternatives, and create manual bookings.")

elif page == "Orders & Excel upload":
    st.subheader("Orders & Excel upload")
    st.info("Material Controller access: purchase-order import, validation preview, and controlled updates.")

elif page == "Warehouse resources":
    st.subheader("Warehouse resources")
    st.info("Warehouse Manager access: shift staffing, operator availability, holidays, sickness, and audited capacity overrides.")

elif page == "Products & delivery rules":
    st.subheader("Products & delivery rules")
    st.info("Material Controller access: configure delivery methods, standard durations, buffers, and product restrictions.")

elif page == "Suppliers":
    st.subheader("Suppliers")
    st.info("Administrator access: manage supplier records and supplier portal access.")

elif page == "Reports":
    st.subheader("Reports")
    st.info("Administrator access: operational reporting and booking history.")

elif page == "User & role management":
    st.subheader("User & role management")
    st.info("Administrator access: manage shared role passwords and enable or disable role access.")

if not auth_role and db_ok:
    st.divider()
    try:
        admin_ready = is_admin_configured()
    except Exception:
        admin_ready = False
        st.error("Administrator access could not be checked. Verify the database connection.")
    if not admin_ready:
        bootstrap_admin()
    else:
        login_panel()

st.divider()
st.caption("KP Snacks Pontefract · Inbound Delivery Management")

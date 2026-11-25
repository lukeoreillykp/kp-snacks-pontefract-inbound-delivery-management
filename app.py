import streamlit as st

st.set_page_config(
    page_title="KP Snacks | Pontefract Inbound",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

KP_RED = "#C8102E"
DARK = "#292929"

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

with st.sidebar:
    st.markdown("## KP Snacks")
    st.caption("Pontefract site")
    page = st.radio(
        "Navigation",
        [
            "Dashboard",
            "Delivery calendar",
            "Booking management",
            "Orders & Excel upload",
            "Warehouse resources",
            "Products & delivery rules",
            "Suppliers",
            "Reports",
            "User & role management",
        ],
    )
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

if page == "Dashboard":
    st.subheader("Operational dashboard")
    st.caption("Live operational figures will appear here once the secure database connection is configured.")

    a, b, c, d = st.columns(4)
    a.metric("Expected today", "—")
    b.metric("Pending approvals", "—")
    c.metric("Confirmed deliveries", "—")
    d.metric("Exceptions", "—")

    st.markdown("### Warehouse capacity")
    left, right = st.columns(2)
    with left:
        st.markdown("**Main inbound / bulk area**")
        st.info("Capacity and operator availability will be displayed here.")
    with right:
        st.markdown("**Goods-out packaging inbound**")
        st.info("Packaging bay capacity and operator availability will be displayed here.")

    st.markdown("### Today's activity")
    st.info("No live bookings are connected yet. Booking data will be loaded from the inbound Supabase project.")

elif page == "Delivery calendar":
    st.subheader("Delivery calendar")
    st.info("Weekly and daily delivery timelines will be added here, including area buffers and operator allocation.")

elif page == "Booking management":
    st.subheader("Booking management")
    st.info("Review supplier requests, approve or reject changes, propose alternatives, and create manual bookings.")

elif page == "Orders & Excel upload":
    st.subheader("Orders & Excel upload")
    st.info("Purchase-order import, validation preview, and controlled updates will be added here.")

elif page == "Warehouse resources":
    st.subheader("Warehouse resources")
    st.info("Manage shift staffing, operator availability, holidays, sickness, and audited capacity overrides.")

elif page == "Products & delivery rules":
    st.subheader("Products & delivery rules")
    st.info("Configure delivery areas, methods, standard durations, buffers, and product-specific restrictions.")

elif page == "Suppliers":
    st.subheader("Suppliers")
    st.info("Manage supplier records and supplier portal access. Suppliers will only be able to see their own data.")

elif page == "Reports":
    st.subheader("Reports")
    st.info("Operational reporting and booking history will be added here.")

elif page == "User & role management":
    st.subheader("User & role management")
    st.info("Administrator-only role controls and secure shared-role password management will be added here.")

st.divider()
st.caption("KP Snacks Pontefract · Inbound Delivery Management")

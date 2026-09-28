import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests

# Page setup must be the first Streamlit command
st.set_page_config(
    page_title="Grid Intermittency & Mitigation Digital Twin",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- CACHED WEATHER TELEMETRY (Open-Meteo API) ---
@st.cache_data(ttl=600)
def fetch_plant_weather(lat, lon):
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ["temperature_2m", "cloud_cover", "direct_normal_irradiance"],
            "timezone": "auto",
            "forecast_days": 1
        }
        res = requests.get(url, timeout=3).json()
        curr = res.get("current", {})
        return {
            "dni": float(curr.get("direct_normal_irradiance", 800.0)),
            "cloud_cover": float(curr.get("cloud_cover", 15.0)),
            "temp": float(curr.get("temperature_2m", 32.0)),
            "status": "Connected to Upstream Solar Park API"
        }
    except Exception:
        return {
            "dni": 850.0,
            "cloud_cover": 10.0,
            "temp": 33.0,
            "status": "Offline (Simulation Fallback Profile Active)"
        }

# --- SIDEBAR: PRESET SCENARIOS & HARDWARE ADJUSTMENTS ---
st.sidebar.title("🕹️ Substation Control Room")

scenario = st.sidebar.selectbox(
    "Select Live Evaluation Scenario:",
    [
        "Manual Tuning (Sliders Below)",
        "Scenario 1: Normal Grid Balanced",
        "Scenario 2: Cloud Dip -> BESS Dispatch (Stage 1)",
        "Scenario 3: Battery Low -> CVR Engaged (Stage 2)",
        "Scenario 4: High Deficit -> LCS Load Shedding (Stage 3)",
        "Scenario 5: Catastrophic Intermittency -> DISCOM Trip (Stage 4)"
    ]
)

# Presets configured for evaluation demonstrations
if scenario == "Scenario 1: Normal Grid Balanced":
    s_cloud, s_soc, s_temp, s_load = 10, 85, 30, 450
elif scenario == "Scenario 2: Cloud Dip -> BESS Dispatch (Stage 1)":
    s_cloud, s_soc, s_temp, s_load = 60, 75, 34, 600
elif scenario == "Scenario 3: Battery Low -> CVR Engaged (Stage 2)":
    s_cloud, s_soc, s_temp, s_load = 65, 15, 34, 600
elif scenario == "Scenario 4: High Deficit -> LCS Load Shedding (Stage 3)":
    s_cloud, s_soc, s_temp, s_load = 85, 10, 39, 1400
elif scenario == "Scenario 5: Catastrophic Intermittency -> DISCOM Trip (Stage 4)":
    s_cloud, s_soc, s_temp, s_load = 98, 5, 42, 1800
else:
    s_cloud, s_soc, s_temp, s_load = 20, 80, 33, 500

st.sidebar.markdown("---")
st.sidebar.subheader("Substation & Grid Parameters")
solar_capacity_kw = st.sidebar.slider("Upstream Solar Farm Rating (kW)", 50, 500, 200, 10)
cloud_cover_pct = st.sidebar.slider("Upstream Cloud Cover (%)", 0, 100, s_cloud, 5)
battery_soc = st.sidebar.slider("Substation BESS SoC (%)", 0, 100, s_soc, 5)
ambient_temp = st.sidebar.slider("Ambient Temperature (°C)", 20, 45, s_temp, 1)

st.sidebar.markdown("---")
st.sidebar.subheader("Neighborhood Demand Parameters")
num_houses = st.sidebar.slider("Connected Houses on Feeder", 10, 150, 50, 5)
base_load_w = st.sidebar.slider("Essential Base Load / House (W)", 200, 800, 350, 50)
hvac_load_w = st.sidebar.slider("Non-Essential (HVAC) / House (W)", 400, 2500, s_load, 50)

# Upstream reference coordinates: Kamuthi Solar Park, Tamil Nadu (9.355° N, 78.393° E)
weather = fetch_plant_weather(9.355, 78.393)

# --- PHYSICAL POWER COMPUTATIONS ---
effective_dni = 1000.0 * (1.0 - (cloud_cover_pct / 100.0) * 0.88)
p_generation_kw = max(0.0, solar_capacity_kw * (effective_dni / 1000.0))

temp_factor = 1.0 + max(0.0, (ambient_temp - 28.0) * 0.04)
essential_kw = (num_houses * base_load_w) / 1000.0
non_essential_kw = (num_houses * hvac_load_w * temp_factor) / 1000.0
gross_demand_kw = essential_kw + non_essential_kw

delta_p_kw = p_generation_kw - gross_demand_kw

# --- 4-STAGE PLC MITIGATION STATE MACHINE ---
active_stage = 0
bess_dispatch_kw = 0.0
cvr_reduction_kw = 0.0
lcs_shed_kw = 0.0
feeder_voltage = 230.0
breaker_status = "CLOSED"

# Proactive SMS warning (Triggers when upstream cloud cover indicates a drop >30%)
sms_alert = cloud_cover_pct >= 45

if delta_p_kw >= 0:
    active_stage = 0
    stage_title = "Stage 0: Normal Operation"
    stage_desc = "Upstream generation fully covers regional demand. Grid is balanced."
else:
    deficit = abs(delta_p_kw)

    # Stage 1: BESS (Safety interlock: SoC > 20%)
    if battery_soc > 20:
        active_stage = 1
        stage_title = "Stage 1: BESS Active Dispatch"
        bess_dispatch_kw = min(deficit, 100.0)
        stage_desc = f"Battery discharging {bess_dispatch_kw:.1f} kW to feeder busbar. Essential & Non-essential loads fully powered."

    # Stage 2: CVR (Safety interlock: Line V >= 218V)
    elif feeder_voltage >= 220.0 and deficit <= (gross_demand_kw * 0.04 + 15):
        active_stage = 2
        stage_title = "Stage 2: Conservation Voltage Reduction (CVR)"
        feeder_voltage = 218.0
        cvr_reduction_kw = gross_demand_kw * 0.035
        stage_desc = "Substation OLTC dropped voltage to 218V (0.95 pu). Shaved 3.5% feeder demand with zero consumer disturbance."

    # Stage 3: LCS Demand Shedding
    elif non_essential_kw > 0 and deficit <= (non_essential_kw + 20):
        active_stage = 3
        stage_title = "Stage 3: LCS Non-Essential Load Shedding"
        lcs_shed_kw = non_essential_kw
        stage_desc = f"Shunt-tripped non-essential contactors across all {num_houses} homes. Essential base-loads preserved."

    # Stage 4: DISCOM Cutoff
    else:
        active_stage = 4
        stage_title = "Stage 4: EMERGENCY FEEDER TRIP"
        breaker_status = "OPEN (ISLANDED)"
        feeder_voltage = 0.0
        stage_desc = "Deficit exceeds all local buffers. Main breaker opened to prevent transformer burnout. Incident pushed to DISCOM."

# --- MAIN DASHBOARD INTERFACE ---
st.title("⚡ Upstream Intermittency Mitigation & PLC Digital Twin")
st.caption(f"Upstream Station: {weather['status']} | Active Scenario: {scenario}")

# Operational Metrics Row
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Upstream Generation", f"{p_generation_kw:.1f} kW", f"DNI: {effective_dni:.0f} W/m²")
m2.metric("Gross Feeder Demand", f"{gross_demand_kw:.1f} kW", f"{num_houses} Homes")
m3.metric("Raw Gap (ΔP)", f"{delta_p_kw:.1f} kW", delta_color="normal" if delta_p_kw >= 0 else "inverse")
m4.metric("Feeder Voltage", f"{feeder_voltage:.1f} V", "-5.2%" if active_stage == 2 else ("0 V" if active_stage == 4 else "Nominal"))
m5.metric("BESS Reserve", f"{battery_soc}%", "Discharging" if active_stage == 1 else "Inhibited" if battery_soc <= 20 else "Standby")

st.markdown("---")

# Visual Status Banner
if active_stage == 0:
    st.success(f"🟢 **{stage_title}** — {stage_desc}")
elif active_stage == 1:
    st.info(f"🔵 **{stage_title}** — {stage_desc}")
elif active_stage == 2:
    st.warning(f"🟡 **{stage_title}** — {stage_desc}")
elif active_stage == 3:
    st.warning(f"🟠 **{stage_title}** — {stage_desc}")
else:
    st.error(f"🔴 **{stage_title}** — {stage_desc}")

# Proactive SMS Advisory Banner
if sms_alert:
    st.info("📲 **PROACTIVE SMS DISPATCHED TO LOCAL BUSINESSES:**\n\n"
            "*'GRID ADVISORY: 1-hour cloud cover drop detected at upstream plant. Generation reduced by >30%. "
            "Non-essential commercial loads may be shed. Please pause heavy lathe/welding/motor operations.'*")

# --- CHARTS SECTION ---
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📊 Dynamic Power Balance & Stage Mitigation")
    cat = ['Solar Output', 'Feeder Demand', 'BESS Support', 'CVR Shaved', 'LCS Shedded']
    vals = [p_generation_kw, gross_demand_kw, bess_dispatch_kw, cvr_reduction_kw, lcs_shed_kw]
    colors = ['#2ca02c', '#d62728', '#1f77b4', '#ff7f0e', '#9467bd']
    
    fig_bar = go.Figure(data=[go.Bar(
        x=cat, y=vals, text=[f"{v:.1f} kW" for v in vals],
        textposition='auto', marker_color=colors
    )])
    fig_bar.update_layout(yaxis_title="Power (kW)", height=380, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig_bar, use_container_width=True)

with col_right:
    st.subheader("📈 24-Hour Diurnal Intermittency Profile")
    hours = np.arange(0, 24)
    solar_profile = np.array([0,0,0,0,0,10,40,90,140,180,195,190,165,110,35,115,70,20,0,0,0,0,0,0]) * (solar_capacity_kw / 200.0)
    demand_profile = np.array([35,30,28,28,32,45,75,95,110,120,125,130,140,150,155,150,145,135,120,105,85,65,50,40]) * (gross_demand_kw / 155.0)

    fig_line = go.Figure()
    fig_line.add_trace(go.Scatter(x=hours, y=solar_profile, mode='lines+markers', name='Solar Gen (kW)', line=dict(color='green', width=2.5)))
    fig_line.add_trace(go.Scatter(x=hours, y=demand_profile, mode='lines+markers', name='Gross Load (kW)', line=dict(color='red', width=2.5, dash='dash')))
    
    fig_line.add_vrect(x0=13.2, x1=15.2, fillcolor="orange", opacity=0.25, line_width=0,
                       annotation_text="Intermittency Window (Cloud Dip)", annotation_position="top left")
    
    fig_line.update_layout(xaxis_title="Hour of Day", yaxis_title="Power (kW)", height=380, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig_line, use_container_width=True)

# --- PLC TELEMETRY STATUS TABLE ---
st.subheader("📋 Substation PLC Operational Status")
status_df = pd.DataFrame({
    "Parameter": ["Substation Bus Status", "BESS Relay State", "OLTC Tap Position", "Household Essential Bus", "Household Non-Essential Bus", "Main Vacuum Breaker"],
    "Value": [
        "ENERGIZED" if breaker_status == "CLOSED" else "DE-ENERGIZED",
        "CLOSED (Discharging)" if active_stage == 1 else "OPEN (Standby/Inhibited)",
        "TAP -2 (218V / 0.95 pu)" if active_stage == 2 else "TAP 0 (230V / 1.0 pu)",
        "POWERED" if active_stage < 4 else "SHUTDOWN",
        "POWERED" if active_stage < 3 else "DISCONNECTED (Shed)",
        breaker_status
    ],
    "Safety Interlock": [
        "Nominal Operating Limits",
        "SoC Floor: 20%",
        "ANSI C84.1 Voltage Floor: 218V",
        "Unconditional Life-Safety Priority",
        "Shed Upon CVR Exhaustion",
        "Anti-Islanding Thermal Trip"
    ]
})
st.dataframe(status_df, use_container_width=True, hide_index=True)

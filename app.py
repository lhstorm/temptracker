import sqlite3
import time

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DB = "dht11_readings.db"

# Set these to your location (placeholder: Vancouver, BC)
LAT, LON = 49.2827, -123.1207

WEATHER_REFRESH = 900  # seconds Open-Meteo data is cached for (15 min)

st.set_page_config(page_title="DHT11 Logger", layout="wide")


# ----------------------------------------------------------------------------
# Data access
# ----------------------------------------------------------------------------
def load_indoor(limit=500):
    """Read the most recent rows from the SQLite DB the logger writes to."""
    # timeout + WAL-friendly read; logger is the only writer
    conn = sqlite3.connect(DB, timeout=10)
    try:
        df = pd.read_sql_query(
            "SELECT timestamp, temperature, humidity "
            "FROM readings ORDER BY id DESC LIMIT ?",
            conn,
            params=(limit,),
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


@st.cache_data(ttl=WEATHER_REFRESH)
def fetch_outdoor(lat, lon):
    """Current outdoor conditions from Open-Meteo.

    Cached for WEATHER_REFRESH seconds so the API is only hit every 15 min
    no matter how often the dashboard reruns.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ["temperature_2m", "relative_humidity_2m", "surface_pressure"],
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    cur = r.json()["current"]
    return {
        "temp": cur["temperature_2m"],
        "humidity": cur["relative_humidity_2m"],
        "pressure": cur["surface_pressure"],
        "fetched": time.strftime("%H:%M:%S"),
    }


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.title("🌡️ DHT11 Sensor Dashboard")

# Sidebar controls
with st.sidebar:
    st.header("Settings")
    lat = st.number_input("Latitude", value=LAT, format="%.4f")
    lon = st.number_input("Longitude", value=LON, format="%.4f")
    limit = st.slider("Rows to show", 50, 2000, 500, step=50)
    auto = st.checkbox("Auto-refresh (5s)", value=True)
    st.caption("Outdoor data is cached for 15 min regardless of refresh.")

indoor = load_indoor(limit)

try:
    outdoor = fetch_outdoor(lat, lon)
    outdoor_ok = True
except Exception as e:  # network/API problems shouldn't crash the dashboard
    outdoor = None
    outdoor_ok = False
    st.warning(f"Couldn't fetch outdoor data: {e}")

# --- Current-value metric cards -------------------------------------------
c1, c2, c3, c4 = st.columns(4)

if not indoor.empty:
    latest = indoor.iloc[-1]
    in_temp = latest["temperature"]
    in_hum = latest["humidity"]
else:
    in_temp = in_hum = None

c1.metric("Indoor Temp", f"{in_temp}°C" if in_temp is not None else "--")
c2.metric("Indoor Humidity", f"{in_hum}%" if in_hum is not None else "--")

if outdoor_ok:
    # Delta = indoor minus outdoor, so you can see how much warmer the office is
    t_delta = (
        f"{in_temp - outdoor['temp']:+.1f}° vs out"
        if in_temp is not None
        else None
    )
    h_delta = (
        f"{in_hum - outdoor['humidity']:+.0f}% vs out"
        if in_hum is not None
        else None
    )
    c3.metric("Outdoor Temp", f"{outdoor['temp']}°C", t_delta, delta_color="inverse")
    c4.metric("Outdoor Humidity", f"{outdoor['humidity']}%", h_delta, delta_color="inverse")
    st.caption(
        f"Outdoor: {outdoor['pressure']} hPa · "
        f"last fetched {outdoor['fetched']} · ({lat:.3f}, {lon:.3f})"
    )
else:
    c3.metric("Outdoor Temp", "--")
    c4.metric("Outdoor Humidity", "--")

st.divider()

# --- Charts ----------------------------------------------------------------
if indoor.empty:
    st.info("No data yet. Make sure the logging script is running.")
else:
    plot_df = indoor.set_index("timestamp")

    st.subheader("Temperature")
    temp_df = plot_df[["temperature"]].rename(columns={"temperature": "Indoor °C"})
    if outdoor_ok:
        # Flat reference line at the current outdoor temp across the window
        temp_df["Outdoor °C (now)"] = outdoor["temp"]
    st.line_chart(temp_df)

    st.subheader("Humidity")
    hum_df = plot_df[["humidity"]].rename(columns={"humidity": "Indoor %"})
    if outdoor_ok:
        hum_df["Outdoor % (now)"] = outdoor["humidity"]
    st.line_chart(hum_df)

    with st.expander("Raw data"):
        st.dataframe(indoor.sort_values("timestamp", ascending=False), use_container_width=True)

# --- Auto-refresh ----------------------------------------------------------
if auto:
    time.sleep(5)
    st.rerun()

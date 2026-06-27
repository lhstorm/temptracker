import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DB = "dht11_readings.db"

# Set these to your location (placeholder: Vancouver, BC)
LAT, LON = 49.2827, -123.1207

WEATHER_REFRESH = 900          # current-conditions cache (15 min)
REALTIME_WINDOW_MIN = 60       # show last hour of indoor data
HISTORY_DAYS = 30              # how far back to pull archive
ARCHIVE_LAG_DAYS = 5           # ERA5 reanalysis lag; end_date = today - this

st.set_page_config(page_title="DHT11 Logger", layout="wide")


# ----------------------------------------------------------------------------
# DB setup — history table lives alongside the logger's `readings` table
# ----------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_history_table():
    conn = get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_history (
                ts            TEXT NOT NULL,
                latitude      REAL NOT NULL,
                longitude     REAL NOT NULL,
                temperature   REAL,
                humidity      REAL,
                pressure      REAL,
                wind_speed    REAL,
                precipitation REAL,
                PRIMARY KEY (ts, latitude, longitude)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Indoor data (the logger's table)
# ----------------------------------------------------------------------------
def load_indoor_window(minutes):
    conn = get_conn()
    try:
        df = pd.read_sql_query(
            "SELECT timestamp, temperature, humidity FROM readings "
            "ORDER BY id DESC LIMIT 5000",
            conn,
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    cutoff = datetime.now() - timedelta(minutes=minutes)
    df = df[df["timestamp"] >= cutoff]
    return df.sort_values("timestamp").reset_index(drop=True)


def load_indoor_hourly(days):
    conn = get_conn()
    try:
        df = pd.read_sql_query(
            "SELECT timestamp, temperature, humidity FROM readings", conn
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    cutoff = datetime.now() - timedelta(days=days)
    df = df[df["timestamp"] >= cutoff]
    if df.empty:
        return df
    hourly = (
        df.set_index("timestamp")
        .resample("1h")[["temperature", "humidity"]]
        .mean()
    )
    return hourly


# ----------------------------------------------------------------------------
# Current outdoor conditions (forecast endpoint)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=WEATHER_REFRESH)
def fetch_current(lat, lon):
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
# Historical outdoor data — fetched once, stored, gap-aware refetch.
# ----------------------------------------------------------------------------
HOURLY_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
    "wind_speed_10m",
    "precipitation",
]
COL_MAP = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "surface_pressure": "pressure",
    "wind_speed_10m": "wind_speed",
    "precipitation": "precipitation",
}


def _stored_date_range(lat, lon):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM weather_history "
            "WHERE latitude=? AND longitude=?",
            (lat, lon),
        ).fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    return (pd.to_datetime(row[0]).date(), pd.to_datetime(row[1]).date())


def _fetch_archive(lat, lon, start_date, end_date):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": HOURLY_VARS,
        "timezone": "UTC",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    h = r.json().get("hourly", {})
    if not h or not h.get("time"):
        return pd.DataFrame()
    df = pd.DataFrame(h).rename(columns={"time": "ts", **COL_MAP})
    return df


def _store_history(df, lat, lon):
    if df.empty:
        return
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO weather_history "
            "(ts, latitude, longitude, temperature, humidity, pressure, "
            " wind_speed, precipitation) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    row["ts"], lat, lon,
                    row.get("temperature"), row.get("humidity"),
                    row.get("pressure"), row.get("wind_speed"),
                    row.get("precipitation"),
                )
                for _, row in df.iterrows()
            ],
        )
        conn.commit()
    finally:
        conn.close()


def sync_history(lat, lon, days, lag_days):
    today = datetime.now(timezone.utc).date()
    want_end = today - timedelta(days=lag_days)
    want_start = today - timedelta(days=days)

    stored = _stored_date_range(lat, lon)

    ranges_to_fetch = []
    if stored is None:
        ranges_to_fetch.append((want_start, want_end))
    else:
        have_min, have_max = stored
        if want_start < have_min:
            ranges_to_fetch.append((want_start, have_min - timedelta(days=1)))
        if want_end > have_max:
            ranges_to_fetch.append((have_max + timedelta(days=1), want_end))

    for start, end in ranges_to_fetch:
        if start > end:
            continue
        try:
            df = _fetch_archive(lat, lon, start, end)
            _store_history(df, lat, lon)
        except Exception as e:
            st.warning(f"Archive fetch failed for {start}-{end}: {e}")

    conn = get_conn()
    try:
        out = pd.read_sql_query(
            "SELECT ts, temperature, humidity, pressure, wind_speed, precipitation "
            "FROM weather_history WHERE latitude=? AND longitude=? AND ts>=? "
            "ORDER BY ts",
            conn,
            params=(lat, lon, want_start.isoformat()),
        )
    finally:
        conn.close()
    if not out.empty:
        out["ts"] = pd.to_datetime(out["ts"])
    return out, ranges_to_fetch


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
init_history_table()

st.title("DHT11 Sensor Dashboard")

with st.sidebar:
    st.header("Settings")
    lat = st.number_input("Latitude", value=LAT, format="%.4f")
    lon = st.number_input("Longitude", value=LON, format="%.4f")
    auto = st.checkbox("Auto-refresh realtime (5s)", value=True)
    st.caption(
        f"Realtime window: last {REALTIME_WINDOW_MIN} min - "
        f"History: last {HISTORY_DAYS} days (ERA5, ~{ARCHIVE_LAG_DAYS}-day lag)."
    )

try:
    current = fetch_current(lat, lon)
    current_ok = True
except Exception as e:
    current, current_ok = None, False
    st.warning(f"Couldn't fetch current outdoor data: {e}")

rt = load_indoor_window(REALTIME_WINDOW_MIN)

c1, c2, c3, c4 = st.columns(4)
if not rt.empty:
    in_temp = rt.iloc[-1]["temperature"]
    in_hum = rt.iloc[-1]["humidity"]
else:
    in_temp = in_hum = None
c1.metric("Indoor Temp", f"{in_temp}C" if in_temp is not None else "--")
c2.metric("Indoor Humidity", f"{in_hum}%" if in_hum is not None else "--")
if current_ok:
    c3.metric("Outdoor Temp", f"{current['temp']}C")
    c4.metric("Outdoor Humidity", f"{current['humidity']}%")
    st.caption(f"Outdoor now: {current['pressure']} hPa - fetched {current['fetched']}")
else:
    c3.metric("Outdoor Temp", "--")
    c4.metric("Outdoor Humidity", "--")

# Realtime: two plots side by side, last hour, labelled y-axes
st.subheader(f"Realtime - last {REALTIME_WINDOW_MIN} minutes")
left, right = st.columns(2)

if rt.empty:
    left.info("No recent indoor data. Is the logger running?")
    right.info("No recent indoor data.")
else:
    rt_idx = rt.set_index("timestamp")
    with left:
        st.markdown("**Temperature**")
        st.line_chart(rt_idx[["temperature"]], y_label="Temperature (C)", x_label="Time")
    with right:
        st.markdown("**Humidity**")
        st.line_chart(rt_idx[["humidity"]], y_label="Relative humidity (%)", x_label="Time")

st.divider()

# Historical: one plot per variable, indoor overlaid where logged
st.subheader(f"Historical outdoor - last {HISTORY_DAYS} days")

hist, fetched_ranges = sync_history(lat, lon, HISTORY_DAYS, ARCHIVE_LAG_DAYS)

if fetched_ranges:
    nice = ", ".join(f"{s}->{e}" for s, e in fetched_ranges if s <= e)
    if nice:
        st.caption(f"Fetched new archive range(s) this run: {nice}")
else:
    st.caption("Served entirely from local cache - no API call needed.")

if hist.empty:
    st.info("No historical data available yet.")
else:
    indoor_hourly = load_indoor_hourly(HISTORY_DAYS)
    hist_idx = hist.set_index("ts")

    st.markdown("**Temperature (C)** - outdoor vs indoor")
    temp_plot = hist_idx[["temperature"]].rename(columns={"temperature": "Outdoor"})
    if not indoor_hourly.empty:
        temp_plot = temp_plot.join(indoor_hourly["temperature"].rename("Indoor"), how="outer")
    st.line_chart(temp_plot, y_label="Temperature (C)", x_label="Date")

    st.markdown("**Relative humidity (%)** - outdoor vs indoor")
    hum_plot = hist_idx[["humidity"]].rename(columns={"humidity": "Outdoor"})
    if not indoor_hourly.empty:
        hum_plot = hum_plot.join(indoor_hourly["humidity"].rename("Indoor"), how="outer")
    st.line_chart(hum_plot, y_label="Relative humidity (%)", x_label="Date")

    st.markdown("**Surface pressure (hPa)**")
    st.line_chart(
        hist_idx[["pressure"]].rename(columns={"pressure": "Outdoor"}),
        y_label="Pressure (hPa)", x_label="Date",
    )

    st.markdown("**Wind speed (km/h)**")
    st.line_chart(
        hist_idx[["wind_speed"]].rename(columns={"wind_speed": "Outdoor"}),
        y_label="Wind speed (km/h)", x_label="Date",
    )

    st.markdown("**Precipitation (mm)**")
    st.line_chart(
        hist_idx[["precipitation"]].rename(columns={"precipitation": "Outdoor"}),
        y_label="Precipitation (mm)", x_label="Date",
    )

    with st.expander("Stored history (raw)"):
        st.dataframe(hist.sort_values("ts", ascending=False), use_container_width=True)

if auto:
    time.sleep(5)
    st.rerun()# Sidebar controls
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

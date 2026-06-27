import time
import sqlite3
import board
import adafruit_dht

dht = adafruit_dht.DHT11(board.D4)  # GPIO4

# Set up the database
conn = sqlite3.connect("dht11_readings.db")
cur = conn.cursor()
cur.execute("""
    CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        temperature REAL,
        humidity REAL
    )
""")
conn.commit()

while True:
    try:
        temp = dht.temperature
        humidity = dht.humidity
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        cur.execute(
            "INSERT INTO readings (timestamp, temperature, humidity) VALUES (?, ?, ?)",
            (timestamp, temp, humidity),
        )
        conn.commit()

        print(f"{timestamp}  {temp}°C  {humidity}% RH")
    except RuntimeError as e:
        # DHT sensors throw occasional read errors; just retry
        print("Read error, retrying:", e.args[0])
    time.sleep(2)

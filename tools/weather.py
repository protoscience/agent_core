"""Open-Meteo forecast client (free, no API key)."""
import time
import httpx
from datetime import datetime, timezone


# 20148 Ashburn, VA
DEFAULT_LATITUDE = 39.0168
DEFAULT_LONGITUDE = -77.5153
DEFAULT_LABEL = "Ashburn, VA · 20148"
DEFAULT_TZ = "America/New_York"


# WMO weather codes → (emoji, short label). Subset sufficient for card rendering.
WMO = {
    0:  ("☀️", "Clear"),
    1:  ("🌤️", "Mostly clear"),
    2:  ("⛅", "Partly cloudy"),
    3:  ("☁️", "Overcast"),
    45: ("🌫️", "Fog"),
    48: ("🌫️", "Icy fog"),
    51: ("🌦️", "Light drizzle"),
    53: ("🌦️", "Drizzle"),
    55: ("🌦️", "Heavy drizzle"),
    56: ("🌧️", "Freezing drizzle"),
    57: ("🌧️", "Freezing drizzle"),
    61: ("🌧️", "Light rain"),
    63: ("🌧️", "Rain"),
    65: ("🌧️", "Heavy rain"),
    66: ("🌧️", "Freezing rain"),
    67: ("🌧️", "Freezing rain"),
    71: ("🌨️", "Light snow"),
    73: ("🌨️", "Snow"),
    75: ("❄️", "Heavy snow"),
    77: ("❄️", "Snow grains"),
    80: ("🌦️", "Showers"),
    81: ("🌧️", "Showers"),
    82: ("🌧️", "Heavy showers"),
    85: ("🌨️", "Snow showers"),
    86: ("❄️", "Snow showers"),
    95: ("⛈️", "Thunderstorm"),
    96: ("⛈️", "Thunderstorm w/ hail"),
    99: ("⛈️", "Severe thunderstorm"),
}


def describe(code: int) -> tuple[str, str]:
    return WMO.get(int(code), ("🌡️", "Unknown"))


_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def wind_compass(degrees: float | None) -> str:
    if degrees is None:
        return "—"
    idx = int((degrees % 360) / 45 + 0.5) % 8
    return _COMPASS[idx]


def fetch_forecast(
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
    tz: str = DEFAULT_TZ,
    hours: int = 24,
) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": tz,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "current": "temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m,wind_direction_10m,apparent_temperature",
        "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m,wind_direction_10m,apparent_temperature",
        "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code",
        "forecast_hours": hours,
        "forecast_days": 2,
    }
    # Open-Meteo occasionally returns 5xx; retry transient failures with backoff.
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            r = httpx.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=15.0)
            if 500 <= r.status_code < 600:
                raise httpx.HTTPStatusError(f"upstream {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            last_exc = e
            if attempt == 3:
                break
            time.sleep(2 ** attempt * 5)  # 5s, 10s, 20s
    raise last_exc  # type: ignore[misc]


def _local_hour_label(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str)
    h = dt.hour
    suffix = "AM" if h < 12 else "PM"
    disp = h % 12 or 12
    return f"{disp}{suffix}"


def build_card_view(data: dict, label: str = DEFAULT_LABEL) -> dict:
    current = data.get("current", {})
    hourly = data.get("hourly", {})
    daily = data.get("daily", {})

    cur_code = int(current.get("weather_code", 0))
    cur_icon, cur_label = describe(cur_code)

    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    precips = hourly.get("precipitation_probability", [])
    codes = hourly.get("weather_code", [])
    wind_speeds = hourly.get("wind_speed_10m", [])
    wind_dirs = hourly.get("wind_direction_10m", [])

    slots = []
    for i in range(min(24, len(times))):
        icon, _ = describe(int(codes[i]) if i < len(codes) else 0)
        slots.append({
            "hour": _local_hour_label(times[i]),
            "icon": icon,
            "temp": round(temps[i]) if i < len(temps) else None,
            "precip": int(precips[i]) if i < len(precips) and precips[i] is not None else 0,
            "wind_speed": round(wind_speeds[i]) if i < len(wind_speeds) and wind_speeds[i] is not None else None,
            "wind_dir": wind_compass(wind_dirs[i] if i < len(wind_dirs) else None),
        })

    # Today highs/lows/precip peak (from daily[0])
    def _day_val(key, idx=0, default=None):
        arr = daily.get(key) or []
        return arr[idx] if idx < len(arr) else default

    high = _day_val("temperature_2m_max")
    low = _day_val("temperature_2m_min")
    precip_peak = _day_val("precipitation_probability_max") or 0
    sunrise_iso = _day_val("sunrise")
    sunset_iso = _day_val("sunset")

    def _fmt_sun(iso):
        if not iso:
            return "—"
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%-I:%M %p")

    return {
        "label": label,
        "generated_at": datetime.now(timezone.utc).astimezone().strftime("%a %b %-d, %Y · %-I:%M %p"),
        "current_temp": round(current.get("temperature_2m") or 0),
        "current_feels": round(current.get("apparent_temperature") or 0),
        "current_icon": cur_icon,
        "current_label": cur_label,
        "current_humidity": int(current.get("relative_humidity_2m") or 0),
        "current_wind": round(current.get("wind_speed_10m") or 0),
        "high": round(high) if high is not None else None,
        "low": round(low) if low is not None else None,
        "precip_peak": int(precip_peak),
        "sunrise": _fmt_sun(sunrise_iso),
        "sunset": _fmt_sun(sunset_iso),
        "slots": slots,
    }


def format_message(view: dict) -> str:
    """WhatsApp-friendly text message with emojis. Uses *bold* which WhatsApp
    renders natively. Shows every 3rd hour to keep the message short."""
    lines = [
        f"{view['current_icon']} *{view['label']}*",
        f"{view['generated_at']}",
        "",
        f"*Now* {view['current_temp']}° · {view['current_label']}",
        f"Feels {view['current_feels']}° · 💧 {view['current_humidity']}% · 💨 {view['current_wind']} mph",
        f"High {view['high']}° / Low {view['low']}° · {view['precip_peak']}% precip peak",
        f"🌅 {view['sunrise']} · 🌇 {view['sunset']}",
        "",
        "*Next 24 hours:*",
    ]
    slots = view.get("slots", [])
    # Every 2nd hour — 12 lines covering the full 24h horizon.
    for s in slots[::2]:
        hour = f"{s['hour']:<5}"
        temp = f"{s['temp']}°".rjust(4) if s['temp'] is not None else "—"
        precip = f"{s['precip']:>2}%"
        wind = f"{s['wind_speed']} mph" if s['wind_speed'] is not None else "—"
        lines.append(f"{hour} {s['icon']}  {temp}  ·  {precip} rain  ·  💨 {wind}")
    return "\n".join(lines)

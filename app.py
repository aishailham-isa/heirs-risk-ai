import re
import csv
import os
from datetime import datetime, timedelta
from io import BytesIO

import ee
import folium
import requests
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

st.set_page_config(page_title="RiskEye", page_icon="🛰️", layout="wide")

st.markdown("""
    <style>
    .stMetric {
        background-color: #F7F5EF;
        padding: 18px 16px;
        border-radius: 12px;
        border: 1px solid #E5E0D3;
    }
    div[data-testid="stMetricValue"] { font-size: 19px; white-space: normal; }
    div[data-testid="stMetricLabel"] { font-size: 13px; color: #555555; }
    .block-container { padding-top: 2rem; max-width: 1100px; }
    h2, h3 { color: #1F5C3F; }
    .section-divider { margin: 28px 0 18px 0; }
    </style>
""", unsafe_allow_html=True)

HISTORY_FILE = "assessment_history.csv"


@st.cache_resource
def init_ee():
    service_account_info = dict(st.secrets["gcp_service_account"])
    credentials = ee.ServiceAccountCredentials(
        service_account_info["client_email"],
        key_data=service_account_info["private_key"]
    )
    ee.Initialize(credentials, project=service_account_info["project_id"])


init_ee()

BUILDING_COST_BENCHMARKS = {
    "Residential": 250000,
    "Commercial / Office": 350000,
    "Warehouse / Industrial": 200000,
    "High-end / Luxury": 500000,
}


def clean_address(address):
    patterns_to_remove = [
        r'\bbehind\b.*?(?=,|$)', r'\bbeside\b.*?(?=,|$)',
        r'\bopposite\b.*?(?=,|$)', r'\bnear\b.*?(?=,|$)',
        r'\bplaza\b', r'\bshopping complex\b',
    ]
    cleaned = address
    for pattern in patterns_to_remove:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*,\s*,', ',', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ,')
    return cleaned


def geocode_with_google(address, api_key):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": api_key}
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data.get("status") == "OK" and data.get("results"):
            result = data["results"][0]
            lat = result["geometry"]["location"]["lat"]
            lng = result["geometry"]["location"]["lng"]
            formatted_address = result["formatted_address"]
            return lat, lng, formatted_address
    except Exception:
        pass
    return None, None, None


def geocode_address(address):
    geolocator = Nominatim(user_agent="riskeye-app")

    try:
        location = geolocator.geocode(address, timeout=5)
        if location:
            return location.latitude, location.longitude, location.address, "free (OpenStreetMap)"
    except Exception:
        location = None

    cleaned = clean_address(address)
    if cleaned != address:
        try:
            location = geolocator.geocode(cleaned, timeout=5)
            if location:
                return location.latitude, location.longitude, location.address, "free (OpenStreetMap)"
        except Exception:
            pass

    if "gcp_static_maps" in st.secrets:
        api_key = st.secrets["gcp_static_maps"]["api_key"]
        lat, lng, formatted_address = geocode_with_google(address, api_key)
        if lat is not None:
            return lat, lng, formatted_address, "Google (paid tier)"

    return None, None, None, None


def score_of(risk_text):
    risk_levels = {"Low": 1, "Medium": 2, "High": 3}
    return risk_levels.get(risk_text.split(" ")[0], 1)


def get_static_map_image(lat, lon, api_key):
    url = "https://maps.googleapis.com/maps/api/staticmap"
    params = {
        "center": f"{lat},{lon}", "zoom": 19, "size": "640x400",
        "maptype": "satellite", "markers": f"color:red|{lat},{lon}", "key": api_key,
    }
    response = requests.get(url, params=params, timeout=10)
    if response.status_code == 200:
        return response.content
    return None


def get_street_view_image(lat, lon, api_key):
    metadata_url = "https://maps.googleapis.com/maps/api/streetview/metadata"
    params = {"location": f"{lat},{lon}", "key": api_key}
    try:
        response = requests.get(metadata_url, params=params, timeout=10)
        metadata = response.json()
    except Exception:
        return None, "REQUEST_FAILED"

    if metadata.get("status") != "OK":
        return None, metadata.get("status", "UNKNOWN")

    image_url = "https://maps.googleapis.com/maps/api/streetview"
    image_params = {"size": "640x400", "location": f"{lat},{lon}", "key": api_key}
    image_response = requests.get(image_url, params=image_params, timeout=10)
    if image_response.status_code == 200:
        return image_response.content, "OK"
    return None, "FETCH_FAILED"


def render_interactive_google_map(lat, lon, api_key, height=550):
    html = f"""
    <div id="map" style="height:{height}px;width:100%;border-radius:10px;overflow:hidden;"></div>
    <script src="https://maps.googleapis.com/maps/api/js?key={api_key}"></script>
    <script>
      function initMap() {{
        var location = {{ lat: {lat}, lng: {lon} }};
        var map = new google.maps.Map(document.getElementById("map"), {{
          zoom: 18, center: location, mapTypeId: "satellite",
        }});
        new google.maps.Marker({{ position: location, map: map }});
      }}
      window.onload = initMap;
    </script>
    """
    components.html(html, height=height)


def get_nearby_places_count(lat, lon, radius_m, place_type, api_key):
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lon}",
        "radius": radius_m,
        "type": place_type,
        "key": api_key,
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        status = data.get("status")
        if status == "OK":
            count = len(data.get("results", []))
            if data.get("next_page_token"):
                count = f"{count}+"
            return count, None
        elif status == "ZERO_RESULTS":
            return 0, None
        else:
            return None, status
    except Exception as e:
        return None, str(e)[:80]


def get_nearby_hazards(lat, lon, api_key):
    fuel_count, fuel_err = get_nearby_places_count(lat, lon, 1000, "gas_station", api_key)
    hosp_count, hosp_err = get_nearby_places_count(lat, lon, 2000, "hospital", api_key)
    school_count, school_err = get_nearby_places_count(lat, lon, 1000, "school", api_key)
    fire_count, fire_err = get_nearby_places_count(lat, lon, 1500, "fire_station", api_key)
    commercial_count, commercial_err = get_nearby_places_count(lat, lon, 500, "store", api_key)
    return {
        "filling_stations": fuel_count, "filling_stations_error": fuel_err,
        "hospitals": hosp_count, "hospitals_error": hosp_err,
        "schools": school_count, "schools_error": school_err,
        "fire_stations": fire_count, "fire_stations_error": fire_err,
        "commercial_nearby": commercial_count, "commercial_error": commercial_err,
    }


def get_area_character(commercial_count):
    """Rough inference of area character based on nearby commercial activity — NOT building detection."""
    if commercial_count is None:
        return "Unknown (data unavailable)"

    if isinstance(commercial_count, str):
        numeric_part = commercial_count.replace("+", "")
        try:
            commercial_count = int(numeric_part)
        except ValueError:
            return "Unknown (data unavailable)"

    if commercial_count == 0:
        return "Likely residential (few nearby businesses detected)"
    elif commercial_count <= 3:
        return "Mixed residential/commercial (some nearby businesses)"
    else:
        return "Likely commercial area (many nearby businesses)"



WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm",
}


def get_weather(lat, lon):
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "current_weather": True},
            timeout=10
        )
        data = response.json()
        current = data.get("current_weather", {})
        code = current.get("weathercode")
        return {
            "temperature_c": current.get("temperature"),
            "windspeed_kmh": current.get("windspeed"),
            "condition": WEATHER_CODES.get(code, "Unknown"),
        }
    except Exception:
        return None


def get_historical_weather_summary(lat, lon, days_back=365):
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days_back)
    try:
        response = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            },
            timeout=15
        )
        data = response.json()
        daily = data.get("daily", {})
        precipitation = daily.get("precipitation_sum", [])
        temp_max = daily.get("temperature_2m_max", [])

        if not precipitation:
            return None

        rainy_days = sum(1 for p in precipitation if p and p > 1.0)
        total_rain = sum(p for p in precipitation if p)
        rainy_pct = round((rainy_days / len(precipitation)) * 100) if precipitation else 0
        valid_temps = [t for t in temp_max if t is not None]
        avg_max_temp = sum(valid_temps) / len(valid_temps) if valid_temps else None

        if total_rain < 1000:
            rainfall_label = "Low annual rainfall"
        elif total_rain < 1800:
            rainfall_label = "Moderate to typical annual rainfall for coastal Nigeria"
        else:
            rainfall_label = "High annual rainfall"

        return {
            "period_days": days_back,
            "rainy_days": rainy_days,
            "rainy_pct": rainy_pct,
            "total_rainfall_mm": round(total_rain, 1),
            "rainfall_label": rainfall_label,
            "avg_max_temp_c": round(avg_max_temp, 1) if avg_max_temp else None,
        }
    except Exception:
        return None


def search_planet_imagery(lat, lon, api_key, days_back=14):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)

    geometry = {"type": "Point", "coordinates": [lon, lat]}

    search_request = {
        "item_types": ["PSScene"],
        "filter": {
            "type": "AndFilter",
            "config": [
                {"type": "GeometryFilter", "field_name": "geometry", "config": geometry},
                {
                    "type": "DateRangeFilter",
                    "field_name": "acquired",
                    "config": {
                        "gte": start_date.strftime("%Y-%m-%dT00:00:00.000Z"),
                        "lte": end_date.strftime("%Y-%m-%dT23:59:59.000Z")
                    }
                },
                {"type": "RangeFilter", "field_name": "cloud_cover", "config": {"lte": 0.2}}
            ]
        }
    }

    try:
        response = requests.post(
            "https://api.planet.com/data/v1/quick-search",
            auth=(api_key, ""),
            json=search_request,
            timeout=20
        )
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}: {response.text[:150]}"

        data = response.json()
        features = data.get("features", [])
        if not features:
            return None, "No clear Planet imagery found in this date range for this location."

        best = min(features, key=lambda f: f["properties"].get("cloud_cover", 1))
        return {
            "id": best["id"],
            "acquired": best["properties"].get("acquired"),
            "cloud_cover": best["properties"].get("cloud_cover"),
            "thumbnail_url": best.get("_links", {}).get("thumbnail"),
        }, None
    except Exception as e:
        return None, str(e)[:150]


def get_planet_thumbnail(thumbnail_url, api_key):
    try:
        response = requests.get(thumbnail_url, auth=(api_key, ""), timeout=20)
        if response.status_code == 200:
            return response.content
    except Exception:
        pass
    return None


def log_assessment(result):
    try:
        file_exists = os.path.isfile(HISTORY_FILE)
        with open(HISTORY_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "address", "latitude", "longitude", "overall_risk",
                    "overall_percent", "flood_risk", "terrain_risk", "declared_value",
                    "estimated_replacement_cost"
                ])
            estimated_cost = result.get("estimated_built_sqm", 0) * result.get("cost_per_sqm", 0)
            writer.writerow([
                datetime.now().isoformat(), result.get("resolved_address"),
                result.get("latitude"), result.get("longitude"),
                result.get("overall_label"), result.get("overall_percent"),
                result.get("flood_risk"), result.get("terrain_risk"),
                result.get("declared_value", 0), estimated_cost
            ])
        return True
    except Exception:
        return False


def load_history():
    if not os.path.isfile(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)
def generate_pdf_report(result):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 20 * mm
    y = height - margin

    c.setFillColorRGB(0.12, 0.36, 0.25)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(margin, y, "RiskEye — Property Risk Report")
    y -= 10 * mm

    c.setFillColorRGB(0.33, 0.33, 0.33)
    c.setFont("Helvetica-Oblique", 10)
    c.drawString(margin, y, f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}")
    y -= 12 * mm

    c.setStrokeColorRGB(0.71, 0.4, 0.11)
    c.setLineWidth(1.5)
    c.line(margin, y, width - margin, y)
    y -= 10 * mm

    def section_title(text):
        nonlocal y
        c.setFillColorRGB(0.12, 0.36, 0.25)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(margin, y, text)
        y -= 7 * mm

    def line(text, bold=False):
        nonlocal y
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.drawString(margin, y, text)
        y -= 6 * mm

    section_title("Location")
    line(f"Address: {result.get('resolved_address', 'N/A')}")
    line(f"Coordinates: {result.get('latitude'):.5f}, {result.get('longitude'):.5f}")
    y -= 4 * mm

    section_title("Overall Risk Assessment")
    line(f"Overall Risk: {result.get('overall_label')} ({result.get('overall_percent')}%)", bold=True)
    line(f"Flood Exposure: {result.get('flood_risk')}")
    line(f"Terrain Risk: {result.get('terrain_risk')}")
    line(f"Surroundings: {result.get('surroundings')}")
    line(f"Recommendation: {result.get('recommendation')}")
    y -= 4 * mm

    section_title("Recommended Actions")
    for action in result.get("actions", []):
        line(f"- {action}")
    y -= 4 * mm

    hazards = result.get("hazards")
    if hazards:
        section_title("Nearby Infrastructure")
        line(f"Filling stations within 1km: {hazards.get('filling_stations', 'N/A')}")
        line(f"Hospitals within 2km: {hazards.get('hospitals', 'N/A')}")
        line(f"Schools within 1km: {hazards.get('schools', 'N/A')}")
        line(f"Fire stations within 1.5km: {hazards.get('fire_stations', 'N/A')}")
        line(f"Area character: {result.get('area_character', 'N/A')}")
        y -= 4 * mm

    weather = result.get("weather")
    if weather:
        section_title("Weather at Time of Assessment")
        line(f"Condition: {weather.get('condition', 'N/A')}")
        line(f"Temperature: {weather.get('temperature_c', 'N/A')} °C")
        line(f"Wind speed: {weather.get('windspeed_kmh', 'N/A')} km/h")
        y -= 4 * mm

    hist_weather = result.get("historical_weather")
    if hist_weather:
        section_title(f"Rainfall Pattern (last {hist_weather.get('period_days')} days)")
        line(f"{hist_weather.get('rainfall_label')}")
        line(f"Rained on {hist_weather.get('rainy_days')} days ({hist_weather.get('rainy_pct')}% of period)")
        line(f"Total rainfall: {hist_weather.get('total_rainfall_mm')} mm")
        y -= 4 * mm

    if result.get("declared_value", 0) > 0:
        section_title("Sum Insured Check")
        estimated_cost = result.get("estimated_built_sqm", 0) * result.get("cost_per_sqm", 0)
        line(f"Building type: {result.get('building_type')}")
        line(f"Estimated built-up area: {result.get('estimated_built_sqm'):,.0f} sqm")
        line(f"Estimated replacement cost: N{estimated_cost:,.0f}")
        line(f"Declared value: N{result.get('declared_value'):,.0f}")
        y -= 4 * mm

    c.setFont("Helvetica-Oblique", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(margin, margin, "Indicative screening report only — not a certified valuation or survey. Physical or drone inspection advised where flagged.")

    c.save()
    buffer.seek(0)
    return buffer


def run_assessment(latitude, longitude, resolved_address, api_key):
    point = ee.Geometry.Point([longitude, latitude])
    area = point.buffer(500)
    search_area = point.buffer(1000)
    footprint_area = point.buffer(30)

    collection = (
        ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
        .filterBounds(area)
        .filterDate('2026-01-01', '2026-09-02')
        .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 15))
        .sort('CLOUDY_PIXEL_PERCENTAGE')
    )
    count = collection.size().getInfo()
    if count == 0:
        return None

    best_image = collection.first()
    image_date = ee.Date(best_image.get('system:time_start')).format('YYYY-MM-dd').getInfo()
    cloud_pct = round(best_image.get('CLOUDY_PIXEL_PERCENTAGE').getInfo(), 1)

    water = ee.Image('JRC/GSW1_4/GlobalSurfaceWater').select('occurrence')
    water_mask = water.gt(50)
    water_distance = water_mask.fastDistanceTransform(30).sqrt().multiply(30)
    distance_dict = water_distance.reduceRegion(
        reducer=ee.Reducer.min(), geometry=search_area, scale=30, maxPixels=1e9
    ).getInfo()
    distance_value = list(distance_dict.values())[0] if distance_dict else None

    if distance_value is None:
        flood_risk = "Low"
    else:
        distance_value = round(distance_value, 1)
        flood_risk = "High" if distance_value < 100 else "Medium" if distance_value < 500 else "Low"

    elevation = ee.Image('USGS/SRTMGL1_003')
    slope = ee.Terrain.slope(elevation)
    terrain_stats = slope.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=area, scale=30, maxPixels=1e9
    ).getInfo()
    avg_slope = list(terrain_stats.values())[0] if terrain_stats else None

    if avg_slope is None:
        terrain_risk = "Unknown"
    else:
        avg_slope = round(avg_slope, 2)
        terrain_risk = (
            "Low — flat, may pool water" if avg_slope < 2
            else "Medium — moderate slope" if avg_slope < 8
            else "High — steep, erosion risk"
        )

    worldcover = ee.ImageCollection('ESA/WorldCover/v200').first()
    landcover_stats = worldcover.reduceRegion(
        reducer=ee.Reducer.mode(), geometry=area, scale=10, maxPixels=1e9
    ).getInfo()
    landcover_code = list(landcover_stats.values())[0] if landcover_stats else None
    landcover_labels = {
        10: "Tree cover", 20: "Shrubland", 30: "Grassland", 40: "Cropland",
        50: "Built-up area", 60: "Bare/sparse vegetation",
        70: "Snow/ice", 80: "Water body", 90: "Wetland", 95: "Mangroves", 100: "Moss/lichen",
    }
    surroundings = landcover_labels.get(landcover_code, "Unknown")

    built_mask = worldcover.eq(50)
    pixel_area = ee.Image.pixelArea()
    built_area_img = built_mask.multiply(pixel_area)
    built_area_stats = built_area_img.reduceRegion(
        reducer=ee.Reducer.sum(), geometry=footprint_area, scale=10, maxPixels=1e9
    ).getInfo()
    estimated_built_sqm = list(built_area_stats.values())[0] if built_area_stats else 0
    estimated_built_sqm = round(estimated_built_sqm, 0) if estimated_built_sqm else 0

    flood_score = score_of(flood_risk)
    terrain_score = score_of(terrain_risk)
    overall_score = max(flood_score, terrain_score)
    overall_label = {1: "LOW", 2: "MEDIUM", 3: "HIGH"}[overall_score]
    overall_percent = round(((flood_score + terrain_score) / 2) / 3 * 100)

    recommendation = "Physical inspection advised" if overall_score >= 2 else "Remote screening sufficient"

    actions = []
    if flood_risk == "High":
        actions.append("Flood exposure is high: recommend flood barriers, elevated foundations, or improved drainage before binding cover.")
    elif flood_risk == "Medium":
        actions.append("Moderate flood exposure: recommend confirming drainage adequacy during inspection.")
    if terrain_risk.startswith("High"):
        actions.append("Steep terrain detected: recommend erosion control and a structural/foundation assessment.")
    elif terrain_risk.startswith("Low"):
        actions.append("Very flat terrain: recommend checking drainage, as flat land can pool water during heavy rain.")
    if not actions:
        actions.append("No significant location-based concerns detected from available data.")

    hazards = get_nearby_hazards(latitude, longitude, api_key) if api_key else {}
    area_character = get_area_character(hazards.get("commercial_nearby"))
    weather = get_weather(latitude, longitude)
    historical_weather = get_historical_weather_summary(latitude, longitude)

    return {
        "resolved_address": resolved_address, "image_date": image_date, "cloud_pct": cloud_pct,
        "flood_risk": flood_risk, "terrain_risk": terrain_risk, "surroundings": surroundings,
        "overall_label": overall_label, "overall_percent": overall_percent,
        "recommendation": recommendation, "actions": actions,
        "best_image": best_image, "area": area, "latitude": latitude, "longitude": longitude,
        "estimated_built_sqm": estimated_built_sqm, "overall_score": overall_score,
        "hazards": hazards, "area_character": area_character,
        "weather": weather, "historical_weather": historical_weather,
    }


# ============================== UI ==============================

st.title("🛰️ RiskEye")
st.caption("AI-assisted property risk screening using satellite imagery")

st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
st.markdown("### 📍 Property Location")
address = st.text_input("Property address", placeholder="e.g. Wuye, Abuja, Nigeria", label_visibility="collapsed")

with st.expander("Enter coordinates manually instead"):
    col1, col2 = st.columns(2)
    with col1:
        manual_lat = st.text_input("Latitude")
    with col2:
        manual_lon = st.text_input("Longitude")

st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
st.markdown("### Sum Insured Check (optional)")
st.caption("Select the building type and enter the declared value to check for possible underinsurance. This is an indicative estimate, not a certified valuation.")

col3, col4, col5 = st.columns(3)
with col3:
    declared_value = st.number_input("Declared value (₦)", min_value=0, step=1000000, value=0)
with col4:
    building_type = st.selectbox("Building type", list(BUILDING_COST_BENCHMARKS.keys()))
with col5:
    default_cost = BUILDING_COST_BENCHMARKS[building_type]
    cost_per_sqm = st.number_input("Cost benchmark (₦/sqm)", min_value=0, step=10000, value=default_cost)

st.write("")
run_clicked = st.button(" Run Risk Assessment", type="primary", use_container_width=False)

if run_clicked:
    st.session_state.result = None

    if manual_lat and manual_lon:
        latitude, longitude, resolved_address = float(manual_lat), float(manual_lon), "Manually entered coordinates"
        geocode_source = "manual entry"
    elif address:
        with st.spinner("Looking up address..."):
            latitude, longitude, resolved_address, geocode_source = geocode_address(address)
    else:
        st.warning("Please enter an address or coordinates.")
        st.stop()

    if latitude is None:
        st.error("Could not find that address, even with the extended lookup. Try entering coordinates manually above.")
    else:
        places_api_key = st.secrets["gcp_static_maps"]["api_key"] if "gcp_static_maps" in st.secrets else None
        with st.spinner("Analyzing satellite imagery, nearby infrastructure, and weather..."):
            result = run_assessment(latitude, longitude, resolved_address, places_api_key)

        if result is None:
            st.error("No clear satellite image found for this location.")
        else:
            result["declared_value"] = declared_value
            result["cost_per_sqm"] = cost_per_sqm
            result["building_type"] = building_type
            result["geocode_source"] = geocode_source
            st.session_state.result = result
            log_assessment(result)

if "result" in st.session_state and st.session_state.result:
    result = st.session_state.result
    risk_color = {"LOW": "green", "MEDIUM": "orange", "HIGH": "red"}[result["overall_label"]]

    st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
    st.markdown("## 📋 Assessment Result")

    top_col1, top_col2 = st.columns([2.5, 1])
    with top_col1:
        st.write(f"**Location:** {result['resolved_address']}")
        st.caption(
            f"Satellite image date: {result['image_date']} • "
            f"Cloud coverage: {result['cloud_pct']}% • "
            f"Location source: {result.get('geocode_source', 'n/a')}"
        )
    with top_col2:
        st.markdown(f"#### Risk: :{risk_color}[{result['overall_label']} ({result['overall_percent']}%)]")

    pdf_buffer = generate_pdf_report(result)
    st.download_button(
        "📄 Download PDF Report",
        data=pdf_buffer,
        file_name=f"RiskEye_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
        mime="application/pdf",
    )

    st.write("")
    c1, c2, c3 = st.columns(3)
    c1.metric("Flood Exposure", result["flood_risk"])
    c2.metric("Terrain Risk", result["terrain_risk"])
    c3.metric("Surroundings", result["surroundings"])

    st.info(f"**Recommendation:** {result['recommendation']}")

    st.markdown("####Recommended Actions")
    st.caption("Based on location risk factors only — building-specific issues (roof, wiring, structure) require a physical or drone inspection.")
    for action in result["actions"]:
        st.markdown(f"- {action}")

    st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
    st.markdown("###  Nearby Infrastructure")
    hazards = result.get("hazards", {})
    h1, h2, h3, h4 = st.columns(4)

    fs, fs_err = hazards.get("filling_stations"), hazards.get("filling_stations_error")
    hs, hs_err = hazards.get("hospitals"), hazards.get("hospitals_error")
    sc, sc_err = hazards.get("schools"), hazards.get("schools_error")
    fr, fr_err = hazards.get("fire_stations"), hazards.get("fire_stations_error")

    h1.metric("Filling stations (1km)", fs if fs is not None else "N/A")
    h2.metric("Hospitals (2km)", hs if hs is not None else "N/A")
    h3.metric("Schools (1km)", sc if sc is not None else "N/A")
    h4.metric("Fire stations (1.5km)", fr if fr is not None else "N/A")

    st.write("")
    st.metric("Area character (inferred)", result.get("area_character", "Unknown"))
    st.caption(
        "Area character is inferred from nearby business density (via Google Places) — it is NOT "
        "computer-vision detection of building types. RiskEye cannot currently look at a building and "
        "determine if it is commercial or residential; this is a contextual estimate only."
    )

    if fs_err or hs_err or sc_err or fr_err:
        st.caption(f"⚠️ Some lookups had issues: {fs_err or ''} {hs_err or ''} {sc_err or ''} {fr_err or ''}".strip())
    st.caption("Counts from Google Places (within radius shown). Coverage is generally strong in major Nigerian cities.")

    st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
    st.markdown("### Weather Conditions")
    weather = result.get("weather")
    hist_weather = result.get("historical_weather")

    w1, w2 = st.columns(2)
    if weather:
        w1.metric("Weather now", weather.get("condition", "N/A"), f"{weather.get('temperature_c', '?')}°C")
    else:
        w1.metric("Weather now", "Unavailable")

    if hist_weather:
        w2.metric(
            "Rainfall pattern (last 12 months)",
            hist_weather["rainfall_label"],
            f"Rained on {hist_weather['rainy_pct']}% of days"
        )
        st.caption(
            f"It rained on {hist_weather['rainy_days']} of the last {hist_weather['period_days']} days, "
            f"totaling {hist_weather['total_rainfall_mm']} mm — {hist_weather['rainfall_label'].lower()}."
        )
    else:
        w2.metric("Rainfall pattern", "Unavailable")

    st.caption("Historical data reflects the last 12 months — a recent pattern, not a multi-year climate record.")

    st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
    st.markdown("### 🔎 Close-Up View")
    if result.get("overall_score", 1) >= 2:
        if "gcp_static_maps" in st.secrets:
            api_key = st.secrets["gcp_static_maps"]["api_key"]

            view_mode = st.radio(
                "View mode",
                ["Interactive (pan/zoom)", "Static image", "Street View (if available)"],
                horizontal=True,
                key="view_mode_radio",
            )

            if view_mode == "Interactive (pan/zoom)":
                st.caption("Interactive Google satellite map — you can pan and zoom directly.")
                render_interactive_google_map(result["latitude"], result["longitude"], api_key)
            elif view_mode == "Static image":
                st.caption("Sharper close-up shown because this property is flagged Medium/High risk.")
                image_bytes = get_static_map_image(result["latitude"], result["longitude"], api_key)
                if image_bytes:
                    st.image(image_bytes, caption="Google satellite close-up (single image, not interactive)")
                else:
                    st.caption("Close-up image could not be retrieved for this location.")
            elif view_mode == "Street View (if available)":
                image_bytes, sv_status = get_street_view_image(result["latitude"], result["longitude"], api_key)
                if image_bytes:
                    st.image(image_bytes, caption="Google Street View (ground-level)")
                else:
                    st.warning(
                        f"No Street View imagery available for this location (status: {sv_status}). "
                        "This is common outside major Nigerian city centers, since Google's Street View "
                        "cars have limited coverage in Nigeria."
                    )
        else:
            st.caption("Close-up imagery is not configured for this deployment.")
    else:
        st.caption("Close-up image is only shown for properties flagged Medium or High risk (this one is Low).")

    if "planet_labs" in st.secrets:
        st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
        st.markdown("### 🪐 Planet Labs Imagery (Trial)")
        st.caption("Daily-refresh satellite imagery from Planet Labs — sharper and fresher than Sentinel-2. Available during trial access only.")

        planet_key = st.secrets["planet_labs"]["api_key"]
        with st.spinner("Searching Planet Labs archive..."):
            planet_result, planet_err = search_planet_imagery(
                result["latitude"], result["longitude"], planet_key
            )

        if planet_result:
            st.write(f"**Image acquired:** {planet_result['acquired']} • **Cloud cover:** {planet_result['cloud_cover']*100:.1f}%")
            if planet_result.get("thumbnail_url"):
                thumb_bytes = get_planet_thumbnail(planet_result["thumbnail_url"], planet_key)
                if thumb_bytes:
                    st.image(thumb_bytes, caption="Planet Labs PlanetScope thumbnail")
                else:
                    st.caption("Thumbnail could not be retrieved (may require full authentication flow).")
        else:
            st.warning(f"Planet Labs imagery unavailable: {planet_err}")

    if result.get("declared_value", 0) > 0:
        st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
        st.markdown("### Sum Insured Check")

        estimated_sqm = result["estimated_built_sqm"]
        estimated_cost = estimated_sqm * result["cost_per_sqm"]

        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("Building type", result["building_type"])
        sc2.metric("Est. built-up area", f"{estimated_sqm:,.0f} sqm")
        sc3.metric("Est. replacement cost", f"₦{estimated_cost:,.0f}")
        sc4.metric("Declared value", f"₦{result['declared_value']:,.0f}")

        if estimated_cost > 0:
            gap = estimated_cost - result["declared_value"]
            gap_pct = max(min((gap / estimated_cost) * 100, 100), -100) if estimated_cost else 0

            if gap_pct > 20:
                st.warning(
                    f"**Possible underinsurance:** declared value is approximately "
                    f"{gap_pct:.0f}% below the estimated replacement cost "
                    f"(≈₦{gap:,.0f} gap). Recommend a proper valuation."
                )
            elif gap_pct < -20:
                st.info("Declared value appears higher than the estimated replacement cost — worth reviewing for over-insurance.")
            else:
                st.success("Declared value appears broadly consistent with the estimated replacement cost.")

        st.caption(
            "This is a rough, indicative estimate: built-up area is measured from satellite imagery "
            "(an aerial footprint, not a ground survey or floor count), combined with a general construction "
            "cost benchmark for the selected building type (structure only — excludes machinery, equipment, "
            "and fittings). Not a certified valuation."
        )

    st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
    st.markdown("### 🛰️ Satellite View (Sentinel-2)")
    st.caption("Zoom using the + / − controls on the map, or scroll while hovering over it. Sentinel-2 imagery has ~10m resolution, so individual buildings will appear blocky rather than sharp.")

    m = folium.Map(location=[result["latitude"], result["longitude"]], zoom_start=17, max_zoom=20)
    map_id_dict = ee.Image(result["best_image"]).getMapId(
        {'bands': ['B4', 'B3', 'B2'], 'min': 0, 'max': 3000}
    )
    folium.TileLayer(
        tiles=map_id_dict['tile_fetcher'].url_format,
        attr='Google Earth Engine', name='Satellite View', overlay=True, max_zoom=20,
    ).add_to(m)
    folium.GeoJson(
        result["area"].getInfo(), name="Property Area",
        style_function=lambda x: {'color': 'red', 'fillOpacity': 0, 'weight': 3}
    ).add_to(m)
    st_folium(m, height=550, width=None)

st.markdown("<div class='section-divider'></div>", unsafe_allow_html=True)
st.markdown("### 📚 Assessment History")
st.caption("⚠️ History is stored temporarily on the app server and may be cleared when the app restarts. This is a lightweight log for demonstration, not permanent storage.")
history = load_history()
if history:
    st.dataframe(history, use_container_width=True)
else:
    st.caption("No assessments logged yet.")

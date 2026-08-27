import re
import csv
import os
from datetime import datetime

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
from io import BytesIO

st.set_page_config(page_title="RiskEye", page_icon="🛰️", layout="wide")

st.markdown("""
    <style>
    .stMetric { background-color: #F2EFE7; padding: 16px; border-radius: 10px; }
    div[data-testid="stMetricValue"] { font-size: 20px; white-space: normal; }
    .block-container { padding-top: 2rem; }
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
        "center": f"{lat},{lon}",
        "zoom": 19,
        "size": "640x400",
        "maptype": "satellite",
        "markers": f"color:red|{lat},{lon}",
        "key": api_key,
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
    <div id="map" style="height:{height}px;width:100%;"></div>
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


def query_overpass_count(lat, lon, radius_m, key, value):
    query = f"""
    [out:json][timeout:15];
    (
      node["{key}"="{value}"](around:{radius_m},{lat},{lon});
      way["{key}"="{value}"](around:{radius_m},{lat},{lon});
    );
    out count;
    """
    try:
        response = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=15
        )
        data = response.json()
        elements = data.get("elements", [])
        if elements and "tags" in elements[0]:
            return int(elements[0]["tags"].get("total", 0))
        return 0
    except Exception:
        return None


def get_nearby_hazards(lat, lon):
    return {
        "filling_stations": query_overpass_count(lat, lon, 1000, "amenity", "fuel"),
        "hospitals": query_overpass_count(lat, lon, 2000, "amenity", "hospital"),
        "schools": query_overpass_count(lat, lon, 1000, "amenity", "school"),
    }


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
        y -= 4 * mm

    weather = result.get("weather")
    if weather:
        section_title("Weather at Time of Assessment")
        line(f"Condition: {weather.get('condition', 'N/A')}")
        line(f"Temperature: {weather.get('temperature_c', 'N/A')} °C")
        line(f"Wind speed: {weather.get('windspeed_kmh', 'N/A')} km/h")
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


def run_assessment(latitude, longitude, resolved_address):
    point = ee.Geometry.Point([longitude, latitude])
    area = point.buffer(500)
    search_area = point.buffer(1000)
    footprint_area = point.buffer(30)

    collection = (
        ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
        .filterBounds(area)
        .filterDate('2026-01-01', '2026-08-26')
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

    hazards = get_nearby_hazards(latitude, longitude)
    weather = get_weather(latitude, longitude)

    return {
        "resolved_address": resolved_address, "image_date": image_date, "cloud_pct": cloud_pct,
        "flood_risk": flood_risk, "terrain_risk": terrain_risk, "surroundings": surroundings,
        "overall_label": overall_label, "overall_percent": overall_percent,
        "recommendation": recommendation, "actions": actions,
        "best_image": best_image, "area": area, "latitude": latitude, "longitude": longitude,
        "estimated_built_sqm": estimated_built_sqm, "overall_score": overall_score,
        "hazards": hazards, "weather": weather,
    }


st.title("🛰️ RiskEye")
st.caption("AI-assisted property risk screening using satellite imagery")
st.divider()

st.subheader("Property Location")
address = st.text_input("Property address", placeholder="e.g. Wuye, Abuja, Nigeria", label_visibility="collapsed")

with st.expander("Enter coordinates manually instead"):
    col1, col2 = st.columns(2)
    with col1:
        manual_lat = st.text_input("Latitude")
    with col2:
        manual_lon = st.text_input("Longitude")

st.write("")
st.subheader("Sum Insured Check (optional)")
st.caption("Select the building type and enter the declared value to check for possible underinsurance. This is an indicative estimate, not a certified valuation.")

col3, col4, col5 = st.columns(3)
with col3:
    declared_value = st.number_input("Declared value (₦)", min_value=0, step=1000000, value=0)
with col4:
    building_type = st.selectbox("Building type", list(BUILDING_COST_BENCHMARKS.keys()))
with col5:
    default_cost = BUILDING_COST_BENCHMARKS[building_type]
    cost_per_sqm = st.number_input("Cost benchmark (₦/sqm)", min_value=0, step=10000, value=default_cost)

run_clicked = st.button("Run Risk Assessment", type="primary", use_container_width=False)
st.write("")

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
        with st.spinner("Analyzing satellite imagery, hazards, and weather..."):
            result = run_assessment(latitude, longitude, resolved_address)

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

    st.divider()
    st.subheader("Assessment Result")

    top_col1, top_col2 = st.columns([2, 1])
    with top_col1:
        st.write(f"**Location:** {result['resolved_address']}")
        st.caption(f"Satellite image date: {result['image_date']} • Cloud coverage: {result['cloud_pct']}% • Location source: {result.get('geocode_source', 'n/a')}")
    with top_col2:
        st.markdown(f"#### Overall Risk: :{risk_color}[{result['overall_label']} ({result['overall_percent']}%)]")

    pdf_buffer = generate_pdf_report(result)
    st.download_button(
        "📄 Download PDF Report",
        data=pdf_buffer,
        file_name=f"RiskEye_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
     

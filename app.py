import re
import ee
import folium
import streamlit as st
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

st.set_page_config(page_title="RiskEye", page_icon="🛰️", layout="wide")

st.markdown("""
    <style>
    .stMetric { background-color: #F2EFE7; padding: 16px; border-radius: 10px; }
    div[data-testid="stMetricValue"] { font-size: 20px; white-space: normal; }
    .block-container { padding-top: 2rem; }
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def init_ee():
    ee.Initialize(project='heirs-risk-ai')

init_ee()


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


def geocode_address(address):
    geolocator = Nominatim(user_agent="riskeye-app")
    location = geolocator.geocode(address)
    if location:
        return location.latitude, location.longitude, location.address

    cleaned = clean_address(address)
    if cleaned != address:
        location = geolocator.geocode(cleaned)
        if location:
            return location.latitude, location.longitude, location.address
    return None, None, None


def score_of(risk_text):
    risk_levels = {"Low": 1, "Medium": 2, "High": 3}
    return risk_levels.get(risk_text.split(" ")[0], 1)


def run_assessment(latitude, longitude, resolved_address):
    point = ee.Geometry.Point([longitude, latitude])
    area = point.buffer(500)
    search_area = point.buffer(1000)

    collection = (
        ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
        .filterBounds(area)
        .filterDate('2026-01-01', '2026-08-19')
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

    overall_score = max(score_of(flood_risk), score_of(terrain_risk))
    overall_label = {1: "LOW", 2: "MEDIUM", 3: "HIGH"}[overall_score]
    recommendation = "Physical inspection advised" if overall_score >= 2 else "Remote screening sufficient"

    return {
        "resolved_address": resolved_address, "image_date": image_date, "cloud_pct": cloud_pct,
        "flood_risk": flood_risk, "terrain_risk": terrain_risk, "surroundings": surroundings,
        "overall_label": overall_label, "recommendation": recommendation,
        "best_image": best_image, "area": area, "latitude": latitude, "longitude": longitude,
    }


# --- UI ---
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

run_clicked = st.button("Run Risk Assessment", type="primary", use_container_width=False)
st.write("")

if run_clicked:
    st.session_state.result = None

    if manual_lat and manual_lon:
        latitude, longitude, resolved_address = float(manual_lat), float(manual_lon), "Manually entered coordinates"
    elif address:
        with st.spinner("Looking up address..."):
            latitude, longitude, resolved_address = geocode_address(address)
    else:
        st.warning("Please enter an address or coordinates.")
        st.stop()

    if latitude is None:
        st.error("Could not find that address. Try a simpler version, or enter coordinates manually above.")
    else:
        with st.spinner("Analyzing satellite imagery and risk factors..."):
            result = run_assessment(latitude, longitude, resolved_address)

        if result is None:
            st.error("No clear satellite image found for this location.")
        else:
            st.session_state.result = result

if "result" in st.session_state and st.session_state.result:
    result = st.session_state.result
    risk_color = {"LOW": "green", "MEDIUM": "orange", "HIGH": "red"}[result["overall_label"]]

    st.divider()
    st.subheader("Assessment Result")

    top_col1, top_col2 = st.columns([2, 1])
    with top_col1:
        st.write(f"**Location:** {result['resolved_address']}")
        st.caption(f"Satellite image date: {result['image_date']} • Cloud coverage: {result['cloud_pct']}%")
    with top_col2:
        st.markdown(f"#### Overall Risk: :{risk_color}[{result['overall_label']}]")

    st.write("")
    c1, c2, c3 = st.columns(3)
    c1.metric("Flood Exposure", result["flood_risk"])
    c2.metric("Terrain Risk", result["terrain_risk"])
    c3.metric("Surroundings", result["surroundings"])

    st.write("")
    st.info(f"**Recommendation:** {result['recommendation']}")

    st.write("")
    st.subheader("Satellite View")
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
import re
import ee
import folium
import requests
import streamlit as st
import streamlit.components.v1 as components
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
    location = geolocator.geocode(address)
    if location:
        return location.latitude, location.longitude, location.address, "free (OpenStreetMap)"

    cleaned = clean_address(address)
    if cleaned != address:
        location = geolocator.geocode(cleaned)
        if location:
            return location.latitude, location.longitude, location.address, "free (OpenStreetMap)"

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
    image_params = {
        "size": "640x400",
        "location": f"{lat},{lon}",
        "key": api_key,
    }
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
          zoom: 18,
          center: location,
          mapTypeId: "satellite",
        }});
        new google.maps.Marker({{ position: location, map: map }});
      }}
      window.onload = initMap;
    </script>
    """
    components.html(html, height=height)


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

    return {
        "resolved_address": resolved_address, "image_date": image_date, "cloud_pct": cloud_pct,
        "flood_risk": flood_risk, "terrain_risk": terrain_risk, "surroundings": surroundings,
        "overall_label": overall_label, "overall_percent": overall_percent,
        "recommendation": recommendation, "actions": actions,
        "best_image": best_image, "area": area, "latitude": latitude, "longitude": longitude,
        "estimated_built_sqm": estimated_built_sqm, "overall_score": overall_score,
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
        with st.spinner("Analyzing satellite imagery and risk factors..."):
            result = run_assessment(latitude, longitude, resolved_address)

        if result is None:
            st.error("No clear satellite image found for this location.")
        else:
            result["declared_value"] = declared_value
            result["cost_per_sqm"] = cost_per_sqm
            result["building_type"] = building_type
            result["geocode_source"] = geocode_source
            st.session_state.result = result

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

    st.write("")
    c1, c2, c3 = st.columns(3)
    c1.metric("Flood Exposure", result["flood_risk"])
    c2.metric("Terrain Risk", result["terrain_risk"])
    c3.metric("Surroundings", result["surroundings"])

    st.write("")
    st.info(f"**Recommendation:** {result['recommendation']}")

    st.write("")
    st.subheader("Recommended Actions")
    st.caption("Based on location risk factors only — building-specific issues (roof, wiring, structure) require a physical or drone inspection.")
    for action in result["actions"]:
        st.markdown(f"- {action}")

    st.write("")
    st.subheader("Close-Up View")
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

    if result.get("declared_value", 0) > 0:
        st.write("")
        st.subheader("Sum Insured Check")

        estimated_sqm = result["estimated_built_sqm"]
        estimated_cost = estimated_sqm * result["cost_per_sqm"]

        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("Building type", result["building_type"])
        sc2.metric("Estimated built-up area", f"{estimated_sqm:,.0f} sqm")
        sc3.metric("Estimated replacement cost", f"₦{estimated_cost:,.0f}")
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

    st.write("")
    st.subheader("Satellite View (Sentinel-2)")
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

const statsGrid = document.getElementById('statsGrid');
const fleetList = document.getElementById('fleetList');
const deliveryList = document.getElementById('deliveryList');
const fleetCount = document.getElementById('fleetCount');
const mapStatus = document.getElementById('mapStatus');

const mapState = {
  map: null,
  markers: [],
  lines: []
};

function formatMetric(value, suffix = '') {
  return `${value}${suffix}`;
}

async function fetchFleet() {
  const [fleetResponse, configResponse] = await Promise.all([
    fetch('/api/fleet', { cache: 'no-store' }),
    fetch('/api/config', { cache: 'no-store' })
  ]);
  const fleet = await fleetResponse.json();
  const config = await configResponse.json();
  renderStats(fleet.analytics);
  renderFleet(fleet.drones);
  renderDeliveries(fleet.drones);
  initMap(config, fleet.drones);
}

function renderStats(analytics) {
  const cards = [
    { label: 'Active drones', value: analytics.active_drones },
    { label: 'Active deliveries', value: analytics.active_deliveries },
    { label: 'Completed', value: analytics.completed_deliveries },
    { label: 'Observed distance', value: `${analytics.total_observed_distance_km.toFixed(1)} km` },
  ];
  statsGrid.innerHTML = cards.map(item => `
    <div class="stat-card">
      <span>${item.label}</span>
      <strong>${item.value}</strong>
    </div>
  `).join('');
}

function renderFleet(drones) {
  fleetCount.textContent = String(drones.length);
  fleetList.innerHTML = drones.map(drone => `
    <article class="fleet-item">
      <div class="drone-color" style="background:${drone.color};"></div>
      <div class="fleet-text">
        <div class="fleet-row">
          <strong>${drone.drone_id}</strong>
          <span class="status-tag ${drone.status}">${drone.status}</span>
        </div>
        <small>${drone.source} · ${drone.mission}</small>
        <div class="meta-row">
          <span>${drone.position.latitude.toFixed(4)}, ${drone.position.longitude.toFixed(4)}</span>
          <span>${drone.actual_distance_km.toFixed(1)} km</span>
        </div>
      </div>
    </article>
  `).join('');
}

function renderDeliveries(drones) {
  const entries = drones.map(drone => ({
    id: drone.mission,
    heading: `${drone.drone_id} → ${drone.destination}`,
    status: drone.delivery_status,
    distance: `${drone.actual_distance_km.toFixed(1)} km travelled`
  }));

  deliveryList.innerHTML = entries.map(item => `
    <div class="delivery-item">
      <div>
        <strong>${item.heading}</strong>
        <small>${item.id}</small>
      </div>
      <div class="delivery-meta">
        <span>${item.status}</span>
        <small>${item.distance}</small>
      </div>
    </div>
  `).join('');
}

function initMap(config, drones) {
  if (window.google && window.google.maps) {
    if (!mapState.map) {
      mapState.map = new google.maps.Map(document.getElementById('map'), {
        center: { lat: 43.6481, lng: -79.3870 },
        zoom: 12,
        mapTypeControl: true,
        streetViewControl: false,
      });
    }
    if (config.google_maps_api_key) {
      mapStatus.textContent = 'Maps API ready';
      mapStatus.className = 'pill success';
    } else {
      mapStatus.textContent = 'Demo mode';
      mapStatus.className = 'pill warning';
    }

    mapState.markers.forEach(marker => marker.setMap(null));
    mapState.lines.forEach(line => line.setMap(null));
    mapState.markers = [];
    mapState.lines = [];

    drones.forEach(drone => {
      const marker = new google.maps.Marker({
        position: { lat: drone.position.latitude, lng: drone.position.longitude },
        map: mapState.map,
        title: drone.drone_id,
        icon: {
          path: google.maps.SymbolPath.CIRCLE,
          scale: 10,
          fillColor: drone.color,
          fillOpacity: 1,
          strokeColor: '#ffffff',
          strokeWeight: 2,
        }
      });
      mapState.markers.push(marker);
    });

    if (drones.length > 1) {
      const routePath = new google.maps.Polyline({
        path: drones.map(drone => ({ lat: drone.position.latitude, lng: drone.position.longitude })),
        geodesic: true,
        strokeColor: '#60a5fa',
        strokeOpacity: 0.8,
        strokeWeight: 3,
        map: mapState.map,
      });
      mapState.lines.push(routePath);
    }
    return;
  }

  mapStatus.textContent = 'Offline map fallback';
  mapStatus.className = 'pill warning';
  document.getElementById('map').innerHTML = '<div class="fallback-map"><strong>Map unavailable</strong><p>Google Maps is not configured or API loading failed. The fleet telemetry remains active in demo mode.</p></div>';
}

function loadGoogleMaps(apiKey) {
  if (!apiKey || window.google && window.google.maps) {
    fetchFleet();
    return;
  }

  const script = document.createElement('script');
  script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(apiKey)}&callback=loadGoogleMapsStub`;
  script.async = true;
  script.defer = true;
  document.head.appendChild(script);
  window.loadGoogleMapsStub = function () {
    fetchFleet();
  };
}

async function bootDashboard() {
  try {
    const response = await fetch('/api/config', { cache: 'no-store' });
    const config = await response.json();
    if (config.google_maps_api_key) {
      loadGoogleMaps(config.google_maps_api_key);
    } else {
      fetchFleet();
    }
  } catch (error) {
    fetchFleet();
  }
}

window.addEventListener('load', bootDashboard);
document.getElementById('refreshButton').addEventListener('click', bootDashboard);
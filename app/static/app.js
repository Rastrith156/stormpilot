const statsGrid = document.getElementById('statsGrid');
const fleetList = document.getElementById('fleetList');
const deliveryList = document.getElementById('deliveryList');
const fleetCount = document.getElementById('fleetCount');
const mapStatus = document.getElementById('mapStatus');
const mapMessage = document.getElementById('mapMessage');
const fleetMessage = document.getElementById('fleetMessage');
const mapElement = document.getElementById('map');
const selectedDroneSelect = document.getElementById('selectedDrone');
const trafficSummary = document.getElementById('trafficSummary');
const trafficOrigin = document.getElementById('trafficOrigin');
const twinSummary = document.getElementById('twinSummary');

const POLL_INTERVAL_MS = 5000;
const mapState = {
  map: null,
  mapReady: false,
  mapError: false,
  fleet: null,
  traffic: null,
  twin: null,
  selectedDroneId: null,
  markers: new Map(),
  observedLines: new Map(),
  plannedLines: new Map(),
  deliveryMarkers: new Map(),
  twinOverlays: [],
  twinSignature: null,
  heatCells: new Map(),
  pollTimer: null,
  pollInFlight: false
};

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function formatNumber(value, digits = 1, suffix = '') {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(digits)}${suffix}` : '—';
}

function formatTime(value) {
  if (!value) return 'No GPS update';
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? 'Invalid timestamp' : time.toLocaleString();
}

function stateClass(state) {
  return ['PHONE_GPS', 'STALE', 'OFFLINE', 'SIMULATED'].includes(state) ? state.toLowerCase() : 'offline';
}

function setMapState(kind, title, detail) {
  mapStatus.textContent = title;
  mapStatus.className = `pill ${kind}`;
  mapMessage.className = `status-block ${kind === 'error' ? 'error' : kind === 'success' ? 'success' : ''}`;
  mapMessage.textContent = detail;
}

async function getJSON(url) {
  const response = await fetch(url, { cache: 'no-store' });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error('The server returned an invalid response.');
  }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
  return payload;
}

function validateFleet(payload) {
  if (!payload || !Array.isArray(payload.drones) || !payload.analytics || !Array.isArray(payload.deliveries)) {
    throw new Error('Fleet response is missing required data.');
  }
  for (const drone of payload.drones) {
    if (!drone || typeof drone.drone_id !== 'string' || !['OFFLINE', 'STALE', 'PHONE_GPS', 'SIMULATED'].includes(drone.position_state)) {
      throw new Error('Fleet response contains an invalid drone record.');
    }
  }
  return payload;
}

function renderStats(analytics) {
  const cards = [
    { label: 'GPS-connected drones', value: analytics.active_drones ?? 0, origin: analytics.gps_origin },
    { label: 'Active deliveries', value: analytics.active_deliveries ?? 0, origin: analytics.delivery_origin },
    { label: 'Completed deliveries', value: analytics.completed_deliveries ?? 0, origin: 'historical' },
    { label: 'Observed GPS distance', value: `${formatNumber(analytics.total_observed_distance_km)} km`, origin: analytics.distance_origin },
    { label: 'Average completed duration', value: analytics.average_delivery_duration_minutes == null ? '—' : `${formatNumber(analytics.average_delivery_duration_minutes)} min`, origin: 'historical' },
    { label: 'Delivery success rate', value: analytics.delivery_success_rate == null ? '—' : `${formatNumber(analytics.delivery_success_rate * 100, 1)}%`, origin: 'historical' }
  ];
  statsGrid.innerHTML = cards.map(item => `
    <div class="stat-card">
      <span>${escapeHTML(item.label)}</span>
      <strong>${escapeHTML(item.value)}</strong>
      <small>Origin: ${escapeHTML(item.origin || 'historical')}</small>
    </div>
  `).join('');
}

function renderFleet(drones) {
  fleetCount.textContent = String(drones.length);
  const selected = drones.some(drone => drone.drone_id === mapState.selectedDroneId)
    ? mapState.selectedDroneId
    : drones[0]?.drone_id || null;
  mapState.selectedDroneId = selected;

  const selectedOptions = drones.map(drone => `
    <option value="${escapeHTML(drone.drone_id)}" ${drone.drone_id === selected ? 'selected' : ''}>${escapeHTML(drone.drone_id)}</option>
  `).join('');
  selectedDroneSelect.innerHTML = selectedOptions || '<option value="">No drones registered</option>';

  const trafficSelection = document.getElementById('trafficDrone').value;
  document.getElementById('trafficDrone').innerHTML = `<option value="">All drones</option>${drones.map(drone => `
    <option value="${escapeHTML(drone.drone_id)}" ${drone.drone_id === trafficSelection ? 'selected' : ''}>${escapeHTML(drone.drone_id)}</option>
  `).join('')}`;

  fleetList.innerHTML = drones.map(drone => `
    <button class="fleet-item ${drone.drone_id === selected ? 'selected' : ''}" type="button" data-drone-id="${escapeHTML(drone.drone_id)}">
      <span class="drone-color" style="background:${escapeHTML(drone.color)}"></span>
      <span class="fleet-text">
        <span class="fleet-row">
          <strong>${escapeHTML(drone.drone_id)}</strong>
          <span class="status-tag ${stateClass(drone.position_state)}">${escapeHTML(drone.position_state)}</span>
        </span>
        <small>${escapeHTML(drone.source)} · ${drone.mission ? `Mission ${escapeHTML(drone.mission)}` : 'No assigned delivery'}</small>
        <span class="meta-row">
          <span>${drone.position ? `${formatNumber(drone.position.latitude, 5)}, ${formatNumber(drone.position.longitude, 5)}` : 'Position unavailable'}</span>
          <span>${formatNumber(drone.actual_distance_km)} km observed</span>
        </span>
      </span>
    </button>
  `).join('');
  fleetList.querySelectorAll('[data-drone-id]').forEach(button => {
    button.addEventListener('click', () => selectDrone(button.dataset.droneId));
  });
  renderSelectedDrone();
}

function renderSelectedDrone() {
  if (!mapState.fleet) return;
  const drone = mapState.fleet.drones.find(item => item.drone_id === mapState.selectedDroneId);
  const target = document.getElementById('droneDetails');
  if (!drone) {
    target.textContent = 'No drone is selected.';
    return;
  }
  const position = drone.position
    ? `${formatNumber(drone.position.latitude, 6)}, ${formatNumber(drone.position.longitude, 6)}`
    : 'Unavailable';
  const mission = drone.mission ? escapeHTML(drone.mission) : 'No active mission';
  const rows = [
    ['Drone ID', drone.drone_id],
    ['Position source / state', `${drone.source} / ${drone.position_state}`],
    ['Latitude, longitude', position],
    ['Reported GPS accuracy', drone.accuracy_m == null ? '—' : `${formatNumber(drone.accuracy_m, 1)} m`],
    ['Last GPS update', formatTime(drone.last_update)],
    ['Actual observed distance', `${formatNumber(drone.actual_distance_km, 3)} km`],
    ['Current delivery observed distance', drone.delivery_actual_distance_km == null ? 'Unavailable' : `${formatNumber(drone.delivery_actual_distance_km, 3)} km`],
    ['Mission / delivery ID', mission],
    ['Delivery status', drone.delivery_status || 'No delivery'],
    ['Planned route distance', drone.planned_route_km == null ? 'Unavailable' : `${formatNumber(drone.planned_route_km, 3)} km`],
    ['Remaining planned distance', drone.remaining_distance_km == null ? 'Unavailable' : `${formatNumber(drone.remaining_distance_km, 3)} km (plan minus observed)`],
    ['Notification status', drone.notification_status || 'Unavailable']
  ];
  target.innerHTML = rows.map(([label, value]) => `
    <div class="detail-cell"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong></div>
  `).join('') + (drone.position_state === 'STALE'
    ? '<p class="warning-copy">Last GPS position is stale and is not treated as current.</p>'
    : '') + (!drone.gps_coverage_sufficient
      ? '<p class="warning-copy">GPS coverage is insufficient for a reliable distance estimate (low accuracy or fewer than two fixes).</p>'
      : '');
}

function renderDeliveries(deliveries) {
  const entries = deliveries.slice(0, 100);
  if (!entries.length) {
    deliveryList.innerHTML = '<p class="muted-copy">No deliveries are recorded in the backend yet.</p>';
    return;
  }
  deliveryList.innerHTML = entries.map(item => {
    const duration = item.delivered_at && item.created_at
      ? (new Date(item.delivered_at) - new Date(item.created_at)) / 60000
      : null;
    return `
      <article class="delivery-item">
        <div>
          <strong>${escapeHTML(item.drone_id)} → ${escapeHTML(item.delivery_id)}</strong>
          <small>${escapeHTML(item.status)} · ${escapeHTML(item.origin)} · updated ${escapeHTML(formatTime(item.updated_at))}</small>
        </div>
        <div class="delivery-meta">
          <span>${formatNumber(item.actual_distance_km, 3)} km observed</span>
          <small>${duration == null ? 'In progress' : `${formatNumber(duration)} min`} · notify ${escapeHTML(item.notification_status || 'unknown')}</small>
        </div>
      </article>
    `;
  }).join('');
}

function renderTraffic(data) {
  if (!data) return;
  trafficOrigin.textContent = `${data.origin} observations`;
  trafficOrigin.className = `pill ${data.origin === 'live' ? 'success' : 'neutral'}`;
  const distances = data.distance_per_drone.map(item =>
    `<li>${escapeHTML(item.drone_id)}: ${formatNumber(item.distance_km, 3)} km observed (${escapeHTML(item.origin)})</li>`
  ).join('');
  const intersections = data.route_intersections.map(item =>
    `<li>${item.kind === 'path_crossing' ? 'Observed path crossing' : `${escapeHTML(item.drone_count)} drones share a grid cell`} near ${formatNumber(item.latitude, 3)}, ${formatNumber(item.longitude, 3)}</li>`
  ).join('') || '<li>No overlapping GPS cells in this window.</li>';
  const busiestCells = data.cells.slice(0, 8).map(cell =>
    `<li>${escapeHTML(cell.sample_count)} fixes · ${escapeHTML(cell.drone_count)} drones near ${formatNumber(cell.latitude, 3)}, ${formatNumber(cell.longitude, 3)} (${escapeHTML(cell.origin)})</li>`
  ).join('') || '<li>No accepted fixes in this window.</li>';
  trafficSummary.innerHTML = `
    <p>${escapeHTML(data.sample_count)} accepted GPS samples; data origin: <strong>${escapeHTML(data.origin)}</strong>; source: ${escapeHTML(data.source)}.</p>
    <p>Geographic cells (${escapeHTML(data.cell_size_deg)}°): ${escapeHTML(data.cells.length)} · Overlapping paths: ${escapeHTML(data.route_intersections.length)} · Active deliveries: ${escapeHTML(data.delivery_counts.active)} · Completed: ${escapeHTML(data.delivery_counts.completed)}</p>
    <h3>Busiest observed GPS cells</h3><ul>${busiestCells}</ul>
    <h3>Observed distance by drone</h3><ul>${distances || '<li>No GPS distance available.</li>'}</ul>
    <h3>Route intersections / shared cells</h3><ul>${intersections}</ul>
    ${data.sample_count >= data.sample_limit ? '<p class="warning-copy">Sample limit reached; narrow the time window or filter the data.</p>' : ''}
  `;
}

function renderTwin(data) {
  if (!data) return;
  const routes = data.scenarios.flatMap(scenario => scenario.routes);
  twinSummary.innerHTML = `
    <p>Dataset origin: <strong>${escapeHTML(data.origin)}</strong> · Virtual corridors: ${escapeHTML(data.corridors.length)} · Demonstration zones: ${escapeHTML(data.zones.length)} · Simulated intersections: ${escapeHTML(data.route_intersections.length)}</p>
    <p>${data.corridors.map(item => escapeHTML(item.name)).join(' · ') || 'No virtual corridors configured'}</p>
    <p>${routes.map(item => escapeHTML(item.drone_id)).join(' · ') || 'No scenario routes configured'}</p>
    <p>${data.zones.map(item => `${escapeHTML(item.name)} (${formatNumber(item.radius_m, 0)} m virtual zone)`).join(' · ') || 'No virtual zones configured'}</p>
    <p>Virtual restrictions enabled: ${data.restrictions.filter(item => item.restricted).length}</p>
  `;
}

function selectDrone(droneId) {
  mapState.selectedDroneId = droneId;
  selectedDroneSelect.value = droneId;
  renderFleet(mapState.fleet?.drones || []);
  renderMap();
}

function googlePosition(point) {
  return { lat: Number(point.latitude ?? point.lat), lng: Number(point.longitude ?? point.lng) };
}

function ensureMap() {
  if (!mapState.mapReady || mapState.map || mapState.mapError) return;
  mapState.map = new google.maps.Map(mapElement, {
    center: { lat: 0, lng: 0 },
    zoom: 2,
    mapTypeControl: true,
    streetViewControl: false,
    fullscreenControl: true
  });
  setMapState('success', 'Google Maps ready', 'Live GPS and backend data continue to refresh independently of the map.');
}

function upsertMarker(mapOfMarkers, key, options, onClick) {
  let marker = mapOfMarkers.get(key);
  if (!marker) {
    marker = Array.isArray(options.path)
      ? new google.maps.Polyline(options)
      : new google.maps.Marker(options);
    if (onClick) marker.addListener('click', onClick);
    mapOfMarkers.set(key, marker);
  } else {
    marker.setOptions(options);
  }
  return marker;
}

function removeUnused(mapOfMarkers, keepKeys) {
  for (const [key, overlay] of mapOfMarkers) {
    if (!keepKeys.has(key)) {
      overlay.setMap(null);
      mapOfMarkers.delete(key);
    }
  }
}

function renderMap() {
  ensureMap();
  if (!mapState.map) return;
  const visibleMarkerIds = new Set();
  const observedIds = new Set();
  const plannedIds = new Set();
  const deliveryMarkerIds = new Set();

  for (const drone of mapState.fleet?.drones || []) {
    if (drone.position && Number.isFinite(Number(drone.position.latitude)) && Number.isFinite(Number(drone.position.longitude))) {
      visibleMarkerIds.add(drone.drone_id);
      const marker = upsertMarker(mapState.markers, drone.drone_id, {
        map: mapState.map,
        position: googlePosition(drone.position),
        title: `${drone.drone_id} · ${drone.position_state}`,
        icon: {
          path: google.maps.SymbolPath.CIRCLE,
          scale: drone.drone_id === mapState.selectedDroneId ? 11 : 8,
          fillColor: drone.color,
          fillOpacity: drone.position_state === 'STALE' ? 0.55 : 1,
          strokeColor: '#ffffff',
          strokeWeight: 2
        },
        zIndex: drone.drone_id === mapState.selectedDroneId ? 10 : 1
      }, () => selectDrone(drone.drone_id));
      marker.setMap(mapState.map);
    } else if (mapState.markers.has(drone.drone_id)) {
      mapState.markers.get(drone.drone_id).setMap(null);
    }

    if (document.getElementById('showObservedRoutes').checked && drone.observed_path?.length > 1) {
      observedIds.add(drone.drone_id);
      const line = upsertMarker(mapState.observedLines, drone.drone_id, {
        map: mapState.map,
        path: drone.observed_path.map(googlePosition),
        geodesic: true,
        strokeColor: drone.color,
        strokeOpacity: drone.position_state === 'STALE' ? 0.35 : 0.85,
        strokeWeight: drone.drone_id === mapState.selectedDroneId ? 5 : 3
      });
      line.setMap(mapState.map);
    }

    if (document.getElementById('showPlannedRoutes').checked && drone.pickup && drone.destination) {
      plannedIds.add(drone.drone_id);
      const line = upsertMarker(mapState.plannedLines, drone.drone_id, {
        map: mapState.map,
        path: [googlePosition(drone.pickup), googlePosition(drone.destination)],
        geodesic: true,
        strokeColor: drone.color,
        strokeOpacity: 0.5,
        strokeWeight: 2,
        icons: [{ icon: { path: 'M 0,-1 0,1', strokeOpacity: 1, scale: 3 }, offset: '0', repeat: '12px' }]
      });
      line.setMap(mapState.map);
      for (const [kind, point, label] of [['pickup', drone.pickup, 'P'], ['delivery', drone.destination, 'D']]) {
        const key = `${drone.drone_id}:${kind}`;
        deliveryMarkerIds.add(key);
        const endpoint = upsertMarker(mapState.deliveryMarkers, key, {
          map: mapState.map,
          position: googlePosition(point),
          title: `${kind === 'pickup' ? 'Pickup' : 'Delivery'} · ${drone.delivery_id}`,
          label: { text: label, color: '#ffffff', fontWeight: 'bold' }
        });
        endpoint.setMap(mapState.map);
      }
    }
  }

  removeUnused(mapState.observedLines, observedIds);
  removeUnused(mapState.plannedLines, plannedIds);
  removeUnused(mapState.deliveryMarkers, deliveryMarkerIds);
  updateHeatmap();
  updateTwinOverlays();
}

function updateHeatmap() {
  if (!mapState.map) return;
  const enabled = document.getElementById('showTraffic').checked;
  const cells = mapState.traffic?.cells || [];
  const maxSamples = Math.max(1, ...cells.map(cell => cell.sample_count));
  const keepKeys = new Set();
  const halfCell = (mapState.traffic?.cell_size_deg || 0.01) / 2;
  for (const cell of cells) {
    const key = `${cell.latitude}:${cell.longitude}`;
    keepKeys.add(key);
    const intensity = Math.sqrt(cell.sample_count / maxSamples);
    const fillColor = intensity >= 0.75 ? '#ef4444' : intensity >= 0.4 ? '#f59e0b' : '#38bdf8';
    const options = {
      bounds: {
        north: Math.min(90, cell.latitude + halfCell),
        south: Math.max(-90, cell.latitude - halfCell),
        east: Math.min(180, cell.longitude + halfCell),
        west: Math.max(-180, cell.longitude - halfCell)
      },
      fillColor,
      fillOpacity: 0.18 + 0.4 * intensity,
      strokeColor: fillColor,
      strokeOpacity: 0.35,
      strokeWeight: 1,
      map: enabled ? mapState.map : null
    };
    let rectangle = mapState.heatCells.get(key);
    if (!rectangle) {
      rectangle = new google.maps.Rectangle(options);
      mapState.heatCells.set(key, rectangle);
    } else {
      rectangle.setOptions(options);
    }
  }
  removeUnused(mapState.heatCells, keepKeys);
}

function updateTwinOverlays() {
  if (!mapState.map) return;
  const signature = mapState.twin ? JSON.stringify(mapState.twin) : '';
  if (signature !== mapState.twinSignature) {
    for (const overlay of mapState.twinOverlays) overlay.setMap(null);
    mapState.twinOverlays = [];
    mapState.twinSignature = signature;
    if (mapState.twin) {
      for (const corridor of mapState.twin.corridors) {
        mapState.twinOverlays.push(new google.maps.Polyline({
          path: corridor.path.map(googlePosition),
          strokeColor: '#fbbf24',
          strokeOpacity: 0.75,
          strokeWeight: 5
        }));
      }
      for (const zone of mapState.twin.zones) {
        const restriction = mapState.twin.restrictions.find(item => item.zone_id === zone.id);
        const color = restriction?.restricted ? '#f87171' : '#62d0ff';
        mapState.twinOverlays.push(new google.maps.Circle({
          center: googlePosition(zone.center),
          radius: zone.radius_m,
          fillColor: color,
          fillOpacity: 0.12,
          strokeColor: color,
          strokeOpacity: 0.7
        }));
      }
      for (const scenario of mapState.twin.scenarios) {
        for (const route of scenario.routes) {
          mapState.twinOverlays.push(new google.maps.Polyline({
            path: route.path.map(googlePosition),
            strokeColor: '#fb923c',
            strokeOpacity: 0.7,
            strokeWeight: 3,
            icons: [{ icon: { path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW }, offset: '50%' }]
          }));
        }
      }
      for (const intersection of mapState.twin.route_intersections) {
        mapState.twinOverlays.push(new google.maps.Marker({
          position: googlePosition(intersection),
          title: `Simulated intersection · ${intersection.drone_ids.join(' / ')}`,
          icon: {
            path: google.maps.SymbolPath.CIRCLE,
            scale: 6,
            fillColor: '#fb923c',
            fillOpacity: 1,
            strokeColor: '#ffffff',
            strokeWeight: 1
          }
        }));
      }
    }
  }
  const enabled = document.getElementById('showTwin').checked;
  for (const overlay of mapState.twinOverlays) overlay.setMap(enabled ? mapState.map : null);
}

function fitVisibleMap() {
  if (!mapState.map) return;
  const bounds = new google.maps.LatLngBounds();
  let points = 0;
  const add = point => {
    if (point) {
      bounds.extend(googlePosition(point));
      points += 1;
    }
  };
  for (const drone of mapState.fleet?.drones || []) {
    add(drone.position);
    if (document.getElementById('showObservedRoutes').checked) drone.observed_path?.forEach(add);
    if (document.getElementById('showPlannedRoutes').checked) {
      add(drone.pickup);
      add(drone.destination);
    }
  }
  if (document.getElementById('showTraffic').checked) {
    mapState.traffic?.cells.forEach(cell => add({ lat: cell.latitude, lng: cell.longitude }));
  }
  if (document.getElementById('showTwin').checked && mapState.twin) {
    mapState.twin.corridors.forEach(item => item.path.forEach(add));
    mapState.twin.zones.forEach(item => add(item.center));
    mapState.twin.scenarios.forEach(scenario => scenario.routes.forEach(route => route.path.forEach(add)));
  }
  if (points) {
    mapState.map.fitBounds(bounds);
    if (points === 1) mapState.map.setZoom(15);
  }
}

async function pollDashboard() {
  if (mapState.pollInFlight) return;
  mapState.pollInFlight = true;
  try {
    const trafficParams = new URLSearchParams({
      window_hours: document.getElementById('trafficWindow').value,
      source: document.getElementById('trafficSource').value,
      cell_size_deg: document.getElementById('trafficCellSize').value
    });
    const droneFilter = document.getElementById('trafficDrone').value;
    const missionFilter = document.getElementById('trafficMission').value.trim();
    if (droneFilter) trafficParams.set('drone_id', droneFilter);
    if (missionFilter) trafficParams.set('mission', missionFilter);

    const [fleetPayload, trafficPayload] = await Promise.all([
      getJSON('/api/fleet'),
      getJSON(`/api/traffic?${trafficParams.toString()}`)
    ]);
    mapState.fleet = validateFleet(fleetPayload);
    if (!trafficPayload || !Array.isArray(trafficPayload.cells) || !Array.isArray(trafficPayload.distance_per_drone)) {
      throw new Error('Traffic response is missing required data.');
    }
    mapState.traffic = trafficPayload;
    renderStats(mapState.fleet.analytics);
    renderFleet(mapState.fleet.drones);
    renderDeliveries(mapState.fleet.deliveries);
    renderTraffic(mapState.traffic);
    renderMap();
    fleetMessage.className = 'status-block success';
    fleetMessage.textContent = `Backend fleet and traffic updated ${new Date().toLocaleTimeString()}. Stale and offline positions are not treated as current.`;
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Fleet data could not be loaded.';
    fleetMessage.className = 'status-block error';
    fleetMessage.textContent = `Fleet API unavailable: ${message} The last successful view is retained.`;
    if (!mapState.fleet) {
      fleetList.innerHTML = '<p class="muted-copy">Waiting for a valid fleet API response.</p>';
      deliveryList.innerHTML = '<p class="muted-copy">Delivery data is unavailable.</p>';
    }
  } finally {
    mapState.pollInFlight = false;
    clearTimeout(mapState.pollTimer);
    mapState.pollTimer = setTimeout(pollDashboard, POLL_INTERVAL_MS);
  }
}

function refreshNow() {
  clearTimeout(mapState.pollTimer);
  pollDashboard();
}

async function bootDashboard() {
  try {
    const config = await getJSON('/api/config');
    if (!config || typeof config.map_ready !== 'boolean') throw new Error('Map configuration response is invalid.');
    if (!config.map_ready || !config.google_maps_api_key) {
      setMapState('warning', 'Map key not configured', 'Fleet APIs remain available. Configure a browser-restricted Google Maps key to enable the interactive map.');
      mapElement.innerHTML = '<div class="fallback-map"><strong>Google Maps is not configured</strong><p>Telemetry and backend fleet updates continue without a map.</p></div>';
    } else {
      loadGoogleMaps(config.google_maps_api_key);
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'Configuration request failed.';
    setMapState('error', 'Configuration unavailable', `Could not load /api/config: ${detail} Fleet data will still be requested.`);
    mapElement.innerHTML = '<div class="fallback-map"><strong>Map configuration unavailable</strong><p>Fleet telemetry continues independently.</p></div>';
  }
  pollDashboard();
  try {
    const twin = await getJSON('/api/twin');
    if (!twin || !Array.isArray(twin.corridors) || !Array.isArray(twin.zones) || !Array.isArray(twin.scenarios)) {
      throw new Error('Digital twin response is invalid.');
    }
    mapState.twin = twin;
    renderTwin(twin);
    renderMap();
  } catch (error) {
    twinSummary.textContent = `Virtual scenario data unavailable: ${error instanceof Error ? error.message : 'request failed'}`;
  }
}

function loadGoogleMaps(apiKey) {
  if (mapState.mapReady || document.getElementById('googleMapsScript')) return;
  setMapState('warning', 'Loading Google Maps', 'Loading the browser Maps JavaScript API. Fleet API polling is independent.');
  window.stormpilotMapsLoaded = () => {
    mapState.mapReady = true;
    ensureMap();
    renderMap();
  };
  window.gm_authFailure = () => {
    mapState.mapError = true;
    setMapState('error', 'Google Maps key rejected', 'Check key billing, HTTP referrer restrictions, and the Maps JavaScript API restriction.');
    mapElement.innerHTML = '<div class="fallback-map"><strong>Google Maps rejected the key</strong><p>Backend telemetry remains available.</p></div>';
  };
  const script = document.createElement('script');
  script.id = 'googleMapsScript';
  script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(apiKey)}&callback=stormpilotMapsLoaded&loading=async&v=weekly`;
  script.async = true;
  script.defer = true;
  script.onerror = () => {
    mapState.mapError = true;
    setMapState('error', 'Google Maps network/API error', 'The Maps script failed to load. Check network access, billing, API restrictions and the browser console.');
  };
  document.head.appendChild(script);
}

document.getElementById('refreshButton').addEventListener('click', refreshNow);
selectedDroneSelect.addEventListener('change', () => selectDrone(selectedDroneSelect.value));
for (const id of ['showObservedRoutes', 'showPlannedRoutes', 'showTraffic', 'showTwin']) {
  document.getElementById(id).addEventListener('change', () => {
    renderMap();
    if (id === 'showTraffic' && document.getElementById(id).checked && !mapState.traffic) refreshNow();
  });
}
for (const id of ['trafficWindow', 'trafficDrone', 'trafficSource', 'trafficCellSize']) {
  document.getElementById(id).addEventListener('change', refreshNow);
}
document.getElementById('trafficMission').addEventListener('change', refreshNow);
document.getElementById('zoomIn').addEventListener('click', () => {
  if (mapState.map) mapState.map.setZoom(mapState.map.getZoom() + 1);
});
document.getElementById('zoomOut').addEventListener('click', () => {
  if (mapState.map) mapState.map.setZoom(mapState.map.getZoom() - 1);
});
document.getElementById('fitMap').addEventListener('click', fitVisibleMap);
window.addEventListener('load', bootDashboard, { once: true });

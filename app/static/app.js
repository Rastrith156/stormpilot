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
  bengaluruDemo: null,
  demoMarkers: new Map(),
  demoRoutes: new Map(),
  demoDeliveryMarkers: new Map(),
  demoHeatCells: new Map(),
  fallbackBounds: {south: 12.9, west: 77.55, north: 13.04, east: 77.66},
  fallbackMapSignature: null,
  replayPath: null,
  replayMarker: null,
  replayPlannedPath: null,
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
  populateWorkbenchOptions();
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
    center: { lat: 12.9716, lng: 77.5946 },
    zoom: 12,
    mapTypeControl: true,
    streetViewControl: false,
    fullscreenControl: true
  });
  setMapState('success', 'Google Maps ready', 'Live GPS and backend data continue to refresh independently of the map.');
}

const BENGALURU_MAP_TILE_URL = 'https://www.openstreetmap.org/export/embed.html';

function mapPoint(point) {
  return {latitude: Number(point.latitude), longitude: Number(point.longitude)};
}

function fallbackMapPercent(point) {
  const bounds = mapState.fallbackBounds;
  const latitude = Number(point.latitude);
  const longitude = Number(point.longitude);
  return {
    x: (longitude - bounds.west) / (bounds.east - bounds.west) * 1000,
    y: (bounds.north - latitude) / (bounds.north - bounds.south) * 600
  };
}

function renderBengaluruDemoList() {
  const target = document.getElementById('bengaluruDemoList');
  const enabled = document.getElementById('showBengaluruDemo').checked;
  const deliveries = mapState.bengaluruDemo?.deliveries || [];
  target.hidden = !enabled || deliveries.length === 0;
  if (!target.hidden) {
    target.innerHTML = `
      <div class="bengaluru-demo-heading"><strong>SIMULATED · Bengaluru sample deliveries</strong><span>${deliveries.length} demos · no real drone GPS</span></div>
      <div class="bengaluru-demo-items">${deliveries.map(item => `
        <article class="bengaluru-demo-item">
          <span class="demo-drone-dot" style="--demo-color:${escapeHTML(item.color)}"></span>
          <div><strong>${escapeHTML(item.drone_id)} · ${escapeHTML(item.area)}</strong><span>${escapeHTML(item.delivery_id)} · ${escapeHTML(item.status.replaceAll('_', ' '))}</span></div>
          <small>${formatNumber(item.position.latitude, 4)}, ${formatNumber(item.position.longitude, 4)}</small>
        </article>
      `).join('')}</div>
    `;
  }
}

function renderFallbackBengaluruMap() {
  const fallback = mapElement.querySelector('.fallback-map');
  if (!fallback || !mapState.bengaluruDemo) return;
  const signature = JSON.stringify({
    enabled: document.getElementById('showBengaluruDemo').checked,
    bounds: mapState.fallbackBounds
  });
  if (
    mapState.fallbackMapSignature === signature
    && (fallback.querySelector('iframe') || !document.getElementById('showBengaluruDemo').checked)
  ) return;
  if (!document.getElementById('showBengaluruDemo').checked) {
    fallback.innerHTML = '<strong>Google Maps is not configured</strong><p>Enable the Bengaluru demo layer to view its simulated sample map.</p>';
    mapState.fallbackMapSignature = signature;
    return;
  }
  const deliveries = mapState.bengaluruDemo.deliveries;
  const heatCells = mapState.bengaluruDemo.heat_cells;
  const maxSamples = Math.max(1, ...heatCells.map(cell => cell.sample_count));
  const northWest = `${mapState.fallbackBounds.west},${mapState.fallbackBounds.north}`;
  const southEast = `${mapState.fallbackBounds.east},${mapState.fallbackBounds.south}`;
  const params = new URLSearchParams({
    bbox: `${mapState.fallbackBounds.west},${mapState.fallbackBounds.south},${mapState.fallbackBounds.east},${mapState.fallbackBounds.north}`,
    layer: 'mapnik'
  });
  const heat = heatCells.map(cell => {
    const point = fallbackMapPercent(mapPoint(cell));
    const intensity = Math.sqrt(cell.sample_count / maxSamples);
    const color = intensity >= 0.75 ? '#ef4444' : intensity >= 0.4 ? '#f59e0b' : '#38bdf8';
    return `<circle cx="${point.x}" cy="${point.y}" r="${14 + intensity * 12}" fill="${color}" fill-opacity="${0.12 + intensity * 0.18}"><title>Simulated route heat · ${cell.sample_count} route samples</title></circle>`;
  }).join('');
  const routes = deliveries.map(item => {
    const pickup = fallbackMapPercent(mapPoint(item.pickup));
    const destination = fallbackMapPercent(mapPoint(item.destination));
    return `<line x1="${pickup.x}" y1="${pickup.y}" x2="${destination.x}" y2="${destination.y}" stroke="${escapeHTML(item.color)}" stroke-width="4" stroke-opacity="0.9" stroke-linecap="round"><title>${escapeHTML(item.delivery_id)} · simulated planned route</title></line>`;
  }).join('');
  const endpoints = deliveries.map(item => {
    const pickup = fallbackMapPercent(mapPoint(item.pickup));
    const destination = fallbackMapPercent(mapPoint(item.destination));
    return `
      <g><circle cx="${pickup.x}" cy="${pickup.y}" r="9" fill="#0f172a" stroke="#f8fafc" stroke-width="3"/><text x="${pickup.x}" y="${pickup.y + 4}" text-anchor="middle" fill="#f8fafc" font-size="10" font-weight="700">P</text><title>${escapeHTML(item.delivery_id)} pickup · SIMULATED</title></g>
      <g><path d="M ${destination.x} ${destination.y - 10} L ${destination.x + 9} ${destination.y} L ${destination.x} ${destination.y + 10} L ${destination.x - 9} ${destination.y} Z" fill="${escapeHTML(item.color)}" stroke="#0f172a" stroke-width="2"/><text x="${destination.x}" y="${destination.y + 4}" text-anchor="middle" fill="#0f172a" font-size="10" font-weight="700">D</text><title>${escapeHTML(item.delivery_id)} delivery destination · SIMULATED</title></g>
    `;
  }).join('');
  const drones = deliveries.map((item, index) => {
    const point = fallbackMapPercent(mapPoint(item.position));
    return `<g><circle cx="${point.x}" cy="${point.y}" r="15" fill="${escapeHTML(item.color)}" stroke="#ffffff" stroke-width="3"/><text x="${point.x}" y="${point.y + 5}" text-anchor="middle" fill="#07111e" font-size="12" font-weight="800">${index + 1}</text><title>${escapeHTML(item.drone_id)} · ${escapeHTML(item.area)} · SIMULATED, not live GPS</title></g>`;
  }).join('');
  const visible = Number(mapState.fallbackBounds.north) > Number(mapState.fallbackBounds.south)
    && Number(mapState.fallbackBounds.east) > Number(mapState.fallbackBounds.west);
  if (!visible) return;
  fallback.className = 'fallback-map fallback-map-bengaluru';
  fallback.setAttribute('aria-label', 'OpenStreetMap of Bengaluru with simulated demo drone, pickup, delivery and heat locations.');
  fallback.innerHTML = `
    <iframe title="OpenStreetMap of Bengaluru" src="${BENGALURU_MAP_TILE_URL}?${params.toString()}" loading="lazy" referrerpolicy="no-referrer" tabindex="-1"></iframe>
    <svg class="fallback-demo-overlay" viewBox="0 0 1000 600" preserveAspectRatio="none" role="img" aria-label="Simulated drone routes and delivery locations">${heat}${routes}${endpoints}${drones}</svg>
    <span class="fallback-map-attribution">Map data © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a> · ${northWest} to ${southEast}</span>
    <span class="fallback-map-legend"><span>Drone</span><span>Pickup</span><span>Drop-off</span><span>Heat</span></span>
  `;
  mapState.fallbackMapSignature = signature;
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
  if (!mapState.map) {
    renderFallbackBengaluruMap();
    return;
  }
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
  updateBengaluruDemoOverlays();
  updateTwinOverlays();
}

function updateBengaluruDemoOverlays() {
  if (!mapState.map) return;
  const enabled = document.getElementById('showBengaluruDemo').checked;
  const deliveries = enabled ? mapState.bengaluruDemo?.deliveries || [] : [];
  const droneIds = new Set();
  const routeIds = new Set();
  const endpointIds = new Set();
  const heatIds = new Set();
  for (const [index, item] of deliveries.entries()) {
    droneIds.add(item.drone_id);
    const droneMarker = upsertMarker(mapState.demoMarkers, item.drone_id, {
      map: mapState.map,
      position: googlePosition(item.position),
      title: `${item.drone_id} · ${item.area} · SIMULATED, not live GPS`,
      icon: {
        path: google.maps.SymbolPath.CIRCLE,
        scale: 10,
        fillColor: item.color,
        fillOpacity: 1,
        strokeColor: '#ffffff',
        strokeWeight: 2
      },
      label: {text: String(index + 1), color: '#07111e', fontWeight: 'bold'},
      zIndex: 15
    });
    droneMarker.setMap(mapState.map);

    routeIds.add(item.delivery_id);
    const route = upsertMarker(mapState.demoRoutes, item.delivery_id, {
      map: mapState.map,
      path: [googlePosition(item.pickup), googlePosition(item.destination)],
      geodesic: true,
      strokeColor: item.color,
      strokeOpacity: 0.85,
      strokeWeight: 4,
      icons: [{icon: {path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW}, offset: '75%'}]
    });
    route.setMap(mapState.map);

    for (const [kind, point, label, color] of [
      ['pickup', item.pickup, 'P', '#f8fafc'],
      ['destination', item.destination, 'D', item.color]
    ]) {
      const key = `${item.delivery_id}:${kind}`;
      endpointIds.add(key);
      const marker = upsertMarker(mapState.demoDeliveryMarkers, key, {
        map: mapState.map,
        position: googlePosition(point),
        title: `${item.delivery_id} · ${kind} · ${item.area} · SIMULATED`,
        icon: {
          path: kind === 'pickup' ? google.maps.SymbolPath.CIRCLE : google.maps.SymbolPath.BACKWARD_CLOSED_ARROW,
          scale: kind === 'pickup' ? 7 : 8,
          fillColor: color,
          fillOpacity: 1,
          strokeColor: '#0f172a',
          strokeWeight: 2
        },
        label: {text: label, color: kind === 'pickup' ? '#0f172a' : '#ffffff', fontWeight: 'bold'}
      });
      marker.setMap(mapState.map);
    }
  }
  removeUnused(mapState.demoMarkers, droneIds);
  removeUnused(mapState.demoRoutes, routeIds);
  removeUnused(mapState.demoDeliveryMarkers, endpointIds);

  const cells = enabled ? mapState.bengaluruDemo?.heat_cells || [] : [];
  const maxSamples = Math.max(1, ...cells.map(cell => cell.sample_count));
  const halfCell = (mapState.bengaluruDemo?.heat_cell_size_deg || 0.005) / 2;
  for (const cell of cells) {
    const key = `${cell.latitude}:${cell.longitude}`;
    heatIds.add(key);
    const intensity = Math.sqrt(cell.sample_count / maxSamples);
    const color = intensity >= 0.75 ? '#ef4444' : intensity >= 0.4 ? '#f59e0b' : '#38bdf8';
    const options = {
      bounds: {
        north: cell.latitude + halfCell,
        south: cell.latitude - halfCell,
        east: cell.longitude + halfCell,
        west: cell.longitude - halfCell
      },
      fillColor: color,
      fillOpacity: 0.12 + 0.24 * intensity,
      strokeColor: color,
      strokeOpacity: 0.4,
      strokeWeight: 1,
      map: mapState.map,
      zIndex: 2
    };
    const rectangle = mapState.demoHeatCells.get(key);
    if (!rectangle) mapState.demoHeatCells.set(key, new google.maps.Rectangle(options));
    else rectangle.setOptions(options);
  }
  removeUnused(mapState.demoHeatCells, heatIds);
  for (const rectangle of mapState.demoHeatCells.values()) rectangle.setMap(mapState.map);
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
  if (!mapState.map) {
    const points = document.getElementById('showBengaluruDemo').checked
      ? (mapState.bengaluruDemo?.deliveries || []).flatMap(item => [
        mapPoint(item.position), mapPoint(item.pickup), mapPoint(item.destination)
      ])
      : [];
    if (!points.length) return;
    const latitudes = points.map(point => point.latitude);
    const longitudes = points.map(point => point.longitude);
    const latPadding = Math.max(0.005, (Math.max(...latitudes) - Math.min(...latitudes)) * 0.14);
    const lngPadding = Math.max(0.005, (Math.max(...longitudes) - Math.min(...longitudes)) * 0.14);
    mapState.fallbackBounds = {
      south: Math.min(...latitudes) - latPadding,
      west: Math.min(...longitudes) - lngPadding,
      north: Math.max(...latitudes) + latPadding,
      east: Math.max(...longitudes) + lngPadding
    };
    renderFallbackBengaluruMap();
    return;
  }
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
  if (document.getElementById('showBengaluruDemo').checked) {
    for (const item of mapState.bengaluruDemo?.deliveries || []) {
      add(item.position);
      add(item.pickup);
      add(item.destination);
    }
  }
  if (points) {
    mapState.map.fitBounds(bounds);
    if (points === 1) mapState.map.setZoom(15);
  }
}

function zoomFallbackMap(factor) {
  const bounds = mapState.fallbackBounds;
  const centerLatitude = (bounds.south + bounds.north) / 2;
  const centerLongitude = (bounds.west + bounds.east) / 2;
  const halfLatitudeSpan = Math.max(0.003, (bounds.north - bounds.south) / (2 * factor));
  const halfLongitudeSpan = Math.max(0.003, (bounds.east - bounds.west) / (2 * factor));
  mapState.fallbackBounds = {
    south: centerLatitude - halfLatitudeSpan,
    west: centerLongitude - halfLongitudeSpan,
    north: centerLatitude + halfLatitudeSpan,
    east: centerLongitude + halfLongitudeSpan
  };
  renderFallbackBengaluruMap();
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
  try {
    const demo = await getJSON('/api/demo/bengaluru');
    if (
      demo?.city !== 'Bengaluru'
      || demo.origin !== 'SIMULATED'
      || !Array.isArray(demo.drones)
      || demo.drones.length !== 5
      || !Array.isArray(demo.deliveries)
      || demo.deliveries.length !== 5
      || !Array.isArray(demo.heat_cells)
    ) {
      throw new Error('Bengaluru sample response is invalid.');
    }
    mapState.bengaluruDemo = demo;
    renderBengaluruDemoList();
    if (!mapState.mapReady && !mapState.mapError) {
      setMapState('warning', mapStatus.textContent, 'OpenStreetMap fallback shows five simulated Bengaluru drones, sample delivery locations and a demo-only route heatmap. This is not live GPS.');
    }
    renderMap();
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'request failed';
    document.getElementById('bengaluruDemoList').textContent = `Bengaluru sample unavailable: ${detail}`;
  }
  pollDashboard();
  try {
    const twin = await getJSON('/api/twin');
    if (!twin || !Array.isArray(twin.corridors) || !Array.isArray(twin.zones) || !Array.isArray(twin.scenarios)) {
      throw new Error('Digital twin response is invalid.');
    }
    mapState.twin = twin;
    renderTwin(twin);
    populateWorkbenchOptions();
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
    mapElement.innerHTML = '<div class="fallback-map"><strong>Google Maps rejected the key</strong><p>Backend telemetry remains available. The Bengaluru sample is also available on the OpenStreetMap fallback.</p></div>';
    renderFallbackBengaluruMap();
  };
  const script = document.createElement('script');
  script.id = 'googleMapsScript';
  script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(apiKey)}&callback=stormpilotMapsLoaded&loading=async&v=weekly`;
  script.async = true;
  script.defer = true;
  script.onerror = () => {
    mapState.mapError = true;
    setMapState('error', 'Google Maps network/API error', 'The Maps script failed to load. Check network access, billing, API restrictions and the browser console.');
    mapElement.innerHTML = '<div class="fallback-map"><strong>Google Maps network/API error</strong><p>The Bengaluru sample is available on the OpenStreetMap fallback.</p></div>';
    renderFallbackBengaluruMap();
  };
  document.head.appendChild(script);
}

const workbench = {
  user: null,
  optimization: null,
  scenario: null,
  replay: null,
  replayIndex: 0,
  replayTimer: null,
  agile: null,
  agileSprintFilter: 'all',
  agileLoading: false
};

function csrfToken() {
  const entry = document.cookie.split('; ').find(value => value.startsWith('stormpilot_csrf='));
  return entry ? decodeURIComponent(entry.slice('stormpilot_csrf='.length)) : '';
}

async function apiRequest(path, method = 'GET', body) {
  const headers = {'Content-Type': 'application/json'};
  if (!['GET', 'HEAD'].includes(method)) {
    const token = csrfToken();
    if (token) headers['X-CSRF-Token'] = token;
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error('The server returned an invalid response.');
  }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
  return payload;
}

function setWorkbenchMessage(id, message, type = '') {
  const element = document.getElementById(id);
  element.textContent = message;
  element.className = `status-block ${type}`;
}

function populateSelect(id, items, selectedValue, emptyLabel) {
  const select = document.getElementById(id);
  if (!select) return;
  const previous = selectedValue ?? select.value;
  const options = items.map(item => `<option value="${escapeHTML(item.value)}">${escapeHTML(item.label)}</option>`).join('');
  select.innerHTML = options || `<option value="">${escapeHTML(emptyLabel)}</option>`;
  if (items.some(item => item.value === previous)) select.value = previous;
}

function populateWorkbenchOptions() {
  const drones = mapState.fleet?.drones || [];
  const deliveries = mapState.fleet?.deliveries || [];
  populateSelect('scenarioDrone', drones.map(item => ({value: item.drone_id, label: item.drone_id})), null, 'No registered drones');
  populateSelect('replayDrone', drones.map(item => ({value: item.drone_id, label: item.drone_id})), null, 'No registered drones');
  populateSelect('replayDelivery', deliveries.map(item => ({value: item.delivery_id, label: `${item.delivery_id} · ${item.status}`})), null, 'No persisted deliveries');
  populateSelect('scenarioDelivery', deliveries.map(item => ({value: item.delivery_id, label: `${item.delivery_id} · ${item.status}`})), null, 'No persisted deliveries');
  populateSelect('scenarioCorridor', (mapState.twin?.corridors || []).map(item => ({value: item.id, label: item.name})), null, 'No virtual corridors');
}

function updateScenarioFields() {
  const kind = document.getElementById('scenarioType').value;
  document.querySelectorAll('[data-scenario-fields]').forEach(field => {
    field.hidden = !field.dataset.scenarioFields.split(' ').includes(kind);
  });
}

function setAuthState(user) {
  workbench.user = user;
  const form = document.getElementById('loginForm');
  form.hidden = Boolean(user);
  document.getElementById('logoutButton').hidden = !user;
  document.getElementById('authRole').textContent = user ? `${user.username} · ${user.role}` : 'Signed out';
  document.getElementById('authRole').className = `pill ${user ? 'success' : 'neutral'}`;
  document.getElementById('userManagement').hidden = user?.role !== 'ADMIN';
  const canPlan = ['ADMIN', 'OPERATOR'].includes(user?.role);
  document.getElementById('optimizeButton').disabled = !canPlan;
  document.getElementById('scenarioForm').querySelector('button[type="submit"]').disabled = !canPlan;
  document.getElementById('replayForm').querySelector('button[type="submit"]').disabled = !user;
  document.getElementById('refreshScenarios').disabled = !user;
  const canEditAgile = ['ADMIN', 'OPERATOR'].includes(user?.role);
  document.getElementById('agileTaskForm').hidden = !canEditAgile;
  document.getElementById('agileSprintForm').hidden = !canEditAgile;
  document.getElementById('agileSignInLink').hidden = Boolean(user);
  setWorkbenchMessage('authMessage', user
    ? `Signed in as ${user.username}. Role permissions are enforced by the server.`
    : 'Sign in to access administration, planning, scenarios and replay.', user ? 'success' : '');
  if (user?.role === 'ADMIN') refreshUsers();
  if (user) {
    refreshScenarioHistory();
    refreshAgileBoard();
    setWorkbenchMessage('optimizerMessage', `Signed in as ${user.role}; request a deterministic plan.`, '');
    setWorkbenchMessage('scenarioMessage', `Signed in as ${user.role}; scenarios are isolated and labelled SIMULATED.`, '');
    setWorkbenchMessage('replayMessage', 'Select a drone and bounded time range to view accepted persisted GPS.', '');
  } else {
    document.getElementById('userList').replaceChildren();
    workbench.agile = null;
    workbench.agileSprintFilter = 'all';
    document.getElementById('agileColumns').replaceChildren();
    document.getElementById('agileSummary').replaceChildren();
    document.getElementById('agileMessage').className = 'status-block';
    document.getElementById('agileMessage').textContent = 'Sign in to view the team board.';
  }
}

async function refreshSession() {
  try {
    const payload = await apiRequest('/api/auth/session');
    setAuthState(payload.user);
  } catch {
    setAuthState(null);
  }
}

function showError(id, error) {
  setWorkbenchMessage(id, error instanceof Error ? error.message : 'The request failed.', 'error');
}

const AGILE_STATUSES = ['TODO', 'IN_PROGRESS', 'REVIEW', 'DONE'];
const AGILE_PRIORITIES = ['LOW', 'NORMAL', 'HIGH', 'URGENT'];

function agileSprintOptions(selectedId = '') {
  const sprints = workbench.agile?.sprints || [];
  return `<option value="">Backlog</option>${sprints.map(sprint => `
    <option value="${escapeHTML(sprint.sprint_id)}" ${sprint.sprint_id === selectedId ? 'selected' : ''}>${escapeHTML(sprint.name)} · ${escapeHTML(sprint.state)}</option>
  `).join('')}`;
}

function agileAssigneeOptions(selectedId = '') {
  const users = workbench.agile?.users || [];
  return `<option value="">Unassigned</option>${users.map(user => `
    <option value="${escapeHTML(user.user_id)}" ${user.user_id === selectedId ? 'selected' : ''}>${escapeHTML(user.username)}</option>
  `).join('')}`;
}

function renderAgileBoard(payload) {
  if (!payload || !Array.isArray(payload.tasks) || !Array.isArray(payload.sprints) || !Array.isArray(payload.users)) {
    throw new Error('Agile board response is invalid.');
  }
  workbench.agile = payload;
  const filter = document.getElementById('agileSprintFilter');
  const previousFilter = workbench.agileSprintFilter || filter.value || 'all';
  const filterOptions = [
    '<option value="all">All work</option>',
    '<option value="backlog">Backlog</option>',
    ...payload.sprints.map(sprint => `<option value="${escapeHTML(sprint.sprint_id)}">${escapeHTML(sprint.name)} · ${escapeHTML(sprint.state)}</option>`)
  ];
  filter.innerHTML = filterOptions.join('');
  workbench.agileSprintFilter = [...filter.options].some(option => option.value === previousFilter)
    ? previousFilter
    : payload.sprints.find(sprint => sprint.state === 'ACTIVE')?.sprint_id || 'all';
  filter.value = workbench.agileSprintFilter;

  for (const formId of ['agileTaskForm']) {
    const form = document.getElementById(formId);
    const selectedSprint = form.elements.sprint_id.value;
    form.elements.sprint_id.innerHTML = agileSprintOptions(selectedSprint);
    form.elements.assignee_id.innerHTML = agileAssigneeOptions(form.elements.assignee_id.value);
  }

  const visibleTasks = payload.tasks.filter(task => {
    if (workbench.agileSprintFilter === 'all') return true;
    if (workbench.agileSprintFilter === 'backlog') return !task.sprint_id;
    return task.sprint_id === workbench.agileSprintFilter;
  });
  const activeSprint = payload.sprints.find(sprint => sprint.state === 'ACTIVE');
  const points = visibleTasks.reduce((total, task) => total + (task.story_points || 0), 0);
  document.getElementById('agileSummary').innerHTML = `
    <div><strong>${visibleTasks.length}</strong><span>Work items</span></div>
    <div><strong>${points}</strong><span>Story points</span></div>
    <div><strong>${escapeHTML(activeSprint?.name || 'No active sprint')}</strong><span>Current sprint</span></div>
    <div><strong>${payload.sprints.length}</strong><span>Sprints</span></div>
  `;
  const selectedSprint = payload.sprints.find(sprint => sprint.sprint_id === workbench.agileSprintFilter);
  const sprintManage = document.getElementById('agileSprintManage');
  sprintManage.hidden = !selectedSprint || !['ADMIN', 'OPERATOR'].includes(workbench.user?.role);
  if (selectedSprint) {
    document.getElementById('agileSprintName').textContent = `${selectedSprint.name} · ${selectedSprint.state}`;
    const dateRange = [selectedSprint.starts_on, selectedSprint.ends_on].filter(Boolean).join(' – ');
    document.getElementById('agileSprintInfo').textContent = [selectedSprint.goal, dateRange].filter(Boolean).join(' · ');
    document.getElementById('startAgileSprint').hidden = selectedSprint.state !== 'PLANNED';
    document.getElementById('completeAgileSprint').hidden = selectedSprint.state !== 'ACTIVE';
  }

  const columns = [
    {status: 'TODO', label: 'To do'},
    {status: 'IN_PROGRESS', label: 'In progress'},
    {status: 'REVIEW', label: 'Review'},
    {status: 'DONE', label: 'Done'}
  ];
  document.getElementById('agileColumns').innerHTML = columns.map(column => {
    const tasks = visibleTasks.filter(task => task.status === column.status);
    return `
      <section class="agile-column" aria-labelledby="agile-${column.status}">
        <div class="agile-column-heading"><h3 id="agile-${column.status}">${column.label}</h3><span class="pill neutral">${tasks.length}</span></div>
        <div class="agile-task-list">${tasks.map(renderAgileTask).join('') || '<p class="agile-empty">No work items</p>'}</div>
      </section>
    `;
  }).join('');
  const editable = ['ADMIN', 'OPERATOR'].includes(workbench.user?.role);
  document.querySelectorAll('.agile-task-status, .agile-task-edit input, .agile-task-edit textarea, .agile-task-edit select')
    .forEach(control => { control.disabled = !editable; });
  document.querySelectorAll('.agile-task-edit button[type="submit"]').forEach(button => { button.hidden = !editable; });
  document.querySelectorAll('[data-delete-agile-task]').forEach(button => {
    button.hidden = workbench.user?.role !== 'ADMIN';
  });
}

function renderAgileTask(task) {
  const editKey = escapeHTML(task.task_id);
  return `
    <article class="agile-task" data-task-id="${editKey}">
      <div class="agile-task-heading">
        <span class="status-tag agile-priority agile-priority-${escapeHTML(task.priority.toLowerCase())}">${escapeHTML(task.priority)}</span>
        ${task.story_points == null ? '' : `<span class="pill neutral">${escapeHTML(task.story_points)} pts</span>`}
      </div>
      <h4>${escapeHTML(task.title)}</h4>
      <p class="agile-task-description">${escapeHTML(task.description || 'No description provided.')}</p>
      <div class="agile-task-meta">
        <span>${escapeHTML(task.assignee || 'Unassigned')}</span>
        <span>${escapeHTML(task.sprint_name || 'Backlog')}</span>
      </div>
      <label class="agile-status-control">Move to
        <select class="agile-task-status" aria-label="Move ${escapeHTML(task.title)} to another status" data-task-id="${editKey}">
          ${AGILE_STATUSES.map(status => `<option value="${status}" ${status === task.status ? 'selected' : ''}>${status.replace('_', ' ')}</option>`).join('')}
        </select>
      </label>
      <details class="agile-edit-details">
        <summary>Edit work item</summary>
        <form class="agile-task-edit" data-task-id="${editKey}">
          <label>Title <input name="title" required maxlength="160" value="${escapeHTML(task.title)}"></label>
          <label>Description <textarea name="description" maxlength="2000" rows="3">${escapeHTML(task.description)}</textarea></label>
          <label>Status <select name="status">${AGILE_STATUSES.map(status => `<option value="${status}" ${status === task.status ? 'selected' : ''}>${status.replace('_', ' ')}</option>`).join('')}</select></label>
          <label>Priority <select name="priority">${AGILE_PRIORITIES.map(priority => `<option value="${priority}" ${priority === task.priority ? 'selected' : ''}>${priority}</option>`).join('')}</select></label>
          <label>Story points <input name="story_points" type="number" min="1" max="100" step="1" value="${task.story_points ?? ''}"></label>
          <label>Assignee <select name="assignee_id">${agileAssigneeOptions(task.assignee_id || '')}</select></label>
          <label>Sprint <select name="sprint_id">${agileSprintOptions(task.sprint_id || '')}</select></label>
          <button class="button secondary" type="submit">Save changes</button>
          <button class="button secondary" type="button" data-delete-agile-task="${editKey}">Delete</button>
        </form>
      </details>
    </article>
  `;
}

async function refreshAgileBoard() {
  if (!workbench.user || workbench.agileLoading) return;
  workbench.agileLoading = true;
  setWorkbenchMessage('agileMessage', 'Loading saved Agile work items…');
  try {
    const payload = await apiRequest('/api/agile');
    renderAgileBoard(payload);
    const taskCount = payload.tasks.length;
    setWorkbenchMessage('agileMessage', `${taskCount} saved work item${taskCount === 1 ? '' : 's'} · Changes are shared with signed-in team members.`, 'success');
  } catch (error) {
    showError('agileMessage', error);
  } finally {
    workbench.agileLoading = false;
  }
}

function agileTaskPayload(form) {
  const values = Object.fromEntries(new FormData(form));
  return {
    ...values,
    story_points: values.story_points ? Number(values.story_points) : null,
    assignee_id: values.assignee_id || null,
    sprint_id: values.sprint_id || null
  };
}

function renderUsers(users) {
  const list = document.getElementById('userList');
  if (!users.length) {
    list.textContent = 'No administrator accounts are available.';
    return;
  }
  list.innerHTML = users.map(user => `
    <form class="user-row" data-user-id="${escapeHTML(user.user_id)}">
      <strong>${escapeHTML(user.username)}</strong>
      <label>Role <select name="role">${['ADMIN', 'OPERATOR', 'VIEWER'].map(role => `<option ${role === user.role ? 'selected' : ''}>${role}</option>`).join('')}</select></label>
      <label class="inline-check"><input name="enabled" type="checkbox" ${user.enabled ? 'checked' : ''}> Enabled</label>
      <label>Rotate password <input name="password" type="password" minlength="12" maxlength="1024" autocomplete="new-password" placeholder="Leave blank to keep"></label>
      <button class="button secondary" type="submit">Save account</button>
    </form>
  `).join('');
}

async function refreshUsers() {
  try {
    const payload = await apiRequest('/api/admin/users');
    renderUsers(payload.users);
  } catch (error) {
    showError('userMessage', error);
    document.getElementById('userMessage').hidden = false;
  }
}

function renderOptimization(result) {
  const strategies = result.strategies.map(strategy => `
    <article class="strategy-card">
      <h3>${escapeHTML(strategy.strategy.replaceAll('_', ' '))}</h3>
      <p>${strategy.assignments.length} assignments · ${strategy.unassigned.length} unassigned</p>
      <p>${formatNumber(strategy.total_planned_distance_km, 3)} km planned · ${formatNumber(strategy.estimated_total_distance_km, 3)} km incl. reposition</p>
      <ul>${strategy.assignments.map(item => `<li>${escapeHTML(item.delivery_id)} → ${escapeHTML(item.drone_id)} · ${formatNumber(item.estimated_duration_minutes)} min · ${escapeHTML(item.rationale)}</li>`).join('') || '<li>No feasible assignments.</li>'}</ul>
      ${strategy.unassigned.map(item => `<p class="warning-copy">${escapeHTML(item.delivery_id)} unassigned: ${escapeHTML(item.reason)}</p>`).join('')}
    </article>
  `).join('');
  document.getElementById('optimizerResults').innerHTML = `
    <p>Baseline: ${escapeHTML(result.origin)} · Total planned distance ${formatNumber(result.total_planned_distance_km, 3)} km · Estimated savings vs current ${formatNumber(result.estimated_distance_savings_vs_current_km, 3)} km.</p>
    <p class="muted-copy">${escapeHTML(result.distance_assumption)} ${escapeHTML(result.range_assumption)}</p>
    <div class="strategy-grid">${strategies}</div>
    ${result.constraint_violations.map(item => `<p class="warning-copy">${escapeHTML(item.delivery_id)} constraint: ${escapeHTML(item.reasons.join('; '))}</p>`).join('')}
  `;
}

function scenarioPayload() {
  const kind = document.getElementById('scenarioType').value;
  const payload = {scenario_type: kind};
  if (['drone_offline', 'range_limit'].includes(kind)) payload.drone_id = document.getElementById('scenarioDrone').value;
  if (['deadline_missed', 'mission_infeasible'].includes(kind)) payload.delivery_id = document.getElementById('scenarioDelivery').value;
  if (kind === 'deadline_missed') {
    const value = document.getElementById('scenarioDeadline').value;
    payload.deadline = value ? new Date(value).toISOString() : '';
  }
  if (kind === 'corridor_unavailable') payload.corridor_id = document.getElementById('scenarioCorridor').value;
  if (kind === 'range_limit') payload.remaining_range_km = Number(document.getElementById('scenarioRange').value);
  if (kind === 'mission_infeasible') payload.reason = document.getElementById('scenarioReason').value.trim();
  if (kind === 'high_priority_delivery') {
    payload.delivery_id = document.getElementById('hypotheticalDelivery').value.trim();
    payload.pickup = {
      lat: Number(document.getElementById('hypotheticalPickupLat').value),
      lng: Number(document.getElementById('hypotheticalPickupLng').value)
    };
    payload.destination = {
      lat: Number(document.getElementById('hypotheticalDestinationLat').value),
      lng: Number(document.getElementById('hypotheticalDestinationLng').value)
    };
    payload.planned_route_km = Number(document.getElementById('hypotheticalRouteKm').value);
    payload.deadline = null;
  }
  return payload;
}

function renderScenario(result) {
  workbench.scenario = result;
  const affectedPersisted = (result.affected_delivery_ids || []).some(id =>
    mapState.fleet?.deliveries.some(item => item.delivery_id === id && item.status === 'CREATED')
  );
  document.getElementById('scenarioResult').innerHTML = `
    <p><strong>SIMULATED</strong> · ${escapeHTML(result.scenario_type)} · Affected drones: ${escapeHTML(result.affected_drone_ids.join(', ') || 'none')} · Affected deliveries: ${escapeHTML(result.affected_delivery_ids.join(', ') || 'none')}</p>
    <p>Estimated distance change: ${formatNumber(result.estimated_distance_change_km, 3)} km. No live telemetry or delivery state was changed by simulation.</p>
    <h3>Recommendation rationale</h3><ul>${result.explanations.map(item => `<li>${escapeHTML(item)}</li>`).join('')}</ul>
    <h3>Infeasible assignments and warnings</h3><ul>${result.warnings.length ? result.warnings.map(item => `<li>${escapeHTML(item.message || `${item.delivery_id || ''} ${(item.reasons || []).join('; ')}`)}</li>`).join('') : '<li>No scenario warnings.</li>'}</ul>
    <button id="applyScenarioButton" class="button secondary" type="button" ${affectedPersisted ? '' : 'hidden'}>Approve and apply affected assignment proposal</button>
  `;
  const button = document.getElementById('applyScenarioButton');
  if (button) button.addEventListener('click', applyScenario);
}

async function refreshScenarioHistory() {
  if (!workbench.user) return;
  try {
    const payload = await apiRequest('/api/scenarios');
    const list = document.getElementById('scenarioHistory');
    list.innerHTML = payload.scenarios.length
      ? `<ul>${payload.scenarios.map(item => `<li><button class="text-button" type="button" data-scenario-id="${escapeHTML(item.scenario_id)}">${escapeHTML(item.scenario_type)} · ${escapeHTML(item.created_at)}</button> · origin SIMULATED · ${item.applied_at ? 'approved/applied' : 'not applied'}</li>`).join('')}</ul>`
      : '<p>No persisted scenarios yet.</p>';
    list.querySelectorAll('[data-scenario-id]').forEach(button => button.addEventListener('click', async () => {
      try {
        const scenario = await apiRequest(`/api/scenarios/${encodeURIComponent(button.dataset.scenarioId)}:replay`);
        renderScenario(scenario);
      } catch (error) {
        showError('scenarioMessage', error);
      }
    }));
  } catch (error) {
    document.getElementById('scenarioHistory').textContent = error instanceof Error ? error.message : 'Scenario history unavailable.';
  }
}

function stopReplay() {
  if (workbench.replayTimer !== null) clearInterval(workbench.replayTimer);
  workbench.replayTimer = null;
}

function renderReplayPosition() {
  const replay = workbench.replay;
  const position = document.getElementById('replayPosition');
  const timeline = document.getElementById('replayTimeline');
  if (!replay || replay.points.length === 0) {
    position.textContent = 'No recorded GPS points in this time range; missing coordinates were not synthesized.';
    timeline.replaceChildren();
    mapState.replayPath?.setMap(null);
    mapState.replayMarker?.setMap(null);
    mapState.replayPlannedPath?.setMap(null);
    return;
  }
  const point = replay.points[workbench.replayIndex];
  const firstTime = new Date(replay.points[0].timestamp).getTime();
  const elapsed = Math.max(0, (new Date(point.device_timestamp).getTime() - firstTime) / 1000);
  position.textContent = `${workbench.replayIndex + 1}/${replay.points.length} · ${formatTime(point.device_timestamp)} · ${formatNumber(point.latitude, 6)}, ${formatNumber(point.longitude, 6)} · accuracy ${formatNumber(point.accuracy_m, 1)} m · elapsed ${formatNumber(elapsed, 0)} s`;
  timeline.innerHTML = `<input id="replaySlider" type="range" min="0" max="${replay.points.length - 1}" value="${workbench.replayIndex}" aria-label="Replay point">`;
  document.getElementById('replaySlider').addEventListener('input', event => {
    workbench.replayIndex = Number(event.target.value);
    renderReplayPosition();
    updateReplayMap();
  });
  document.getElementById('replayBack').disabled = workbench.replayIndex === 0;
  document.getElementById('replayForward').disabled = workbench.replayIndex >= replay.points.length - 1;
  updateReplayMap();
}

function updateReplayMap() {
  if (!mapState.map || !workbench.replay?.points.length) return;
  const points = workbench.replay.points;
  const path = points.map(point => ({lat: point.latitude, lng: point.longitude}));
  const lineOptions = {
    map: mapState.map,
    path,
    geodesic: true,
    strokeColor: '#f8fafc',
    strokeOpacity: 0.75,
    strokeWeight: 3
  };
  if (!mapState.replayPath) mapState.replayPath = new google.maps.Polyline(lineOptions);
  else mapState.replayPath.setOptions(lineOptions);
  const current = points[workbench.replayIndex];
  const markerOptions = {
    map: mapState.map,
    position: {lat: current.latitude, lng: current.longitude},
    title: `Replay · ${current.device_timestamp}`,
    icon: {path: google.maps.SymbolPath.CIRCLE, scale: 7, fillColor: '#f8fafc', fillOpacity: 1, strokeColor: '#0b1220', strokeWeight: 2}
  };
  if (!mapState.replayMarker) mapState.replayMarker = new google.maps.Marker(markerOptions);
  else mapState.replayMarker.setOptions(markerOptions);
  const fleetDrone = mapState.fleet?.drones.find(item => item.drone_id === workbench.replay.drone_id);
  const delivery = workbench.replay.delivery;
  const pickup = delivery?.pickup || fleetDrone?.pickup;
  const destination = delivery?.destination || fleetDrone?.destination;
  if (pickup && destination) {
    const plannedOptions = {
      map: mapState.map,
      path: [googlePosition(pickup), googlePosition(destination)],
      geodesic: true,
      strokeColor: '#fbbf24',
      strokeOpacity: 0.9,
      strokeWeight: 3,
      icons: [{icon: {path: 'M 0,-1 0,1', strokeOpacity: 1, scale: 3}, offset: '0', repeat: '12px'}]
    };
    if (!mapState.replayPlannedPath) mapState.replayPlannedPath = new google.maps.Polyline(plannedOptions);
    else mapState.replayPlannedPath.setOptions(plannedOptions);
  } else {
    mapState.replayPlannedPath?.setMap(null);
  }
}

function stepReplay(delta) {
  if (!workbench.replay?.points.length) return;
  workbench.replayIndex = Math.max(0, Math.min(workbench.replay.points.length - 1, workbench.replayIndex + delta));
  renderReplayPosition();
}

async function loadReplay(event) {
  event.preventDefault();
  stopReplay();
  const start = new Date(document.getElementById('replayFrom').value);
  const end = new Date(document.getElementById('replayTo').value);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start >= end || end - start > 30 * 86400000) {
    setWorkbenchMessage('replayMessage', 'Choose a valid range of at most 30 days.', 'error');
    return;
  }
  const target = document.getElementById('replayTarget').value;
  const params = new URLSearchParams({
    [target === 'drone' ? 'drone_id' : 'delivery_id']: document.getElementById(target === 'drone' ? 'replayDrone' : 'replayDelivery').value,
    from: start.toISOString(),
    to: end.toISOString(),
    limit: '2000'
  });
  setWorkbenchMessage('replayMessage', 'Loading bounded persisted replay…');
  try {
    const replay = await apiRequest(`/api/replay?${params}`);
    if (!Array.isArray(replay.points) || !Array.isArray(replay.events) || !Array.isArray(replay.gaps)) throw new Error('Replay response is invalid.');
    workbench.replay = replay;
    workbench.replayTarget = target;
    workbench.replayIndex = 0;
    for (const id of ['replayPlay', 'replayPause', 'replayBack', 'replayForward', 'replayReport']) {
      document.getElementById(id).disabled = replay.points.length === 0;
    }
    document.getElementById('replayEvents').innerHTML = `
      <p>Origin: ${escapeHTML(replay.origin)} · ${replay.points.length} accepted fixes · ${replay.gaps.length} recording gaps · ${replay.events.length} events.</p>
      <p>${replay.delivery?.planned_distance_km == null ? 'Planned route distance is unavailable for this selection.' : `Planned route distance: ${formatNumber(replay.delivery.planned_distance_km, 3)} km (separate from observed GPS).`}</p>
      <ul>${replay.events.map(item => `<li>${escapeHTML(item.timestamp)} · ${escapeHTML(item.event_type)} · ${escapeHTML(item.entity_id)}</li>`).join('') || '<li>No delivery events in this range.</li>'}</ul>
      <ul>${replay.gaps.map(item => `<li class="warning-copy">Recording gap: ${escapeHTML(item.from)} to ${escapeHTML(item.to)} (${formatNumber(item.duration_seconds, 0)} s)</li>`).join('') || '<li>No gaps over the reporting threshold detected.</li>'}</ul>
    `;
    setWorkbenchMessage('replayMessage', replay.points.length ? replay.message : 'No recorded GPS points for this selection.', replay.points.length ? '' : 'warning');
    renderReplayPosition();
  } catch (error) {
    showError('replayMessage', error);
  }
}

async function downloadReplayReport() {
  if (!workbench.replay) return;
  const replay = workbench.replay;
  const params = new URLSearchParams({
    [workbench.replayTarget === 'delivery' ? 'delivery_id' : 'drone_id']: workbench.replayTarget === 'delivery' ? replay.delivery.delivery_id : replay.drone_id,
    from: replay.time_range.from,
    to: replay.time_range.to
  });
  try {
    const payload = await apiRequest(`/api/replay/report?${params}`);
    const blob = new Blob([JSON.stringify(payload.report, null, 2)], {type: 'application/json'});
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'stormpilot-mission-report.json';
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) {
    showError('replayMessage', error);
  }
}

document.getElementById('loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const payload = await apiRequest('/api/auth/login', 'POST', {
      username: document.getElementById('loginUsername').value,
      password: document.getElementById('loginPassword').value
    });
    document.getElementById('loginPassword').value = '';
    setAuthState(payload.user);
  } catch (error) {
    showError('authMessage', error);
  }
});

document.getElementById('logoutButton').addEventListener('click', async () => {
  try {
    await apiRequest('/api/auth/logout', 'POST', {});
    setAuthState(null);
    setWorkbenchMessage('optimizerMessage', 'Sign in as ADMIN or OPERATOR to request a plan.');
    setWorkbenchMessage('scenarioMessage', 'Sign in as ADMIN or OPERATOR to run a deterministic simulation.');
    setWorkbenchMessage('replayMessage', 'Sign in to query persisted route history.');
    document.getElementById('optimizerResults').replaceChildren();
    document.getElementById('scenarioResult').replaceChildren();
    stopReplay();
    mapState.replayPath?.setMap(null);
    mapState.replayMarker?.setMap(null);
    mapState.replayPlannedPath?.setMap(null);
  } catch (error) {
    showError('authMessage', error);
  }
});

document.getElementById('userCreateForm').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await apiRequest('/api/admin/users', 'POST', {
      username: document.getElementById('newUsername').value.trim(),
      password: document.getElementById('newPassword').value,
      role: document.getElementById('newRole').value
    });
    event.target.reset();
    document.getElementById('userMessage').hidden = false;
    setWorkbenchMessage('userMessage', 'Account created.', 'success');
    await refreshUsers();
  } catch (error) {
    document.getElementById('userMessage').hidden = false;
    showError('userMessage', error);
  }
});

document.getElementById('userList').addEventListener('submit', async event => {
  if (!event.target.matches('.user-row')) return;
  event.preventDefault();
  const form = event.target;
  const payload = {
    role: form.elements.role.value,
    enabled: form.elements.enabled.checked
  };
  if (form.elements.password.value) payload.password = form.elements.password.value;
  try {
    await apiRequest(`/api/admin/users/${encodeURIComponent(form.dataset.userId)}`, 'PUT', payload);
    form.elements.password.value = '';
    document.getElementById('userMessage').hidden = false;
    setWorkbenchMessage('userMessage', 'Account updated; affected sessions were revoked where required.', 'success');
    await refreshUsers();
    await refreshSession();
  } catch (error) {
    document.getElementById('userMessage').hidden = false;
    showError('userMessage', error);
  }
});

document.getElementById('optimizeButton').addEventListener('click', async () => {
  setWorkbenchMessage('optimizerMessage', 'Comparing deterministic assignment strategies…');
  try {
    const result = await apiRequest('/api/missions/optimize', 'POST', {});
    workbench.optimization = result;
    renderOptimization(result);
    document.getElementById('applyOptimizationButton').hidden = result.recommended_assignments.length === 0;
    setWorkbenchMessage('optimizerMessage', 'Plan ready. Review the constraints and explicitly approve before changing eligible live assignments.', 'success');
  } catch (error) {
    showError('optimizerMessage', error);
  }
});

document.getElementById('applyOptimizationButton').addEventListener('click', async () => {
  if (!workbench.optimization?.optimization_id) return;
  if (!window.confirm('Apply this reviewed assignment proposal to eligible CREATED deliveries?')) return;
  try {
    const result = await apiRequest('/api/missions/optimize/apply', 'POST', {optimization_id: workbench.optimization.optimization_id});
    setWorkbenchMessage('optimizerMessage', `${result.assignments_applied} assignment(s) applied by your approval.`, 'success');
    document.getElementById('applyOptimizationButton').hidden = true;
    refreshNow();
  } catch (error) {
    showError('optimizerMessage', error);
  }
});

document.getElementById('scenarioType').addEventListener('change', updateScenarioFields);
document.getElementById('scenarioForm').addEventListener('submit', async event => {
  event.preventDefault();
  setWorkbenchMessage('scenarioMessage', 'Capturing a persisted baseline and running the isolated scenario…');
  try {
    const result = await apiRequest('/api/scenarios', 'POST', scenarioPayload());
    renderScenario(result);
    setWorkbenchMessage('scenarioMessage', `Scenario ${result.scenario_id} stored as SIMULATED. No live GPS or delivery state changed.`, 'success');
    await refreshScenarioHistory();
  } catch (error) {
    showError('scenarioMessage', error);
  }
});

async function applyScenario() {
  if (!workbench.scenario || !window.confirm('Apply the reviewed proposal to eligible persisted CREATED deliveries?')) return;
  try {
    const result = await apiRequest(`/api/scenarios/${encodeURIComponent(workbench.scenario.scenario_id)}/apply`, 'POST', {approved: true});
    setWorkbenchMessage('scenarioMessage', `${result.assignments_applied} assignment(s) applied by operator approval.`, 'success');
    await refreshScenarioHistory();
    refreshNow();
  } catch (error) {
    showError('scenarioMessage', error);
  }
}

document.getElementById('refreshScenarios').addEventListener('click', refreshScenarioHistory);
document.getElementById('replayForm').addEventListener('submit', loadReplay);
document.getElementById('replayTarget').addEventListener('change', event => {
  const isDrone = event.target.value === 'drone';
  document.getElementById('replayDroneField').hidden = !isDrone;
  document.getElementById('replayDeliveryField').hidden = isDrone;
});
document.getElementById('replayPlay').addEventListener('click', () => {
  if (!workbench.replay?.points.length || workbench.replayTimer !== null) return;
  const speed = Number(document.getElementById('replaySpeed').value);
  workbench.replayTimer = setInterval(() => {
    if (workbench.replayIndex >= workbench.replay.points.length - 1) {
      stopReplay();
      return;
    }
    stepReplay(speed);
  }, 1000);
});
document.getElementById('replayPause').addEventListener('click', stopReplay);
document.getElementById('replayBack').addEventListener('click', () => stepReplay(-1));
document.getElementById('replayForward').addEventListener('click', () => stepReplay(1));
document.getElementById('replayReport').addEventListener('click', downloadReplayReport);

document.getElementById('refreshAgileBoard').addEventListener('click', refreshAgileBoard);
async function transitionAgileSprint(action) {
  const sprintId = workbench.agileSprintFilter;
  if (!sprintId || sprintId === 'all' || sprintId === 'backlog') return;
  try {
    await apiRequest(`/api/agile/sprints/${encodeURIComponent(sprintId)}`, 'PUT', {action});
    await refreshAgileBoard();
    setWorkbenchMessage('agileMessage', `Sprint ${action === 'start' ? 'started' : 'completed'}.`, 'success');
  } catch (error) {
    showError('agileMessage', error);
  }
}
document.getElementById('startAgileSprint').addEventListener('click', () => transitionAgileSprint('start'));
document.getElementById('completeAgileSprint').addEventListener('click', () => transitionAgileSprint('complete'));
document.getElementById('agileSprintFilter').addEventListener('change', event => {
  workbench.agileSprintFilter = event.target.value;
  if (workbench.agile) renderAgileBoard(workbench.agile);
});
document.getElementById('agileTaskForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await apiRequest('/api/agile/tasks', 'POST', agileTaskPayload(form));
    form.reset();
    form.elements.priority.value = 'NORMAL';
    const sprintId = workbench.agileSprintFilter !== 'all' && workbench.agileSprintFilter !== 'backlog'
      ? workbench.agileSprintFilter
      : '';
    form.elements.sprint_id.value = sprintId;
    await refreshAgileBoard();
    setWorkbenchMessage('agileMessage', 'Work item created.', 'success');
  } catch (error) {
    showError('agileMessage', error);
  }
});
document.getElementById('agileSprintForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  try {
    const result = await apiRequest('/api/agile/sprints', 'POST', values);
    form.reset();
    workbench.agileSprintFilter = result.sprint_id;
    await refreshAgileBoard();
    setWorkbenchMessage('agileMessage', `Sprint created and ${result.state.toLowerCase()}.`, 'success');
  } catch (error) {
    showError('agileMessage', error);
  }
});
document.getElementById('agileColumns').addEventListener('change', async event => {
  if (!event.target.matches('.agile-task-status')) return;
  const select = event.target;
  const form = select.closest('.agile-task').querySelector('.agile-task-edit');
  const payload = agileTaskPayload(form);
  payload.status = select.value;
  select.disabled = true;
  try {
    await apiRequest(`/api/agile/tasks/${encodeURIComponent(select.dataset.taskId)}`, 'PUT', payload);
    await refreshAgileBoard();
    setWorkbenchMessage('agileMessage', 'Work item moved.', 'success');
  } catch (error) {
    showError('agileMessage', error);
  } finally {
    select.disabled = false;
  }
});
document.getElementById('agileColumns').addEventListener('submit', async event => {
  if (!event.target.matches('.agile-task-edit')) return;
  event.preventDefault();
  const form = event.target;
  try {
    await apiRequest(`/api/agile/tasks/${encodeURIComponent(form.dataset.taskId)}`, 'PUT', agileTaskPayload(form));
    await refreshAgileBoard();
    setWorkbenchMessage('agileMessage', 'Work item updated.', 'success');
  } catch (error) {
    showError('agileMessage', error);
  }
});
document.getElementById('agileColumns').addEventListener('click', async event => {
  const button = event.target.closest('[data-delete-agile-task]');
  if (!button) return;
  button.disabled = true;
  try {
    await apiRequest(`/api/agile/tasks/${encodeURIComponent(button.dataset.deleteAgileTask)}`, 'DELETE');
    await refreshAgileBoard();
    setWorkbenchMessage('agileMessage', 'Work item deleted.', 'success');
  } catch (error) {
    button.disabled = false;
    showError('agileMessage', error);
  }
});

const defaultReplayTo = new Date();
const defaultReplayFrom = new Date(defaultReplayTo.getTime() - 24 * 60 * 60 * 1000);
document.getElementById('replayTo').value = new Date(defaultReplayTo.getTime() - defaultReplayTo.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
document.getElementById('replayFrom').value = new Date(defaultReplayFrom.getTime() - defaultReplayFrom.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
document.getElementById('scenarioDeadline').value = new Date(Date.now() - 60 * 60000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
updateScenarioFields();
refreshSession();

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
  else zoomFallbackMap(1.5);
});
document.getElementById('zoomOut').addEventListener('click', () => {
  if (mapState.map) mapState.map.setZoom(mapState.map.getZoom() - 1);
  else zoomFallbackMap(1 / 1.5);
});
document.getElementById('fitMap').addEventListener('click', fitVisibleMap);
document.getElementById('showBengaluruDemo').addEventListener('change', () => {
  renderBengaluruDemoList();
  renderMap();
});
window.addEventListener('load', bootDashboard, { once: true });

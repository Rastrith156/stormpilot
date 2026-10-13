"""Deterministic, explainable assignment recommendations for the demo fleet."""
import json
import math
import os
from datetime import datetime, timedelta, timezone

try:
    from app import db as store
except ImportError:  # pragma: no cover
    import db as store

FINAL_DELIVERY_STATES = {'DELIVERED', 'CANCELLED', 'FAILED'}
PRIORITY_ORDER = {'critical': 0, 'high': 1, 'normal': 2, 'low': 3}
EARTH_RADIUS_KM = 6371.0088


def capture_snapshot():
    now = datetime.now(timezone.utc)
    conn = store.get_connection()
    try:
        drone_rows = conn.execute(
            """
            SELECT d.drone_id,
                   t.latitude, t.longitude, t.accuracy, t.device_timestamp,
                   t.server_received_at, t.source,
                   COALESCE((SELECT SUM(CASE
                       WHEN quality_flags IS NULL OR quality_flags NOT LIKE '%GPS_JUMP%'
                       THEN COALESCE(distance_m, 0) ELSE 0 END)
                       FROM telemetry_samples WHERE drone_id = d.drone_id AND status = 'ACCEPTED'), 0) AS observed_distance_m
            FROM drone_registrations d
            LEFT JOIN telemetry_samples t ON t.id = (
                SELECT id FROM telemetry_samples
                WHERE drone_id = d.drone_id AND status = 'ACCEPTED'
                ORDER BY device_timestamp DESC, id DESC LIMIT 1
            )
            ORDER BY d.drone_id
            """
        ).fetchall()
        delivery_rows = conn.execute(
            "SELECT * FROM deliveries WHERE status NOT IN ('DELIVERED','CANCELLED','FAILED') ORDER BY delivery_id LIMIT 500"
        ).fetchall()
        twin_row = conn.execute(
            'SELECT config_json FROM digital_twin_config WHERE config_id = 1'
        ).fetchone()
    finally:
        conn.close()
    drones = []
    for row in drone_rows:
        received = _parse_time(row['server_received_at'])
        device = _parse_time(row['device_timestamp'])
        fresh = bool(
            received and device
            and 0 <= (now - received).total_seconds() <= 120
            and 0 <= (now - device).total_seconds() <= 120
        )
        drones.append({
            'drone_id': row['drone_id'],
            'position': (
                {'lat': row['latitude'], 'lng': row['longitude']}
                if row['latitude'] is not None and row['longitude'] is not None else None
            ),
            'state': 'PHONE_GPS' if fresh else 'STALE' if row['latitude'] is not None else 'OFFLINE',
            'accuracy_m': row['accuracy'],
            'observed_distance_km': round(float(row['observed_distance_m'] or 0) / 1000, 3),
        })
    deliveries = []
    for row in delivery_rows:
        evidence = _json_object(row['current_evidence'])
        deliveries.append({
            'delivery_id': row['delivery_id'],
            'drone_id': row['drone_id'],
            'status': row['status'],
            'pickup': _position(row['pickup_lat'], row['pickup_lng']),
            'destination': _position(row['destination_lat'], row['destination_lng']),
            'planned_route_km': row['planned_route_km'],
            'actual_observed_km': round(float(row['actual_observed_km'] or 0), 3),
            'priority': str(evidence.get('priority', 'normal')).lower(),
            'deadline': evidence.get('deadline'),
        })
    if twin_row:
        twin = _json_object(twin_row['config_json'])
    else:
        try:
            from app import api as api_module
        except ImportError:  # pragma: no cover
            import api as api_module
        twin = api_module.DEFAULT_TWIN_CONFIG
    return {
        'captured_at': _iso(now),
        'drones': drones,
        'deliveries': deliveries,
        'twin': twin,
    }


def optimize(snapshot=None, scenario=None):
    snapshot = json.loads(json.dumps(snapshot or capture_snapshot()))
    scenario = scenario or {}
    drones = {drone['drone_id']: dict(drone) for drone in snapshot['drones']}
    deliveries = [dict(item) for item in snapshot['deliveries']]
    for drone_id in scenario.get('offline_drone_ids', []):
        if drone_id in drones:
            drones[drone_id]['state'] = 'OFFLINE'
            drones[drone_id]['position'] = None
    for drone_id, budget in scenario.get('range_budgets', {}).items():
        if drone_id in drones:
            drones[drone_id]['range_budget_override_km'] = budget
    for delivery_id, deadline in scenario.get('deadline_overrides', {}).items():
        for delivery in deliveries:
            if delivery['delivery_id'] == delivery_id:
                delivery['deadline'] = deadline
    if scenario.get('forced_infeasible_delivery'):
        target = scenario['forced_infeasible_delivery']
        for delivery in deliveries:
            if delivery['delivery_id'] == target['delivery_id']:
                delivery['forced_violation'] = target['reason']
    if scenario.get('new_delivery'):
        deliveries.append(dict(scenario['new_delivery']))
    blocked_corridors = set(scenario.get('blocked_corridor_ids', []))
    now = _parse_time(snapshot['captured_at']) or datetime.now(timezone.utc)
    max_range = _positive_setting('STORMPILOT_MAX_DRONE_RANGE_KM', 25.0)
    default_budget = _positive_setting('STORMPILOT_REMAINING_RANGE_KM', max_range)
    speed = _positive_setting('STORMPILOT_ESTIMATED_SPEED_KMH', 35.0)
    twin = snapshot.get('twin') or {}
    zones = {
        zone['id']: zone for zone in twin.get('zones', [])
        if isinstance(zone, dict) and isinstance(zone.get('id'), str)
    }
    restrictions = [
        restriction for restriction in twin.get('restrictions', [])
        if isinstance(restriction, dict) and restriction.get('restricted') is True
    ]
    corridors = {
        corridor['id']: corridor for corridor in twin.get('corridors', [])
        if isinstance(corridor, dict) and isinstance(corridor.get('id'), str)
    }
    eligible_deliveries = [item for item in deliveries if item['status'] == 'CREATED']
    fixed_deliveries = [item for item in deliveries if item['status'] != 'CREATED']

    current = _current_strategy(
        drones, deliveries, max_range, default_budget, speed, now,
        zones, restrictions, corridors, blocked_corridors,
    )
    nearest = _assign_strategy(
        'nearest_eligible', eligible_deliveries, fixed_deliveries, drones,
        max_range, default_budget, speed, now, zones, restrictions,
        corridors, blocked_corridors,
    )
    optimized = _assign_strategy(
        'optimized', eligible_deliveries, fixed_deliveries, drones,
        max_range, default_budget, speed, now, zones, restrictions,
        corridors, blocked_corridors,
    )
    optimized['estimated_distance_savings_vs_current_km'] = round(
        max(0.0, current['estimated_total_distance_km'] - optimized['estimated_total_distance_km']), 3,
    )
    return {
        'origin': 'LIVE_PERSISTED_BASELINE',
        'generated_at': snapshot['captured_at'],
        'distance_assumption': 'Stored planned route distance is preferred. Missing route distance uses a 1.3x straight-line estimate; drone-to-pickup repositioning is a straight-line planning heuristic, not a road or safe flight route.',
        'range_assumption': f'Operator-configured nominal range and remaining budget; no battery telemetry is available. Maximum route distance: {max_range:g} km.',
        'estimated_speed_kmh': speed,
        'strategies': [current, nearest, optimized],
        'recommended_assignments': optimized['assignments'],
        'unassigned_deliveries': optimized['unassigned'],
        'constraint_violations': optimized['constraint_violations'],
        'total_planned_distance_km': optimized['total_planned_distance_km'],
        'workload_distribution': optimized['workload_distribution'],
        'estimated_distance_savings_vs_current_km': optimized['estimated_distance_savings_vs_current_km'],
    }


def _current_strategy(drones, deliveries, max_range, default_budget, speed, now, zones, restrictions, corridors, blocked):
    assignments = []
    unassigned = []
    violations = []
    total_planned = 0.0
    total_estimated = 0.0
    workload = {drone_id: {'delivery_count': 0, 'planned_distance_km': 0.0} for drone_id in drones}
    for delivery in deliveries:
        if delivery['status'] in FINAL_DELIVERY_STATES:
            continue
        drone = drones.get(delivery['drone_id'])
        route, method = _route_km(delivery)
        reposition = _haversine_km(drone['position'], delivery['pickup']) if drone and drone['position'] else None
        if delivery['status'] == 'CREATED':
            if drone and drone['state'] == 'PHONE_GPS':
                assignments.append(_assignment(delivery, drone['drone_id'], route, reposition or 0, method, speed, now, 'Existing persisted assignment.'))
            else:
                unassigned.append({'delivery_id': delivery['delivery_id'], 'reason': 'Current assignment has no fresh connected drone GPS.'})
        else:
            assignments.append(_assignment(delivery, delivery['drone_id'], route, reposition or 0, method, speed, now, 'Existing active assignment is held fixed.'))
        total_planned += route
        total_estimated += route + (reposition or 0)
        if drone:
            workload[drone['drone_id']]['delivery_count'] += 1
            workload[drone['drone_id']]['planned_distance_km'] += route
    return _strategy_result(
        'current_assignments', assignments, unassigned, violations,
        total_planned, total_estimated, workload,
    )


def _assign_strategy(name, pending, fixed, drones, max_range, default_budget, speed, now, zones, restrictions, corridors, blocked):
    workload = {drone_id: {'delivery_count': 0, 'planned_distance_km': 0.0} for drone_id in drones}
    busy = set()
    assignments = []
    unassigned = []
    violations = []
    total_planned = 0.0
    total_estimated = 0.0
    for delivery in fixed:
        if delivery['status'] in FINAL_DELIVERY_STATES:
            continue
        route, _ = _route_km(delivery)
        total_planned += route
        total_estimated += route
        if delivery['drone_id'] in workload:
            busy.add(delivery['drone_id'])
            workload[delivery['drone_id']]['delivery_count'] += 1
            workload[delivery['drone_id']]['planned_distance_km'] += route

    priorities = sorted(
        pending,
        key=lambda item: (
            PRIORITY_ORDER.get(item['priority'], PRIORITY_ORDER['normal']),
            _parse_time(item.get('deadline')) or datetime.max.replace(tzinfo=timezone.utc),
            item['delivery_id'],
        ),
    )
    for delivery in priorities:
        route, route_method = _route_km(delivery)
        reasons = []
        candidates = []
        for drone_id in sorted(drones):
            drone = drones[drone_id]
            candidate_reasons = []
            if drone_id in busy:
                candidate_reasons.append('Drone already has an active or recommended assignment.')
            if drone['state'] != 'PHONE_GPS' or drone['position'] is None:
                candidate_reasons.append(f'Drone is {drone["state"]}; a fresh connected position is required.')
            budget = float(drone.get('range_budget_override_km', default_budget))
            consumed = workload[drone_id]['planned_distance_km']
            remaining = max(0.0, budget - consumed)
            if route > max_range:
                candidate_reasons.append(f'Planned route exceeds configured maximum range ({max_range:g} km).')
            if route > remaining:
                candidate_reasons.append(f'Planned route exceeds remaining configured range budget ({remaining:.2f} km).')
            zone_hits = _restricted_zone_hits(delivery, zones, restrictions)
            if zone_hits:
                candidate_reasons.append('Direct origin-destination planning segment intersects restricted virtual zone(s): ' + ', '.join(zone_hits))
            if _blocked_corridor_hits(delivery, corridors, blocked):
                candidate_reasons.append('Direct planning segment intersects an unavailable virtual corridor.')
            if delivery.get('forced_violation'):
                candidate_reasons.append(str(delivery['forced_violation']))
            reposition = _haversine_km(drone['position'], delivery['pickup']) if drone['position'] else None
            duration = ((route + (reposition or 0)) / speed) * 60
            deadline = _parse_time(delivery.get('deadline'))
            if deadline and now + timedelta(minutes=duration) > deadline:
                candidate_reasons.append('Estimated arrival misses the delivery deadline.')
            if candidate_reasons:
                reasons.extend(f'{drone_id}: {reason}' for reason in candidate_reasons)
                continue
            balance_penalty = workload[drone_id]['planned_distance_km'] * 0.25 if name == 'optimized' else 0.0
            score = (reposition or 0) if name == 'nearest_eligible' else route + (reposition or 0) + balance_penalty
            candidates.append((score, drone_id, reposition or 0))
        if not candidates:
            message = '; '.join(dict.fromkeys(reasons)) or 'No drone satisfies all configured constraints.'
            unassigned.append({'delivery_id': delivery['delivery_id'], 'reason': message})
            violations.append({'delivery_id': delivery['delivery_id'], 'reasons': list(dict.fromkeys(reasons)) or [message]})
            continue
        _, drone_id, reposition = min(candidates, key=lambda item: (item[0], item[1]))
        busy.add(drone_id)
        workload[drone_id]['delivery_count'] += 1
        workload[drone_id]['planned_distance_km'] += route
        total_planned += route
        total_estimated += route + reposition
        assignments.append(_assignment(
            delivery,
            drone_id,
            route,
            reposition,
            route_method,
            speed,
            now,
            f'{name.replace("_", " ").title()} eligible drone; estimated reposition {reposition:.2f} km and delivery route {route:.2f} km.',
        ))
    return _strategy_result(name, assignments, unassigned, violations, total_planned, total_estimated, workload)


def _assignment(delivery, drone_id, route, reposition, method, speed, now, rationale):
    duration = ((route + reposition) / speed) * 60
    deadline = _parse_time(delivery.get('deadline'))
    feasible = not deadline or now + timedelta(minutes=duration) <= deadline
    return {
        'delivery_id': delivery['delivery_id'],
        'drone_id': drone_id,
        'priority': delivery['priority'],
        'estimated_route_distance_km': round(route, 3),
        'route_distance_method': method,
        'estimated_reposition_distance_km': round(reposition, 3),
        'estimated_total_distance_km': round(route + reposition, 3),
        'estimated_duration_minutes': round(duration, 1),
        'deadline': delivery.get('deadline'),
        'deadline_feasible': feasible,
        'rationale': rationale,
    }


def _strategy_result(name, assignments, unassigned, violations, planned, estimated, workload):
    return {
        'strategy': name,
        'assignments': assignments,
        'unassigned': unassigned,
        'constraint_violations': violations,
        'total_planned_distance_km': round(planned, 3),
        'estimated_total_distance_km': round(estimated, 3),
        'workload_distribution': {
            drone_id: {
                'delivery_count': stats['delivery_count'],
                'planned_distance_km': round(stats['planned_distance_km'], 3),
            }
            for drone_id, stats in sorted(workload.items())
        },
    }


def _route_km(delivery):
    planned = delivery.get('planned_route_km')
    if isinstance(planned, (int, float)) and math.isfinite(planned) and planned > 0:
        return float(planned), 'persisted_planned_route'
    straight = _haversine_km(delivery.get('pickup'), delivery.get('destination'))
    if straight is None:
        return 0.0, 'unavailable'
    return straight * 1.3, 'straight_line_estimate_x1.3'


def _restricted_zone_hits(delivery, zones, restrictions):
    start, end = delivery.get('pickup'), delivery.get('destination')
    hits = []
    for restriction in restrictions:
        zone = zones.get(restriction.get('zone_id'))
        if not zone or not start or not end or not zone.get('center'):
            continue
        if _segment_hits_circle(start, end, zone['center'], float(zone.get('radius_m', 0)) / 1000):
            hits.append(zone['id'])
    return hits


def _blocked_corridor_hits(delivery, corridors, blocked):
    start, end = delivery.get('pickup'), delivery.get('destination')
    if not start or not end:
        return []
    hits = []
    for corridor_id in sorted(blocked):
        corridor = corridors.get(corridor_id)
        if corridor and any(
            _segments_intersect(start, end, first, second)
            for first, second in zip(corridor.get('path', []), corridor.get('path', [])[1:])
        ):
            hits.append(corridor_id)
    return hits


def _segment_hits_circle(start, end, center, radius_km):
    origin = start['lat']
    scale_x = math.cos(math.radians(origin))
    ax, ay = 0.0, 0.0
    bx = (end['lng'] - start['lng']) * 111.195 * scale_x
    by = (end['lat'] - start['lat']) * 111.195
    cx = (center['lng'] - start['lng']) * 111.195 * scale_x
    cy = (center['lat'] - start['lat']) * 111.195
    denom = bx * bx + by * by
    t = 0 if denom == 0 else max(0.0, min(1.0, (cx * bx + cy * by) / denom))
    return math.hypot(cx - t * bx, cy - t * by) <= radius_km


def _segments_intersect(a, b, c, d):
    def orient(p, q, r):
        return (q['lng'] - p['lng']) * (r['lat'] - p['lat']) - (q['lat'] - p['lat']) * (r['lng'] - p['lng'])
    def on_segment(p, q, r):
        return (
            min(p['lng'], r['lng']) <= q['lng'] <= max(p['lng'], r['lng'])
            and min(p['lat'], r['lat']) <= q['lat'] <= max(p['lat'], r['lat'])
        )
    first, second = orient(a, b, c), orient(a, b, d)
    third, fourth = orient(c, d, a), orient(c, d, b)
    if first * second < 0 and third * fourth < 0:
        return True
    return (
        (first == 0 and on_segment(a, c, b))
        or (second == 0 and on_segment(a, d, b))
        or (third == 0 and on_segment(c, a, d))
        or (fourth == 0 and on_segment(c, b, d))
    )


def _haversine_km(first, second):
    if not first or not second:
        return None
    lat1, lon1 = math.radians(float(first['lat'])), math.radians(float(first['lng']))
    lat2, lon2 = math.radians(float(second['lat'])), math.radians(float(second['lng']))
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _position(latitude, longitude):
    return {'lat': latitude, 'lng': longitude} if latitude is not None and longitude is not None else None


def _json_object(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def _positive_setting(name, default):
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f'{name} must be a finite positive number.') from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a finite positive number.')
    return value

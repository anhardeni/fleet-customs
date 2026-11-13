# Copyright (c) 2026, anhar91 and contributors
# For license information, please see license.txt

import math

EARTH_RADIUS = 6371000.0  # Meter

def haversine(lat1, lon1, lat2, lon2):
	"""Menghitung jarak geospasial terpendek antara dua koordinat (Great Circle Distance)."""
	phi1, phi2 = math.radians(lat1), math.radians(lat2)
	dphi = math.radians(lat2 - lat1)
	dlam = math.radians(lon2 - lon1)
	a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0)**2
	return 2.0 * EARTH_RADIUS * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def distance_to_segment(p_lat, p_lng, a_lat, a_lng, b_lat, b_lng):
	"""Jarak terpendek titik P ke segmen garis tertutup AB (Clamped factor t in [0,1]).
	Menggunakan proyeksi ekuirektangular lokal berkinerja tinggi tanpa dependensi PostGIS.
	"""
	x = math.radians(b_lng - a_lng) * math.cos(math.radians((a_lat + b_lat) / 2.0))
	y = math.radians(b_lat - a_lat)
	seg_len_sq = x * x + y * y

	if seg_len_sq == 0.0:
		return haversine(p_lat, p_lng, a_lat, a_lng)

	px = math.radians(p_lng - a_lng) * math.cos(math.radians((a_lat + p_lat) / 2.0))
	py = math.radians(p_lat - a_lat)
	t = max(0.0, min(1.0, (px * x + py * y) / seg_len_sq))

	proj_lat = a_lat + t * (b_lat - a_lat)
	proj_lng = a_lng + t * (b_lng - a_lng)
	return haversine(p_lat, p_lng, proj_lat, proj_lng)

def verify_corridor(lat, lng, route_polyline, tolerance_meters=300.0):
	"""Verifikasi apakah titik koordinat berada di dalam koridor rute pabean sah.
	Tahap 1: Bounding Box Pre-Check (Eliminasi cepat untuk titik di luar jangkauan umum).
	Tahap 2: Segment Iteration (Kalkulasi presisi titik ke tiap segmen polylines).
	"""
	if not route_polyline or len(route_polyline) < 2:
		return True

	# 1. Bounding Box Pre-Check
	lats = [p[0] for p in route_polyline]
	lngs = [p[1] for p in route_polyline]
	lat_pad = tolerance_meters / 111000.0
	lng_pad = tolerance_meters / (111000.0 * math.cos(math.radians(lat)))

	min_lat, max_lat = min(lats) - lat_pad, max(lats) + lat_pad
	min_lng, max_lng = min(lngs) - lng_pad, max(lngs) + lng_pad

	if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
		return False

	# 2. Segment Iteration
	min_dist = float("inf")
	for i in range(len(route_polyline) - 1):
		d = distance_to_segment(
			lat, lng,
			route_polyline[i][0], route_polyline[i][1],
			route_polyline[i + 1][0], route_polyline[i + 1][1]
		)
		if d < min_dist:
			min_dist = d
		if min_dist <= tolerance_meters:
			return True

	return min_dist <= tolerance_meters


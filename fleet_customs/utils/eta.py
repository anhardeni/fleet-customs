# Copyright (c) 2026, anhar91 and contributors
# For license information, please see license.txt

import json
from datetime import timedelta
import frappe
from frappe.utils import now_datetime, get_datetime
from fleet_customs.utils.spatial import haversine

def calculate_trip_eta(trip_id):
	"""Menghitung estimasi waktu tiba (ETA) secara dinamis berbasis:
	1. Jarak sisa (Haversine) armada ke geofence tujuan pabean.
	2. Kecepatan rata-rata 30 menit terakhir dari tabTruck GPS Log.
	3. Membandingkan hasil dengan batas SLA kepabeanan (sla_expiry_datetime).
	"""
	if not frappe.db.exists("Truck Trip Delivery", trip_id):
		return None

	trip = frappe.get_doc("Truck Trip Delivery", trip_id)
	if trip.route_status in ("Closed", "Arrived", "Draft"):
		return None

	current_lat = trip.current_latitude or 0.0
	current_lng = trip.current_longitude or 0.0

	# Cari jarak ke tujuan terdekat dari daftar kontainer
	min_remaining_dist = float("inf")
	for c in trip.containers:
		if c.dest_lat and c.dest_lng:
			dist = haversine(current_lat, current_lng, c.dest_lat, c.dest_lng)
			if dist < min_remaining_dist:
				min_remaining_dist = dist

	if min_remaining_dist == float("inf"):
		return None

	# Ambil rata-rata kecepatan armada 30 menit terakhir dari log GPS
	avg_speed = frappe.db.sql("""
		SELECT AVG(speed) FROM `tabTruck GPS Log`
		WHERE trip = %s AND timestamp >= DATE_SUB(NOW(), INTERVAL 30 MINUTE)
		  AND speed > 0
	""", (trip_id,))[0][0] or 0.0

	now = now_datetime()

	# Jika kecepatan kendaraan sangat rendah (< 5 km/h), asumsikan kecepatan moderat tol 40 km/h untuk estimasi
	effective_speed = float(avg_speed) if float(avg_speed) >= 5.0 else 40.0

	# Jarak dalam km / kecepatan dalam km/jam = durasi dalam jam
	dist_km = min_remaining_dist / 1000.0
	eta_hours = dist_km / effective_speed
	eta_datetime = now + timedelta(hours=eta_hours)

	# Tentukan status ETA vs SLA pabean
	sla_expiry = trip.sla_expiry_datetime
	if not sla_expiry:
		eta_status = "On Time"
	else:
		sla_expiry_dt = get_datetime(sla_expiry)
		if eta_datetime > sla_expiry_dt:
			eta_status = "Will Be Late"
		elif (sla_expiry_dt - eta_datetime).total_seconds() < 1800:  # < 30 menit
			eta_status = "Tight"
		else:
			eta_status = "On Time"

	# Simpan hasil perhitungan ke database master trip
	frappe.db.set_value("Truck Trip Delivery", trip_id, {
		"eta_datetime": eta_datetime,
		"eta_status": eta_status
	}, update_modified=False)
	frappe.db.commit()

	return {
		"trip_id": trip_id,
		"remaining_distance_km": round(dist_km, 2),
		"effective_speed_kmh": round(effective_speed, 1),
		"eta_datetime": str(eta_datetime),
		"eta_status": eta_status
	}


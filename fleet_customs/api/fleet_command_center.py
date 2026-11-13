# Copyright (c) 2026, anhar91 and contributors
# For license information, please see license.txt

import json
import frappe
from frappe import _

@frappe.whitelist()
def get_initial_fleet_state():
	"""Mengambil state awal seluruh armada aktif untuk Command Center (Hanya dipanggil saat Page Load)."""
	trips = frappe.get_all("Truck Trip Delivery",
		filters={"route_status": ["!=", "Closed"]},
		fields=[
			"name", "truck", "customs_doc", "nomor_aju", "bc_type", "bc_number",
			"route_status", "workflow_state", "current_latitude", "current_longitude",
			"seal_status", "eta_status", "departure_time", "sla_expiry_datetime",
			"has_breakdown", "route_corridor"
		]
	)

	fleet = []
	counts = {"total": 0, "safe": 0, "warning": 0, "breach": 0, "breakdown": 0, "emergency": 0}

	for t in trips:
		counts["total"] += 1
		risk_level = "SAFE"
		if t.route_status == "Emergency":
			risk_level = "EMERGENCY"
			counts["emergency"] += 1
		elif t.route_status == "Off Route / Alert" or t.seal_status == "Tampered":
			risk_level = "BREACH"
			counts["breach"] += 1
		elif t.has_breakdown or t.route_status == "Breakdown / Stopped":
			risk_level = "BREAKDOWN"
			counts["breakdown"] += 1
		elif t.eta_status in ("Tight", "Will Be Late") or t.workflow_state == "Inspection Required":
			risk_level = "WARNING"
			counts["warning"] += 1
		else:
			counts["safe"] += 1

		# Ambil data kontainer dan segel
		containers = frappe.get_all("Truck Trip Container Item",
			filters={"parent": t.name},
			fields=["container_no", "container_size", "eseal", "seal_status", "seal_battery"]
		)

		fleet.append({
			"trip_id": t.name,
			"truck_plate": t.truck,
			"customs_doc": t.customs_doc,
			"nomor_aju": t.nomor_aju,
			"bc_type": t.bc_type,
			"bc_number": t.bc_number,
			"route_status": t.route_status,
			"workflow_state": t.workflow_state,
			"lat": t.current_latitude or -6.21453,
			"lng": t.current_longitude or 106.98542,
			"seal_status": t.seal_status,
			"risk_level": risk_level,
			"eta_status": t.eta_status or "On Time",
			"route_corridor": t.route_corridor,
			"containers": containers
		})

	can_dispatch = "Customs Inspector" in frappe.get_roles() or "System Manager" in frappe.get_roles()

	return {
		"fleet": fleet,
		"hud": counts,
		"user_permissions": {
			"can_dispatch_p2": can_dispatch,
			"can_approve_unseal": can_dispatch
		}
	}

@frappe.whitelist()
def get_trip_track(trip_id):
	"""Mengambil koordinat historis dari tabTruck GPS Log untuk fitur Trip Timeline Replay."""
	logs = frappe.db.sql("""
		SELECT latitude, longitude, speed, heading, battery, timestamp
		FROM `tabTruck GPS Log`
		WHERE trip = %s
		ORDER BY timestamp ASC
		LIMIT 1000
	""", (trip_id,), as_dict=True)

	return logs

@frappe.whitelist()
def get_incident_heatmap():
	"""Mengambil akumulasi titik insiden tamper untuk visualisasi heatmap titik rawan kriminal."""
	points = frappe.db.sql("""
		SELECT latitude, longitude, COUNT(*) as intensity
		FROM `tabCustoms Tamper Log`
		GROUP BY ROUND(latitude, 3), ROUND(longitude, 3)
		LIMIT 500
	""", as_dict=True)

	# Format Leaflet.heat: [[lat, lng, intensity], ...]
	return [[float(p.latitude), float(p.longitude), min(1.0, float(p.intensity) * 0.2)] for p in points if p.latitude and p.longitude]

@frappe.whitelist()
def get_corridor_geometry(corridor_name):
	"""Mengambil koordinat polyline koridor rute sah untuk Corridor Visualizer."""
	if not frappe.db.exists("Customs Route Corridor", corridor_name):
		return None
	c = frappe.get_doc("Customs Route Corridor", corridor_name)
	try:
		polyline = json.loads(c.route_polyline_json or "[]")
	except Exception:
		polyline = []
	return {
		"corridor_name": c.name,
		"tolerance_meters": c.tolerance_meters or 300,
		"polyline": polyline
	}

@frappe.whitelist()
def dispatch_p2_action(trip_id):
	"""Aksi darurat: Mobilisasi Tim Penindakan (P2) Bea Cukai ke lokasi armada."""
	if not ("Customs Inspector" in frappe.get_roles() or "System Manager" in frappe.get_roles()):
		frappe.throw(_("Hanya Pejabat Bea Cukai yang berwenang mengirim Tim P2."))

	trip = frappe.get_doc("Truck Trip Delivery", trip_id)
	trip.route_status = "Off Route / Alert"
	trip.workflow_state = "Inspection Required"
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.publish_realtime("customs_p2_dispatched", {
		"trip_id": trip_id,
		"truck": trip.truck,
		"message": _("Tim Penindakan dan Penyidikan (P2) resmi dimobilisasi ke posisi armada!")
	})

	return {"status": "SUCCESS", "message": _("Instruksi penindakan P2 berhasil dikirimkan.")}


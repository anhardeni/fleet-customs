# Copyright (c) 2026, anhar91 and contributors
# For license information, please see license.txt

import json
import hmac
import hashlib
import requests
import frappe
from frappe import _
from frappe.utils import now_datetime

@frappe.whitelist()
def dispatch_trip_to_customs(trip_id):
	"""Mengirim outbound payload Dispatch & Trip Pairing ke SKP DJBC CEISA
	Sesuai kontrak KEP-97/BC/2025: POST /api/v1/h2h/customs/dispatch
	"""
	if not frappe.db.exists("Truck Trip Delivery", trip_id):
		frappe.throw(_("Trip {0} tidak ditemukan").format(trip_id))

	trip = frappe.get_doc("Truck Trip Delivery", trip_id)

	# Format Nomor Aju standar CEISA 4.0 (26 karakter tanpa spasi/tanda hubung)
	nomor_aju_clean = (trip.nomor_aju or "").replace("-", "").replace(" ", "")

	consignments = []
	for idx, c in enumerate(trip.containers, start=1):
		consignments.append({
			"itemIndex": idx,
			"containerNumber": c.container_no,
			"containerSize": c.container_size,
			"sealId": c.eseal,
			"customsDeclaration": {
				"nomorAju": nomor_aju_clean,
				"bcType": trip.bc_type or "BC 2.3",
				"bcNumber": trip.bc_number or ""
			},
			"destination": {
				"destinationCode": c.destination_code or "DEST-001",
				"latitude": c.dest_lat or 0.0,
				"longitude": c.dest_lng or 0.0,
				"geofenceRadiusMeter": int(c.dest_radius_meter or 250)
			}
		})

	request_id = f"REQ-{trip.name}-{frappe.generate_hash(length=6).upper()}"
	timestamp_iso = now_datetime().isoformat()

	payload = {
		"header": {
			"requestId": request_id,
			"timestamp": timestamp_iso
		},
		"transport": {
			"truckPlate": trip.truck,
			"truckGpsId": trip.truck_gps_id or "GPS-TRK-001",
			"driverName": trip.driver_name or "Driver Transporter",
			"transporterCompany": trip.transporter_company or ""
		},
		"sla": {
			"maxAllowedHours": float(trip.max_allowed_hours or 6.0),
			"slaExpiryTime": trip.sla_expiry_datetime.isoformat() if trip.sla_expiry_datetime else ""
		},
		"consignments": consignments
	}

	# Hitung signature SHA-256
	raw_json = json.dumps(payload, separators=(',', ':'))
	h2h_key = b"CEISA_H2H_INTEGRATION_KEY_2026"
	signature = hmac.new(h2h_key, raw_json.encode('utf-8'), hashlib.sha256).hexdigest()
	payload["header"]["signature"] = signature

	# Simpan singlecore_ref_id untuk jejak audit H2H
	trip.singlecore_ref_id = request_id
	trip.workflow_state = "In Transit"
	trip.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "SUCCESS",
		"message": _("Outbound H2H Payload berhasil disiapkan & dipairing."),
		"requestId": request_id,
		"signature": signature,
		"payload": payload
	}


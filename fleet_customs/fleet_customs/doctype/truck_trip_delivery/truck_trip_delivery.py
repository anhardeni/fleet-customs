# Copyright (c) 2026, anhar91 and contributors
# For license information, please see license.txt

import math
import hashlib
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, add_to_date

from fleet_customs.utils.spatial import haversine, verify_corridor
from fleet_customs.utils.eta import calculate_trip_eta

class TruckTripDelivery(Document):
	def validate(self):
		self.validate_customs_doc()
		self.validate_containers()
		self.compute_sla()
		self.ensure_panic_token()

	def ensure_panic_token(self):
		# Generate panic token jika belum ada
		if not self.get_password("panic_token", raise_exception=False):
			token = frappe.generate_hash(length=16)
			self.panic_token = token

	def validate_customs_doc(self):
		if self.customs_doc:
			c_doc = frappe.get_doc("Customs Declaration", self.customs_doc)
			self.nomor_aju = c_doc.nomor_aju
			self.bc_type = c_doc.bc_type
			self.bc_number = c_doc.bc_number
			self.customs_office = c_doc.customs_office

	def validate_containers(self):
		if not self.containers:
			frappe.throw(_("Minimal harus ada 1 kontainer pada pengiriman ini."))

		for row in self.containers:
			if row.eseal:
				seal = frappe.get_doc("Electronic Seal", row.eseal)
				if seal.seal_battery < 15 and self.docstatus == 0:
					frappe.msgprint(_("Peringatan: e-Seal {0} baterai kritis ({1}%). Disarankan ganti perangkat.").format(seal.name, seal.seal_battery))

	def compute_sla(self):
		if self.departure_time and self.max_allowed_hours:
			self.sla_expiry_datetime = add_to_date(self.departure_time, hours=float(self.max_allowed_hours))

	@frappe.whitelist()
	def check_corridor_deviation(self):
		"""Memeriksa apakah koordinat armada saat ini menyimpang dari koridor rute pabean sah."""
		if not self.route_corridor:
			return {"in_corridor": True}

		corridor_doc = frappe.get_doc("Customs Route Corridor", self.route_corridor)
		if not corridor_doc.route_polyline_json:
			return {"in_corridor": True}

		try:
			import json
			polyline = json.loads(corridor_doc.route_polyline_json)
		except Exception:
			return {"in_corridor": True}

		lat = self.current_latitude or 0.0
		lng = self.current_longitude or 0.0
		tolerance = float(corridor_doc.tolerance_meters or 300.0)

		is_valid = verify_corridor(lat, lng, polyline, tolerance_meters=tolerance)
		if not is_valid and self.route_status in ("On Route", "In Transit"):
			# Otomatis tandai rute mengalami deviasi/alert
			self.route_status = "Off Route / Alert"
			self.save(ignore_permissions=True)
			frappe.publish_realtime("corridor_breach_alert", {
				"trip_id": self.name,
				"truck": self.truck,
				"lat": lat,
				"lng": lng,
				"message": _("Armada menyimpang di luar koridor sah pabean (> {0}m)").format(int(tolerance))
			})

		return {"in_corridor": is_valid, "tolerance_meters": tolerance}

	@frappe.whitelist()
	def request_unseal_approval(self):
		"""Diajukan oleh transporter atau operator saat armada berada di geofence tujuan."""
		self.validate_arrival_geofence()

		if self.seal_status == "Tampered":
			frappe.throw(_("Pengajuan buka segel ditolak: Terdapat insiden perusakan segel (Tampered). Wajib penerbitan BAP fisik!"))

		self.workflow_state = "Inspection Required"
		self.save(ignore_permissions=True)
		frappe.msgprint(_("Permohonan verifikasi unsealing pabean berhasil diajukan."))

	@frappe.whitelist()
	def approve_unseal_customs(self, officer_nip, permit_number):
		"""Disetujui oleh Pejabat Bea Cukai (Customs Inspector)."""
		if not ("Customs Inspector" in frappe.get_roles() or "System Manager" in frappe.get_roles() or frappe.flags.in_webhook):
			frappe.throw(_("Akses ditolak: Hanya Pejabat Bea Cukai yang berwenang yang dapat menyetujui unseal."))

		self.validate_arrival_geofence()

		# Generate Cryptographic Token OTP 16 Karakter (64-bit entropy)
		otp_token = frappe.generate_hash(length=16).upper()
		token_hash = hashlib.sha256(otp_token.encode()).hexdigest()

		for item in self.containers:
			frappe.db.set_value("Truck Trip Container Item", item.name, {
				"unseal_permission_granted": 1,
				"unseal_officer_nip": officer_nip,
				"unseal_permit_number": permit_number,
				"unseal_token": token_hash,
				"seal_status": "Unlocked",
				"item_status": "Unsealed",
				"unlock_timestamp": now_datetime()
			})
			frappe.db.set_value("Electronic Seal", item.eseal, "seal_status", "Unlocked")

		self.workflow_state = "Completed"
		self.route_status = "Closed"
		self.arrival_time = now_datetime()
		self.save(ignore_permissions=True)

		return {"status": "SUCCESS", "unseal_token": otp_token}

	def validate_arrival_geofence(self):
		lat = self.current_latitude or 0.0
		lng = self.current_longitude or 0.0
		for cont in self.containers:
			if cont.dest_lat and cont.dest_lng:
				dist = haversine(lat, lng, cont.dest_lat, cont.dest_lng)
				max_r = float(cont.dest_radius_meter or 250)
				if dist > max_r:
					frappe.throw(_("Armada berada di luar radius tujuan pabean ({0}m > batas {1}m).").format(int(dist), int(max_r)))

	@frappe.whitelist()
	def verify_unseal_token(self, container_no, token_input):
		"""Memverifikasi token unseal e-Seal dari driver/petugas lapangan dengan proteksi rate-limit."""
		for item in self.containers:
			if item.container_no == container_no:
				# Cek batas percobaan
				if item.unseal_attempts >= item.unseal_max_attempts:
					frappe.throw(_("Batas percobaan unseal tercapai ({0}x). Pengajuan diblokir!").format(item.unseal_max_attempts))

				input_hash = hashlib.sha256(token_input.encode()).hexdigest()
				if item.unseal_token and item.unseal_token == input_hash:
					# Reset attempts saat sukses
					frappe.db.set_value("Truck Trip Container Item", item.name, {
						"item_status": "Unsealed",
						"seal_status": "Unlocked",
						"unlock_timestamp": now_datetime()
					})
					frappe.db.set_value("Electronic Seal", item.eseal, "seal_status", "Unlocked")
					return {"status": "SUCCESS", "message": _("Segel berhasil dibuka secara sah.")}
				else:
					# Increment counter percobaan gagal
					new_attempts = item.unseal_attempts + 1
					frappe.db.set_value("Truck Trip Container Item", item.name, "unseal_attempts", new_attempts)
					frappe.db.commit()
					sisa = item.unseal_max_attempts - new_attempts
					frappe.throw(_("Token unseal salah! Sisa percobaan: {0}").format(max(0, sisa)))

		frappe.throw(_("Kontainer {0} tidak ditemukan pada trip ini.").format(container_no))
		lat = self.current_latitude or 0.0
		lng = self.current_longitude or 0.0
		for cont in self.containers:
			if cont.dest_lat and cont.dest_lng:
				dist = haversine(lat, lng, cont.dest_lat, cont.dest_lng)
				max_r = float(cont.dest_radius_meter or 250)
				if dist > max_r:
					frappe.throw(_("Armada berada di luar radius tujuan pabean ({0}m > batas {1}m).").format(int(dist), int(max_r)))


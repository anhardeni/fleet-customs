import json
import frappe
from frappe import _
from frappe.utils import now_datetime

@frappe.whitelist(allow_guest=True)
def trigger_panic():
	"""Endpoint tombol darurat driver (Driver Panic Button).
	Menerima POST body JSON untuk mencegah token bocor di server access log / referer header.
	"""
	if frappe.request.method != "POST":
		frappe.throw(_("Hanya menerima request HTTP POST"), frappe.PermissionError)

	try:
		data = json.loads(frappe.request.data or "{}")
	except Exception:
		data = frappe.form_dict

	trip_id = data.get("trip_id")
	token = data.get("token")
	driver_lat = data.get("lat")
	driver_lng = data.get("lng")

	if not trip_id or not token:
		frappe.throw(_("Parameter trip_id dan token wajib disertakan"), frappe.ValidationError)

	if not frappe.db.exists("Truck Trip Delivery", trip_id):
		frappe.throw(_("Trip {0} tidak ditemukan").format(trip_id), frappe.DoesNotExistError)

	trip_doc = frappe.get_doc("Truck Trip Delivery", trip_id)

	# [FIX DOC PRIMER] Gunakan get_password untuk dekripsi Fernet dari tabel __Auth
	stored_token = trip_doc.get_password("panic_token")
	if not stored_token or stored_token != token:
		frappe.throw(_("Token darurat tidak valid atau sudah kedaluwarsa"), frappe.AuthenticationError)

	# Ubah status rute menjadi Emergency
	trip_doc.route_status = "Emergency"
	if driver_lat and driver_lng:
		trip_doc.current_latitude = float(driver_lat)
		trip_doc.current_longitude = float(driver_lng)
	trip_doc.save(ignore_permissions=True)
	frappe.db.commit()

	# Push alert WebSocket real-time ke Command Center
	frappe.publish_realtime(
		event="driver_panic_alert",
		message={
			"trip_id": trip_id,
			"truck": trip_doc.truck,
			"driver_name": trip_doc.driver_name,
			"driver_phone": trip_doc.driver_phone,
			"lat": trip_doc.current_latitude,
			"lng": trip_doc.current_longitude,
			"timestamp": str(now_datetime())
		}
	)

	return {
		"status": "SUCCESS",
		"message": _("Sinyal darurat berhasil diterima. Pusat Komando Pabean & Tim Pengawas telah dimobilisasi.")
	}


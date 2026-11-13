import json
import time
import socket
import os
import redis
import frappe
from frappe.utils import now_datetime

def get_iot_redis():
	return redis.Redis(host="127.0.0.1", port=11001, decode_responses=True)

def init_tamper_group():
	r = get_iot_redis()
	try:
		r.xgroup_create("stream:tamper", "grp_tamper", id="0", mkstream=True)
	except redis.exceptions.ResponseError:
		pass

def run_reliable_priority_consumer():
	r = get_iot_redis()
	init_tamper_group()

	# Nama consumer stabil berbasis host & PID
	consumer_name = f"tamper-{socket.gethostname()}-{os.getpid()}"
	print(f"[{now_datetime()}] Priority Tamper Worker aktif: {consumer_name}")

	while True:
		try:
			# Watchdog Heartbeat
			r.set("heartbeat:tamper_worker", str(now_datetime()), ex=30)

			# Baca 1 pesan instan tanpa delay
			entries = r.xreadgroup(
				"grp_tamper", consumer_name,
				{"stream:tamper": ">"}, count=1, block=2000
			)

			if not entries:
				continue

			msg_id, data = entries[0][1][0]
			payload = json.loads(data["payload"])

			success = process_tamper_event(r, payload)
			if success:
				r.xack("stream:tamper", "grp_tamper", msg_id)
			else:
				# Masukkan ke Dead-Letter Queue jika transaksi DB gagal
				r.xadd("stream:tamper_dlq", {"payload": data["payload"], "error_at": str(now_datetime())})
				r.xack("stream:tamper", "grp_tamper", msg_id)

		except Exception as e:
			frappe.log_error(title="Tamper Worker Exception", message=str(e))
			time.sleep(1)

def process_tamper_event(r, data):
	seal_id = data.get("sealId")
	trip_id = data.get("tripId")
	lat = float(data.get("lat", 0.0))
	lng = float(data.get("lng", 0.0))
	tamper_type = data.get("tamperType") or "WIRE_LOOP_CUT"
	now = now_datetime()

	try:
		# A. AUDIT PERMANEN (DATA LAYER): Catat SEMUA raw trigger ke Customs Tamper Log
		tamper_log = frappe.get_doc({
			"doctype": "Customs Tamper Log",
			"trip": trip_id,
			"seal_id": seal_id,
			"latitude": lat,
			"longitude": lng,
			"alert_type": tamper_type,
			"timestamp": now
		})
		tamper_log.insert(ignore_permissions=True)
		frappe.db.commit()

		# B. ACTION DEBOUNCING (ESCALATION LAYER):
		# Mencegah badai alarm. Alarm hanya dipicu 1 KALI dalam jendela 10 menit per trip.
		debounce_key = f"action_tamper_lock:{trip_id}"
		should_escalate = r.set(debounce_key, "1", ex=600, nx=True)

		if should_escalate:
			# [FIX B1] Perbarui status trip & seal secara presisi tanpa query kolom invalid
			frappe.db.sql("""
				UPDATE `tabTruck Trip Delivery`
				SET route_status = 'Off Route / Alert', seal_status = 'Tampered'
				WHERE name = %s
			""", (trip_id,))

			frappe.db.sql("""
				UPDATE `tabElectronic Seal`
				SET seal_status = 'Tampered', last_telemetry = %s
				WHERE name = %s
			""", (now, seal_id))

			# Update juga child table per kontainer yang membawa segel terkait
			frappe.db.sql("""
				UPDATE `tabTruck Trip Container Item`
				SET seal_status = 'Tampered', item_status = 'Tampered'
				WHERE parent = %s AND eseal = %s
			""", (trip_id, seal_id))

			frappe.db.commit()

			# Push Delta Realtime WebSocket
			frappe.publish_realtime(
				event="fleet_delta_update",
				message={
					"trip_id": trip_id,
					"seal_id": seal_id,
					"lat": lat,
					"lng": lng,
					"risk_level": "BREACH",
					"seal_status": "Tampered",
					"incident": tamper_type,
					"timestamp": str(now)
				}
			)

		return True
	except Exception as err:
		frappe.db.rollback()
		frappe.log_error(title="Tamper Processing Error", message=str(err))
		return False

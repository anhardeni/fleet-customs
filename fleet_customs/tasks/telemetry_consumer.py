import os
import json
import time
import socket
import redis
import pymysql
import frappe
from frappe.utils import now_datetime, get_datetime

def get_iot_redis():
	return redis.Redis(host="127.0.0.1", port=11001, decode_responses=True)

def init_consumer_group():
	r = get_iot_redis()
	try:
		r.xgroup_create("stream:telemetry", "grp_telemetry", id="0", mkstream=True)
	except redis.exceptions.ResponseError:
		pass

def run_telemetry_batch_consumer():
	r = get_iot_redis()
	init_consumer_group()

	# [FIX A6] Nama consumer stabil berbasis hostname & PID
	consumer_name = f"telemetry-{socket.gethostname()}-{os.getpid()}"
	print(f"[{now_datetime()}] Telemetry Consumer aktif: {consumer_name}")

	last_autoclaim_time = time.time()

	while True:
		try:
			# Watchdog Heartbeat
			r.set("heartbeat:telemetry_consumer", str(now_datetime()), ex=30)

			# [FIX A6] XAUTOCLAIM setiap 60 detik untuk memulihkan pesan terbengkalai (orphaned PEL)
			if time.time() - last_autoclaim_time > 60:
				try:
					# Klaim pesan yang idle > 60000ms (60s) dari worker yang crash
					next_id, claimed_entries, _ = r.xautoclaim(
						"stream:telemetry", "grp_telemetry", consumer_name,
						min_idle_time=60000, start_id="0-0", count=50
					)
					if claimed_entries:
						print(f"[{now_datetime()}] Memulihkan {len(claimed_entries)} pesan PEL terbengkalai.")
						process_entries(r, claimed_entries)
				except Exception as claim_err:
					print("Error running xautoclaim:", claim_err)
				last_autoclaim_time = time.time()

			# Baca batch hingga 200 event baru per iterasi dengan blocking 1000ms
			entries = r.xreadgroup(
				"grp_telemetry", consumer_name,
				{"stream:telemetry": ">"}, count=200, block=1000
			)

			if not entries:
				continue

			process_entries(r, entries[0][1])

		except Exception as e:
			frappe.log_error(title="Telemetry Consumer Crash", message=str(e))
			time.sleep(2)

def process_entries(r, stream_data):
	raw_packets = []
	msg_ids = []

	for msg_id, data in stream_data:
		msg_ids.append(msg_id)
		payload_str = data.get("payload")
		if payload_str:
			raw_packets.append(json.loads(payload_str))

	success = process_telemetry_batch(raw_packets)

	if success and msg_ids:
		r.xack("stream:telemetry", "grp_telemetry", *msg_ids)
	else:
		time.sleep(0.5)

def process_telemetry_batch(packets):
	if not packets:
		return True

	logs_to_insert = []
	latest_seals = {}
	latest_trucks = {}
	latest_trips = {}

	for p in packets:
		lat = float(p.get("lat", 0.0))
		lng = float(p.get("lng", 0.0))
		speed = float(p.get("speed", 0.0))
		heading = float(p.get("heading", 0.0))
		battery = int(p.get("battery", 100))
		
		ts_raw = p.get("deviceTimestamp")
		if ts_raw:
			try:
				ts = get_datetime(ts_raw)
			except Exception:
				ts = now_datetime()
		else:
			ts = now_datetime()

		trip_id = p.get("tripId")
		seal_id = p.get("sealId")
		truck_id = p.get("truckId")

		# [FIX B3] Gunakan hash 16 karakter (64-bit entropy) untuk Zero Birthday Collision
		log_pk = frappe.generate_hash(length=16)

		logs_to_insert.append((
			log_pk,
			trip_id,
			seal_id,
			truck_id,
			lat, lng, speed, heading, battery, ts
		))

		if seal_id:
			latest_seals[seal_id] = (lat, lng, battery, ts)
		if truck_id:
			latest_trucks[truck_id] = (lat, lng, ts)
		if trip_id:
			latest_trips[trip_id] = (lat, lng, ts)

	try:
		# 1. APPEND-ONLY BULK INSERT ke tabTruck GPS Log
		if logs_to_insert:
			template = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(logs_to_insert))
			flat = [val for row in logs_to_insert for val in row]
			frappe.db.sql(f"""
				INSERT INTO `tabTruck GPS Log`
				(name, trip, seal_id, truck, latitude, longitude, speed, heading, battery, timestamp)
				VALUES {template}
			""", flat)

		# 2. CURRENT-STATE UPDATE DENGAN DETERMINISTIC SORTING (ZERO DEADLOCK)
		sorted_seals = sorted(latest_seals.items(), key=lambda x: x[0])
		sorted_trucks = sorted(latest_trucks.items(), key=lambda x: x[0])
		sorted_trips = sorted(latest_trips.items(), key=lambda x: x[0])

		for seal_id, (lat, lng, bat, ts) in sorted_seals:
			frappe.db.sql("""
				UPDATE `tabElectronic Seal`
				SET current_latitude = %s, current_longitude = %s, seal_battery = %s, last_telemetry = %s
				WHERE name = %s
			""", (lat, lng, bat, ts, seal_id))

			# Proactive Battery Alert
			if bat < 15:
				frappe.publish_realtime("seal_battery_critical", {"seal_id": seal_id, "battery": bat})

		for truck_id, (lat, lng, ts) in sorted_trucks:
			frappe.db.sql("""
				UPDATE `tabTruck`
				SET current_latitude = %s, current_longitude = %s, last_updated = %s
				WHERE name = %s
			""", (lat, lng, ts, truck_id))

		for trip_id, (lat, lng, ts) in sorted_trips:
			frappe.db.sql("""
				UPDATE `tabTruck Trip Delivery`
				SET current_latitude = %s, current_longitude = %s
				WHERE name = %s
			""", (lat, lng, trip_id))

		frappe.db.commit()
		return True

	except pymysql.err.OperationalError as e:
		frappe.db.rollback()
		if e.args[0] in (1213, 1205):  # Deadlock / Lock Wait Timeout
			time.sleep(0.2)
		return False
	except Exception:
		frappe.db.rollback()
		return False

import redis
import frappe
from frappe.utils import now_datetime, get_datetime

def get_iot_redis():
	return redis.Redis(host="127.0.0.1", port=11001, decode_responses=True)

def check_consumer_health():
	"""Dijalankan periodik via scheduler / cron untuk memantau status worker."""
	try:
		r = get_iot_redis()
		consumers = {
			"Telemetry Batch Consumer": "heartbeat:telemetry_consumer",
			"Priority Tamper Worker": "heartbeat:tamper_worker"
		}

		for name, key in consumers.items():
			last_beat = r.get(key)
			if not last_beat:
				frappe.publish_realtime("system_alert", {
					"component": name,
					"status": "STOPPED",
					"message": f"Worker {name} belum aktif atau tidak mengirim heartbeat."
				})
			else:
				diff = (now_datetime() - get_datetime(last_beat)).total_seconds()
				if diff > 60:
					frappe.publish_realtime("system_alert", {
						"component": name,
						"status": "STALLED",
						"message": f"Worker {name} hang/stalled selama {int(diff)} detik!"
					})
	except Exception as e:
		frappe.log_error(title="Watchdog Error", message=str(e))


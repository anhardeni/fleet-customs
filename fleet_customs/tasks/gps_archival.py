# Copyright (c) 2026, anhar91 and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import now_datetime

def archive_old_gps_logs(days_threshold=30):
	"""Memindahkan data GPS Log yang lebih tua dari threshold hari
	dari tabel operasional `tabTruck GPS Log` ke tabel `tabTruck GPS Log Partitioned`.
	Tujuannya untuk menjaga query table operasional tetap ringan dan responsif.
	"""
	print(f"[{now_datetime()}] Memulai proses archival GPS Log (> {days_threshold} hari)...")

	try:
		# 1. Salin data lawas ke tabel berpartisi
		frappe.db.sql("""
			INSERT IGNORE INTO `tabTruck GPS Log Partitioned`
			(name, trip, seal_id, truck, latitude, longitude, speed, heading, battery, timestamp)
			SELECT name, trip, seal_id, truck, latitude, longitude, speed, heading, battery, timestamp
			FROM `tabTruck GPS Log`
			WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)
		""", (days_threshold,))

		rows_copied = frappe.db.sql("SELECT ROW_COUNT()")[0][0]

		# 2. Hapus data yang sudah aman tersalin di tabel berpartisi
		if rows_copied > 0:
			frappe.db.sql("""
				DELETE FROM `tabTruck GPS Log`
				WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)
			""", (days_threshold,))
			frappe.db.commit()
			print(f"[{now_datetime()}] Berhasil mengarsipkan {rows_copied} baris log GPS.")
		else:
			print(f"[{now_datetime()}] Tidak ada data GPS Log yang memenuhi kriteria arsip.")

		return {"status": "SUCCESS", "archived_rows": rows_copied}

	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title="Failed archiving GPS logs", message=str(e))
		return {"status": "FAILED", "error": str(e)}


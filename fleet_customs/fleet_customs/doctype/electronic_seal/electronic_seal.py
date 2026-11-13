# Copyright (c) 2026, anhar91 and contributors
# For license information, please see license.txt

import redis
import frappe
from frappe.model.document import Document

class ElectronicSeal(Document):
	def on_update(self):
		"""Sinkronisasi kunci PSK HMAC ke Redis IoT Hash agar Go Gateway dapat membaca per-device key."""
		self.sync_key_to_redis()

	def sync_key_to_redis(self):
		plain_key = self.get_password("psk_hmac_key")
		if not plain_key:
			return

		try:
			r = redis.Redis(host="127.0.0.1", port=11001, decode_responses=True)
			r.hset(f"seal_keys:{self.seal_id}", "psk", plain_key)
		except Exception as e:
			frappe.log_error(title="Failed sync seal key to Redis", message=str(e))

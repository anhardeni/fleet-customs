app_name = "fleet_customs"
app_title = "Fleet Customs"
app_publisher = "anhar91"
app_description = "Customs Fleet Management and e-Seal System"
app_email = "anhar91@example.com"
app_license = "mit"

# Scheduler Events
scheduler_events = {
	"cron": {
		"0 6 * * *": [
			"fleet_customs.tasks.daily_digest.send_daily_customs_digest"
		],
		"*/1 * * * *": [
			"fleet_customs.tasks.watchdog.check_consumer_health"
		]
	},
	"monthly": [
		"fleet_customs.tasks.gps_archival.archive_old_gps_logs"
	]
}

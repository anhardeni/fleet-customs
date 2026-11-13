// Copyright (c) 2026, anhar91 and contributors
// For license information, please see license.txt

frappe.pages['fleet_command_center'].on_page_load = function(wrapper) {
    let page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __('Customs Fleet Command Center (KEP-97/BC/2025)'),
        single_column: true
    });
    frappe.fleet_cc = new CustomsCommandCenter(page, wrapper);
};

class CustomsCommandCenter {
    constructor(page, wrapper) {
        this.page = page;
        this.wrapper = $(wrapper);
        this.markers = {};
        this.fleet_cache = [];
        this.current_filter = 'all';
        this.heatLayer = null;
        this.corridorLayers = [];
        this.replayLayers = [];
        this.init_system();
    }

    init_system() {
        // Load Asset Self-Hosted Leaflet & Leaflet.heat
        frappe.require([
            "/assets/fleet_customs/css/leaflet/leaflet.css",
            "/assets/fleet_customs/js/leaflet/leaflet.js",
            "/assets/fleet_customs/js/leaflet/leaflet-heat.js"
        ], () => {
            this.build_layout();
            this.init_leaflet();
            this.fetch_state();
            this.bind_sockets();
            this.bind_ui();
        });
    }

    build_layout() {
        this.wrapper.find('.layout-main-section').html(
            frappe.render_template('fleet_command_center', {})
        );
    }

    init_leaflet() {
        // Inisialisasi peta Leaflet dengan preferCanvas: true
        const osmLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution: '© OpenStreetMap contributors'
        });

        this.map = L.map('command-center-map', {
            center: [-6.2145, 106.9854],
            zoom: 11,
            layers: [osmLayer],
            preferCanvas: true
        });

        this.canvasRenderer = L.canvas({ padding: 0.5 });
    }

    fetch_state() {
        frappe.call({
            method: 'fleet_customs.api.fleet_command_center.get_initial_fleet_state',
            callback: (r) => {
                if (r.message) {
                    this.permissions = r.message.user_permissions;
                    this.fleet_cache = r.message.fleet;
                    this.update_metrics(r.message.hud);
                    this.render_markers(this.fleet_cache);
                    this.render_cards(this.fleet_cache);
                }
            }
        });
    }

    render_markers(fleet) {
        Object.values(this.markers).forEach(m => this.map.removeLayer(m));
        this.markers = {};

        fleet.forEach(item => {
            if (!item.lat || !item.lng) return;

            let color = '#2e7d32'; // Safe
            if (item.risk_level === 'EMERGENCY') color = '#6a1b9a';
            else if (item.risk_level === 'BREACH') color = '#c62828';
            else if (item.risk_level === 'WARNING') color = '#f57f17';
            else if (item.risk_level === 'BREAKDOWN') color = '#e65100';

            const marker = L.circleMarker([item.lat, item.lng], {
                renderer: this.canvasRenderer,
                radius: item.risk_level === 'EMERGENCY' || item.risk_level === 'BREACH' ? 9 : 6,
                fillColor: color,
                color: '#ffffff',
                weight: 2,
                fillOpacity: 0.9
            });

            marker.bindPopup(() => this.create_safe_popup(item));
            marker.addTo(this.map);
            this.markers[item.trip_id] = marker;
        });
    }

    create_safe_popup(item) {
        // Sanitasi XSS Menyeluruh
        const plate = frappe.utils.escape_html(item.truck_plate);
        const doc = frappe.utils.escape_html(item.customs_doc);
        const aju = frappe.utils.escape_html(item.nomor_aju || '-');
        const eta = frappe.utils.escape_html(item.eta_status || 'On Time');

        let contRows = (item.containers || []).map(c => `
            <div style="margin-top:4px; font-size:11px;">
                📦 <b>${frappe.utils.escape_html(c.container_no)}</b> (${c.container_size}ft)
                <span class="badge ${c.seal_status === 'Locked' ? 'badge-success' : 'badge-danger'}">
                    ${frappe.utils.escape_html(c.seal_status)} (${c.seal_battery}%)
                </span>
            </div>
        `).join('');

        let p2Btn = this.permissions && this.permissions.can_dispatch_p2 && (item.risk_level === 'BREACH' || item.risk_level === 'EMERGENCY') ?
            `<button class="btn btn-xs btn-danger btn-block" style="margin-top:8px;" onclick="frappe.fleet_cc.dispatch_p2('${item.trip_id}')">🚨 Dispatch Tim P2</button>` : '';

        let replayBtn = `<button class="btn btn-xs btn-primary btn-block" style="margin-top:6px;" onclick="frappe.fleet_cc.replay_trip('${item.trip_id}')">🎬 Replay Perjalanan</button>`;
        let corridorBtn = item.route_corridor ?
            `<button class="btn btn-xs btn-default btn-block" style="margin-top:4px;" onclick="frappe.fleet_cc.show_corridor('${item.route_corridor}')">🗺️ Lihat Koridor Rute</button>` : '';

        return `
            <div class="customs-popup-card">
                <b>🚛 ${plate}</b> | <span class="badge badge-info">${eta}</span><br>
                <small>${doc} | AJU: ${aju}</small>
                <hr style="margin:6px 0;">
                ${contRows}
                ${replayBtn}
                ${corridorBtn}
                ${p2Btn}
            </div>
        `;
    }

    render_cards(fleet) {
        const container = $('#fleet-card-container');
        container.empty();

        const filtered = fleet.filter(item => {
            if (this.current_filter === 'alert') return item.risk_level === 'BREACH';
            if (this.current_filter === 'breakdown') return item.risk_level === 'BREAKDOWN';
            if (this.current_filter === 'emergency') return item.risk_level === 'EMERGENCY';
            return true;
        });

        if (filtered.length === 0) {
            container.html('<div class="text-muted text-center" style="padding:20px;">Tidak ada armada dalam kategori ini.</div>');
            return;
        }

        filtered.forEach(item => {
            const card = $(`
                <div class="fleet-card risk-${item.risk_level.toLowerCase()}">
                    <div class="card-head">
                        <span class="plate">${frappe.utils.escape_html(item.truck_plate)}</span>
                        <span class="badge-status">${item.risk_level}</span>
                    </div>
                    <div class="card-body-text">
                        <span>${frappe.utils.escape_html(item.trip_id)}</span>
                        <span>ETA: <b>${frappe.utils.escape_html(item.eta_status || 'On Time')}</b></span>
                    </div>
                </div>
            `);

            card.on('click', () => {
                this.map.flyTo([item.lat, item.lng], 15);
                if (this.markers[item.trip_id]) {
                    this.markers[item.trip_id].openPopup();
                }
            });

            container.append(card);
        });
    }

    update_metrics(hud) {
        $('#count-total').text(hud.total || 0);
        $('#count-safe').text(hud.safe || 0);
        $('#count-warning').text(hud.warning || 0);
        $('#count-breach').text(hud.breach || 0);
        $('#count-breakdown').text(hud.breakdown || 0);
        $('#count-emergency').text(hud.emergency || 0);
    }

    bind_sockets() {
        // [ENHANCED] Delta update WebSocket: Hanya update 1 marker & 1 card tanpa reload berat
        frappe.realtime.on('fleet_delta_update', (data) => {
            frappe.show_alert({
                message: __(`🚨 PERINGATAN PABEAN: e-Seal ${frappe.utils.escape_html(data.seal_id)} Trip ${data.trip_id} status ${data.seal_status}!`),
                indicator: 'red'
            }, 10);

            $('#audit-log-body').prepend(`
                <tr class="log-critical">
                    <td>${new Date().toLocaleTimeString()}</td>
                    <td><span class="badge badge-danger">CRITICAL</span></td>
                    <td>${frappe.utils.escape_html(data.trip_id)}</td>
                    <td>${frappe.utils.escape_html(data.incident || 'TAMPER')}</td>
                    <td><button class="btn btn-xs btn-danger" onclick="frappe.fleet_cc.map.flyTo([${data.lat}, ${data.lng}], 16)">Locate</button></td>
                </tr>
            `);

            // Update cache lokal
            let existing = this.fleet_cache.find(f => f.trip_id === data.trip_id);
            if (existing) {
                existing.lat = data.lat;
                existing.lng = data.lng;
                existing.risk_level = data.risk_level;
                existing.seal_status = data.seal_status;
            }
            this.render_markers(this.fleet_cache);
            this.render_cards(this.fleet_cache);
        });

        // Driver Panic Alert
        frappe.realtime.on('driver_panic_alert', (data) => {
            frappe.show_alert({
                message: __(`🆘 DARURAT: Driver ${frappe.utils.escape_html(data.driver_name)} memicu tombol darurat pada Trip ${data.trip_id}!`),
                indicator: 'purple'
            }, 15);

            $('#audit-log-body').prepend(`
                <tr style="background:#f3e5f5;">
                    <td>${new Date().toLocaleTimeString()}</td>
                    <td><span class="badge" style="background:#6a1b9a; color:#fff;">PANIC</span></td>
                    <td>${frappe.utils.escape_html(data.trip_id)}</td>
                    <td>EMERGENCY DRIVER</td>
                    <td><button class="btn btn-xs btn-primary" onclick="frappe.fleet_cc.map.flyTo([${data.lat}, ${data.lng}], 16)">Locate</button></td>
                </tr>
            `);
            this.fetch_state();
        });
    }

    bind_ui() {
        $('.tab-btn').on('click', (e) => {
            $('.tab-btn').removeClass('active');
            $(e.currentTarget).addClass('active');
            this.current_filter = $(e.currentTarget).data('filter');
            this.render_cards(this.fleet_cache);
        });

        $('#btn-toggle-heatmap').on('click', () => this.toggle_heatmap());
        $('#btn-reset-map').on('click', () => {
            this.map.flyTo([-6.2145, 106.9854], 11);
        });
    }

    // [NEW] Toggle Incident Heatmap
    toggle_heatmap() {
        if (this.heatLayer) {
            this.map.removeLayer(this.heatLayer);
            this.heatLayer = null;
            $('#btn-toggle-heatmap').text('🔥 Toggle Heatmap Insiden');
            return;
        }

        frappe.call({
            method: 'fleet_customs.api.fleet_command_center.get_incident_heatmap',
            callback: (r) => {
                if (r.message && r.message.length > 0) {
                    this.heatLayer = L.heatLayer(r.message, {
                        radius: 25,
                        blur: 15,
                        maxZoom: 16
                    }).addTo(this.map);
                    $('#btn-toggle-heatmap').text('❌ Matikan Heatmap');
                } else {
                    frappe.msgprint(__('Belum ada catatan insiden tamper untuk heatmap.'));
                }
            }
        });
    }

    // [NEW] Corridor Visualizer
    show_corridor(corridor_name) {
        this.clear_corridor();
        frappe.call({
            method: 'fleet_customs.api.fleet_command_center.get_corridor_geometry',
            args: { corridor_name: corridor_name },
            callback: (r) => {
                if (r.message && r.message.polyline.length > 0) {
                    const coords = r.message.polyline;
                    const tolerance = r.message.tolerance_meters;

                    // Buffer Zone (Area Transparan)
                    const bufferZone = L.polyline(coords, {
                        color: '#2196F3',
                        weight: Math.max(12, tolerance / 15),
                        opacity: 0.2
                    }).addTo(this.map);

                    // Polyline Garis Koridor
                    const centerLine = L.polyline(coords, {
                        color: '#1976D2',
                        weight: 3,
                        dashArray: '8, 6'
                    }).addTo(this.map);

                    this.corridorLayers.push(bufferZone, centerLine);
                    this.map.fitBounds(centerLine.getBounds(), { padding: [50, 50] });
                    $('#corridor-active-indicator').show();
                }
            }
        });
    }

    clear_corridor() {
        this.corridorLayers.forEach(l => this.map.removeLayer(l));
        this.corridorLayers = [];
        $('#corridor-active-indicator').hide();
    }

    // [NEW] Trip Timeline Replay
    replay_trip(trip_id) {
        frappe.call({
            method: 'fleet_customs.api.fleet_command_center.get_trip_track',
            args: { trip_id: trip_id },
            callback: (r) => {
                if (!r.message || r.message.length === 0) {
                    frappe.msgprint(__('Belum ada data GPS Log untuk trip ini.'));
                    return;
                }

                // Bersihkan layer replay sebelumnya
                this.replayLayers.forEach(l => this.map.removeLayer(l));
                this.replayLayers = [];

                const points = r.message.map(p => [p.latitude, p.longitude]);
                const trail = L.polyline(points, { color: '#00bcd4', weight: 4 }).addTo(this.map);
                this.replayLayers.push(trail);
                this.map.fitBounds(trail.getBounds(), { padding: [40, 40] });

                const replayMarker = L.circleMarker(points[0], {
                    radius: 8,
                    fillColor: '#ff9800',
                    color: '#ffffff',
                    weight: 2,
                    fillOpacity: 1
                }).addTo(this.map);
                this.replayLayers.push(replayMarker);

                let idx = 0;
                frappe.show_alert({ message: __('🎬 Memulai Replay Perjalanan...'), indicator: 'blue' }, 3);

                const anim = setInterval(() => {
                    idx++;
                    if (idx >= points.length) {
                        clearInterval(anim);
                        frappe.show_alert({ message: __('🏁 Replay Selesai.'), indicator: 'green' }, 3);
                        return;
                    }
                    replayMarker.setLatLng(points[idx]);
                }, 200);
            }
        });
    }

    dispatch_p2(trip_id) {
        frappe.confirm(__('Kirim Tim Penindakan dan Penyidikan (P2) Bea Cukai ke lokasi armada ini?'), () => {
            frappe.call({
                method: 'fleet_customs.api.fleet_command_center.dispatch_p2_action',
                args: { trip_id: trip_id },
                callback: () => {
                    frappe.msgprint(__('Instruksi penindakan P2 berhasil dikirimkan.'));
                    this.fetch_state();
                }
            });
        });
    }
}


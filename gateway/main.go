package main

import (
	"bufio"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

type IngestPayload struct {
	EventID         string  `json:"eventId"`
	SealID          string  `json:"sealId"`
	TruckID         string  `json:"truckId"`
	TripID          string  `json:"tripId"`
	Lat             float64 `json:"lat"`
	Lng             float64 `json:"lng"`
	Speed           float64 `json:"speed"`
	Heading         float64 `json:"heading"`
	Battery         int     `json:"battery"`
	IsLocked        int     `json:"isLocked"`
	IsTampered      int     `json:"isTampered"`
	TamperType      string  `json:"tamperType,omitempty"`
	DeviceTimestamp string  `json:"deviceTimestamp"`
}

var (
	rdb            *redis.Client
	ctx            = context.Background()
	diskBufferPath = "/home/anhar91/frappe-bench/apps/fleet_customs/gateway/logs/buffer.jsonl"
	bufMutex       sync.Mutex

	// In-memory key cache untuk melindungi Gateway jika Redis restart (TTL 5 Menit)
	keyCacheLock sync.RWMutex
	keyCache     = make(map[string]cachedKey)
)

type cachedKey struct {
	key       []byte
	expiresAt time.Time
}

func initRedis() {
	addr := os.Getenv("IOT_REDIS_ADDR")
	if addr == "" {
		addr = "127.0.0.1:11001"
	}
	rdb = redis.NewClient(&redis.Options{
		Addr:         addr,
		PoolSize:     150,
		MinIdleConns: 30,
	})
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Printf("Peringatan: Belum dapat terhubung ke Redis IoT Stream (%v). Buffer disk lokal siap digunakan.", err)
	} else {
		log.Println("Terkoneksi ke Dedicated IoT Redis Stream pada:", addr)
	}
}

// [FIX A1 & A2] Lookup Kunci HMAC per-device dari Redis dengan in-memory cache
func getDeviceKey(sealID string) ([]byte, error) {
	keyCacheLock.RLock()
	if c, ok := keyCache[sealID]; ok && time.Now().Before(c.expiresAt) {
		keyCacheLock.RUnlock()
		return c.key, nil
	}
	keyCacheLock.RUnlock()

	if rdb == nil {
		return nil, fmt.Errorf("redis_client_nil")
	}

	val, err := rdb.HGet(ctx, "seal_keys:"+sealID, "psk").Result()
	if err != nil || len(val) == 0 {
		return nil, fmt.Errorf("key_not_found_for_seal: %s", sealID)
	}

	rawKey := []byte(val)
	keyCacheLock.Lock()
	keyCache[sealID] = cachedKey{
		key:       rawKey,
		expiresAt: time.Now().Add(5 * time.Minute),
	}
	keyCacheLock.Unlock()

	return rawKey, nil
}

func verifyHMAC(key []byte, message []byte, receivedMac string) bool {
	mac := hmac.New(sha256.New, key)
	mac.Write(message)
	expectedMac := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(expectedMac), []byte(receivedMac))
}

// [FIX A3 & B2] Replay Attack Dedup (SET NX)
func isDuplicateEvent(eventID string) bool {
	if eventID == "" || rdb == nil {
		return false
	}
	set, err := rdb.SetNX(ctx, "seen:"+eventID, "1", 1*time.Hour).Result()
	if err != nil {
		return false
	}
	return !set
}

func cancelDedupKey(eventID string) {
	if eventID != "" && rdb != nil {
		rdb.Del(ctx, "seen:"+eventID)
	}
}

// [FIX B5] Local Disk Buffer & Drainer
func bufferToDisk(raw []byte) error {
	bufMutex.Lock()
	defer bufMutex.Unlock()

	f, err := os.OpenFile(diskBufferPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	defer f.Close()

	_, err = f.Write(append(raw, '\n'))
	return err
}

func startDiskDrainer() {
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()

		for range ticker.C {
			drainDiskBuffer()
		}
	}()
}

func drainDiskBuffer() {
	bufMutex.Lock()
	defer bufMutex.Unlock()

	if _, err := os.Stat(diskBufferPath); os.IsNotExist(err) {
		return
	}

	file, err := os.Open(diskBufferPath)
	if err != nil {
		return
	}

	scanner := bufio.NewScanner(file)
	var remainingLines [][]byte
	drainedCount := 0

	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}

		var p IngestPayload
		if err := json.Unmarshal(line, &p); err != nil {
			continue
		}

		streamKey := "stream:telemetry"
		if p.IsTampered == 1 {
			streamKey = "stream:tamper"
		}

		if rdb != nil {
			err := rdb.XAdd(ctx, &redis.XAddArgs{
				Stream: streamKey,
				MaxLen: 200000,
				Approx: true,
				Values: map[string]interface{}{
					"payload": string(line),
					"eventId": p.EventID,
				},
			}).Err()

			if err == nil {
				drainedCount++
				continue
			}
		}

		// Jika gagal XAdd, simpan kembali line yang belum ter-drain
		lineCopy := make([]byte, len(line))
		copy(lineCopy, line)
		remainingLines = append(remainingLines, lineCopy)
	}
	file.Close()

	if drainedCount > 0 {
		log.Printf("Disk Buffer: Berhasil mengalirkan %d paket yang tertunda ke Redis Stream.", drainedCount)
	}

	if len(remainingLines) == 0 {
		os.Remove(diskBufferPath)
	} else {
		// Tulis ulang sisa antrean yang belum terkirim
		f, err := os.OpenFile(diskBufferPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0644)
		if err == nil {
			for _, l := range remainingLines {
				f.Write(append(l, '\n'))
			}
			f.Close()
		}
	}
}

func ingestHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, `{"error":"METHOD_NOT_ALLOWED"}`, http.StatusMethodNotAllowed)
		return
	}

	// [FIX A1] Wajib ada signature header
	receivedSig := r.Header.Get("X-Payload-Signature")
	if receivedSig == "" {
		http.Error(w, `{"error":"MISSING_SIGNATURE"}`, http.StatusUnauthorized)
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, 65536) // Batas 64KB
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, `{"error":"PAYLOAD_EXCEEDS_LIMIT"}`, http.StatusBadRequest)
		return
	}

	var p IngestPayload
	if err := json.Unmarshal(body, &p); err != nil || p.SealID == "" || p.TripID == "" {
		http.Error(w, `{"error":"INVALID_SCHEMA"}`, http.StatusBadRequest)
		return
	}

	// [FIX A1 & A2] Verifikasi HMAC per-device (Tolak jika device key tidak ditemukan atau signature salah)
	deviceKey, err := getDeviceKey(p.SealID)
	if err != nil {
		http.Error(w, `{"error":"UNKNOWN_DEVICE_OR_UNREGISTERED_KEY"}`, http.StatusUnauthorized)
		return
	}

	if !verifyHMAC(deviceKey, body, receivedSig) {
		http.Error(w, `{"error":"INVALID_HMAC_SIGNATURE"}`, http.StatusUnauthorized)
		return
	}

	// [FIX A3] Dedup diperiksa SETELAH autentikasi HMAC lolos (Anti Front-Running DOS)
	if isDuplicateEvent(p.EventID) {
		http.Error(w, `{"error":"DUPLICATE_EVENT_REJECTED"}`, http.StatusConflict)
		return
	}

	// Pencegahan Clock Skew (+-15 Menit)
	devTime, err := time.Parse(time.RFC3339, p.DeviceTimestamp)
	if err == nil {
		drift := time.Since(devTime)
		if drift > 15*time.Minute || drift < -15*time.Minute {
			p.DeviceTimestamp = time.Now().UTC().Format(time.RFC3339)
		}
	} else {
		p.DeviceTimestamp = time.Now().UTC().Format(time.RFC3339)
	}

	rawJSON, _ := json.Marshal(p)

	// [FIX A4] XADD dengan MAXLEN 200000 approx trim (~200k items) untuk Zero-OOM
	streamKey := "stream:telemetry"
	if p.IsTampered == 1 {
		streamKey = "stream:tamper"
	}

	var streamErr error
	if rdb != nil {
		streamErr = rdb.XAdd(ctx, &redis.XAddArgs{
			Stream: streamKey,
			MaxLen: 200000,
			Approx: true,
			Values: map[string]interface{}{
				"payload": string(rawJSON),
				"eventId": p.EventID,
			},
		}).Err()
	} else {
		streamErr = fmt.Errorf("redis_client_nil")
	}

	if streamErr != nil {
		// [FIX B2] Hapus key dedup jika XADD gagal agar tidak terjadi data lockout 1 jam
		cancelDedupKey(p.EventID)

		// Simpan ke disk buffer
		_ = bufferToDisk(rawJSON)
		w.WriteHeader(http.StatusAccepted)
		w.Write([]byte(`{"status":"BUFFERED_ON_DISK"}`))
		return
	}

	w.WriteHeader(http.StatusAccepted)
	w.Write([]byte(`{"status":"QUEUED"}`))
}

func main() {
	initRedis()
	startDiskDrainer()

	mux := http.NewServeMux()
	mux.HandleFunc("/ingest", ingestHandler)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("HEALTHY"))
	})

	server := &http.Server{
		Addr:         ":8088",
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
		IdleTimeout:  30 * time.Second,
	}

	log.Println("Go Ingestion Engine (Hardened v2.1) aktif di port :8088")
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Listener crash: %v", err)
	}
}

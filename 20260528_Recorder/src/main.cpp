#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>

// =================== user config ===================
#define WIFI_SSID      "2433"
#define WIFI_PASS      "freespace"

#define SERVER_HOST    "grayfog.chat"
#define SERVER_PORT    9123

#define SAMPLE_RATE_HZ 10000   // samples per second (ADC rate)
#define BATCH_MS       250     // milliseconds of data per upload
// ===================================================

// Samples per batch and bytes (uint16 little-endian per sample).
static const size_t BATCH_SAMPLES = (size_t)SAMPLE_RATE_HZ * BATCH_MS / 1000;
static uint16_t g_buf[BATCH_SAMPLES];

static String g_boardId;
static uint32_t g_seq = 0;
static String g_url;

static WiFiClient g_client;
static HTTPClient g_http;

// Unique per-board ID derived from the factory eFuse MAC (48-bit).
static String deriveBoardId() {
  uint64_t mac = ESP.getEfuseMac();
  uint8_t b[6];
  for (int i = 0; i < 6; i++) b[i] = (mac >> (8 * i)) & 0xFF;
  char out[20];
  snprintf(out, sizeof(out), "esp32-%02x%02x%02x%02x%02x%02x",
           b[0], b[1], b[2], b[3], b[4], b[5]);
  return String(out);
}

// Data source. TODO: replace with real ADC ring-buffer drain once the
// hardware sampling path works. For now: simulated 12-bit ADC noise.
static void fillSamples(uint16_t *buf, size_t n) {
  for (size_t i = 0; i < n; i++) {
    buf[i] = (uint16_t)(esp_random() & 0x0FFF);  // 0..4095
  }
}

static void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[wifi] connecting to %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] connected, ip=%s rssi=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("[wifi] connect timeout, will retry");
  }
}

static bool uploadBatch(const uint8_t *data, size_t len) {
  g_http.begin(g_client, g_url);
  g_http.setReuse(true);
  g_http.addHeader("Content-Type", "application/octet-stream");
  g_http.addHeader("X-Seq", String(g_seq));
  g_http.addHeader("X-Sample-Rate", String(SAMPLE_RATE_HZ));
  int code = g_http.POST((uint8_t *)data, len);
  g_http.end();
  if (code == 200) return true;
  Serial.printf("[upload] seq=%lu HTTP %d\n", (unsigned long)g_seq, code);
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  g_boardId = deriveBoardId();
  g_url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/ingest/" + g_boardId;
  Serial.printf("[boot] board=%s\n", g_boardId.c_str());
  Serial.printf("[boot] url=%s\n", g_url.c_str());
  Serial.printf("[boot] rate=%dHz batch=%dms (%u samples / %u bytes)\n",
                SAMPLE_RATE_HZ, BATCH_MS, (unsigned)BATCH_SAMPLES,
                (unsigned)(BATCH_SAMPLES * 2));
  connectWiFi();
}

void loop() {
  static uint32_t nextBatch = 0;
  static uint32_t okCount = 0, failCount = 0;
  static uint32_t lastReport = 0;

  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    return;
  }

  uint32_t now = millis();
  if ((int32_t)(now - nextBatch) >= 0) {
    nextBatch = now + BATCH_MS;
    fillSamples(g_buf, BATCH_SAMPLES);
    if (uploadBatch((uint8_t *)g_buf, BATCH_SAMPLES * 2)) {
      okCount++;
      g_seq++;
    } else {
      failCount++;
    }
  }

  if (now - lastReport >= 2000) {
    lastReport = now;
    Serial.printf("[stat] seq=%lu ok=%lu fail=%lu rssi=%d heap=%lu\n",
                  (unsigned long)g_seq, (unsigned long)okCount,
                  (unsigned long)failCount, WiFi.RSSI(),
                  (unsigned long)ESP.getFreeHeap());
  }
}

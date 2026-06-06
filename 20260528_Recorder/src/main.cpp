#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "driver/adc.h"   // ESP-IDF 4.4 continuous (DMA) ADC for gap-free sampling
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

// =================== user config ===================
// WiFi networks tried strictly in this order, each with WIFI_CONN_TIMEOUT_MS.
// If none connect, the list is retried from the top, looping until one does.
// Empty-SSID slots are skipped.
struct WifiCred { const char *ssid; const char *pass; };
static const WifiCred WIFI_LIST[] = {
  {"DDX",          "18817308954"},   // #1
  {"沙包大的沙包",  "123qweasdzxc"},   // #2
  {"2433",         "freespace"},      // #3
};
#define WIFI_CONN_TIMEOUT_MS 10000    // per-network connect timeout

#define SERVER_HOST    "grayfog.chat"
#define SERVER_PORT    8123

#define SAMPLE_RATE_HZ 10000   // samples per second (ADC rate)
#define BATCH_MS       250     // milliseconds of data per upload

// MAX9814 electret mic amplifier wiring.
#define MIC_OUT_PIN    4       // OUT -> GPIO4 = ADC1_CH3 (WiFi-safe; ADC1 only)
#define MIC_GAIN_PIN   14      // GAIN select pin (driven digital / left hi-Z)
#define MIC_GAIN_DB    50      // 40 = GAIN->VDD, 50 = GAIN->GND, 60 = GAIN floating

// Continuous ADC (DMA) sampling. The DMA pool keeps filling at SAMPLE_RATE_HZ
// regardless of upload state, so a slow/stalled POST no longer creates a gap.
#define MIC_ADC_CHANNEL  ADC1_CHANNEL_3   // GPIO4 = ADC1_CH3 (keep in sync with MIC_OUT_PIN)
#define ADC_DMA_SECONDS  1                 // hardware DMA pool depth (seconds of audio)

// Producer/consumer decoupling: the ADC reader task drains the DMA pool into
// this many app-level batch buffers; the uploader task drains those over HTTP.
// A slow/stalled POST only grows this backlog instead of starving the ADC, so
// sampling runs at 100% duty. Backlog tolerance = NUM_BATCH_BUFS * BATCH_MS
// (plus the hardware DMA pool). When full, the oldest batch is dropped.
#define NUM_BATCH_BUFS   16                // 16 * 250ms = 4s of upload-stall slack

// Debug: when 1, skip WiFi/upload entirely and stream GPIO7 over serial as
// "P <mean> <min> <max>" at ~100Hz for the local web plot (webgui/serial_plot.py).
// Set back to 0 and reflash to resume the recorder/upload behavior.
#define DEBUG_PLOT     0

// LED_SCAN: diagnostic mode to find the onboard RGB data pin. When 1, setup()
// lights each candidate GPIO white for 2s in order and loops forever; whichever
// step turns the LED white identifies the pin. Set back to 0 after locating it.
#define LED_SCAN       0
// ===================================================

// Samples per batch and bytes (uint16 little-endian per sample).
static const size_t BATCH_SAMPLES = (size_t)SAMPLE_RATE_HZ * BATCH_MS / 1000;

// Pool of fixed-size batch buffers cycled between the two queues below. A buffer
// is always in exactly one place: g_freeQ (empty), g_readyQ (filled, awaiting
// upload), being filled by the reader, or being POSTed by the uploader.
static uint16_t g_pool[NUM_BATCH_BUFS][BATCH_SAMPLES];
static QueueHandle_t g_freeQ;    // holds uint16_t* to empty buffers
static QueueHandle_t g_readyQ;   // holds uint16_t* to filled buffers

static String g_boardId;
static String g_url;

// Cross-task stats (32-bit aligned -> atomic enough on Xtensa for reporting).
static volatile uint32_t g_seq = 0;          // owned by uploader
static volatile uint32_t g_okCount = 0;      // owned by uploader
static volatile uint32_t g_failCount = 0;    // owned by uploader
static volatile uint32_t g_dropCount = 0;    // owned by reader: app-buffer overruns
static volatile uint32_t g_hwOverflow = 0;   // owned by reader: DMA pool overruns
static volatile uint16_t g_mn = 0, g_mx = 0, g_mean = 0;  // owned by uploader

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

// Select MAX9814 gain via the GAIN pin's three states.
//   GAIN -> VDD = 40dB, GAIN -> GND = 50dB, GAIN floating = 60dB.
static void setMicGain() {
#if MIC_GAIN_DB == 40
  pinMode(MIC_GAIN_PIN, OUTPUT);
  digitalWrite(MIC_GAIN_PIN, HIGH);
#elif MIC_GAIN_DB == 50
  pinMode(MIC_GAIN_PIN, OUTPUT);
  digitalWrite(MIC_GAIN_PIN, LOW);
#else  // 60dB: leave the pin floating (high-impedance input).
  pinMode(MIC_GAIN_PIN, INPUT);
#endif
}

// Quick batch stats so the mic can be verified from the serial log alone,
// even with no ingest server running. A silent batch sits near the DC bias
// with a small spread; sound widens min..max noticeably.
static void batchStats(const uint16_t *buf, size_t n,
                       uint16_t *mn, uint16_t *mx, uint16_t *mean) {
  uint16_t lo = 0xFFFF, hi = 0;
  uint64_t sum = 0;
  for (size_t i = 0; i < n; i++) {
    uint16_t v = buf[i];
    if (v < lo) lo = v;
    if (v > hi) hi = v;
    sum += v;
  }
  *mn = lo;
  *mx = hi;
  *mean = (uint16_t)(sum / (n ? n : 1));
}

#if DEBUG_PLOT
// Stream GPIO7 over serial for the web plot: ~100 windows/sec, each window is
// 10ms of samples reduced to mean/min/max. Line format: "P <mean> <min> <max>".
static void debugPlotLoop() {
  const uint32_t period_us = 1000000UL / SAMPLE_RATE_HZ;
  const size_t WIN = SAMPLE_RATE_HZ / 100;  // 10ms worth of samples
  uint32_t sum = 0;
  uint16_t mn = 0xFFFF, mx = 0;
  uint32_t t = micros();
  for (size_t i = 0; i < WIN; i++) {
    uint16_t v = (uint16_t)analogRead(MIC_OUT_PIN);
    sum += v;
    if (v < mn) mn = v;
    if (v > mx) mx = v;
    t += period_us;
    int32_t wait = (int32_t)(t - micros());
    if (wait > 0) delayMicroseconds((uint32_t)wait);
  }
  Serial.printf("P %u %u %u\n", (unsigned)(sum / WIN), mn, mx);
}
#endif

// Try each configured network in order, each up to WIFI_CONN_TIMEOUT_MS.
// Loops over the whole list repeatedly and only returns once connected.
static void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  const size_t n = sizeof(WIFI_LIST) / sizeof(WIFI_LIST[0]);
  uint32_t round = 0;
  while (true) {
    for (size_t i = 0; i < n; i++) {
      if (WIFI_LIST[i].ssid[0] == '\0') continue;  // skip empty slot
      Serial.printf("[wifi] try #%u %s ...\n", (unsigned)(i + 1), WIFI_LIST[i].ssid);
      WiFi.disconnect();
      WiFi.begin(WIFI_LIST[i].ssid, WIFI_LIST[i].pass);
      uint32_t start = millis();
      while (WiFi.status() != WL_CONNECTED &&
             millis() - start < WIFI_CONN_TIMEOUT_MS) {
        delay(250);
        Serial.print('.');
      }
      Serial.println();
      if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[wifi] connected ssid=%s ip=%s rssi=%d\n",
                      WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(),
                      WiFi.RSSI());
        return;
      }
      Serial.printf("[wifi] #%u timeout\n", (unsigned)(i + 1));
    }
    Serial.printf("[wifi] none connected (round %lu), retrying list ...\n",
                  (unsigned long)(++round));
  }
}

// POST one batch. Runs only on the uploader task, so a slow/stalled request
// blocks nothing but itself -- the reader keeps sampling meanwhile.
static bool uploadBatch(HTTPClient &http, WiFiClient &client,
                        const uint8_t *data, size_t len) {
  http.begin(client, g_url);
  http.setReuse(true);
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("X-Seq", String(g_seq));
  http.addHeader("X-Sample-Rate", String(SAMPLE_RATE_HZ));
  int code = http.POST((uint8_t *)data, len);
  http.end();
  if (code == 200) return true;
  Serial.printf("[upload] seq=%lu HTTP %d\n", (unsigned long)g_seq, code);
  return false;
}

// Each DMA conversion result is one of these (4 bytes on the ESP32-S3).
static const size_t ADC_RESULT_BYTES = sizeof(adc_digi_output_data_t);

// Start ADC1 continuous (DMA) sampling of MIC_ADC_CHANNEL at SAMPLE_RATE_HZ.
// The driver fills an internal pool via DMA/IRQ in the background; the reader
// task drains it. Pool size = ADC_DMA_SECONDS of audio; the app-level backlog
// (NUM_BATCH_BUFS) absorbs longer upload stalls on top of that.
static void adcDmaInit() {
  adc_digi_init_config_t init_cfg = {};
  init_cfg.max_store_buf_size =
      (uint32_t)(SAMPLE_RATE_HZ * ADC_DMA_SECONDS * ADC_RESULT_BYTES);
  init_cfg.conv_num_each_intr = 1024;
  init_cfg.adc1_chan_mask = (uint16_t)BIT(MIC_ADC_CHANNEL);
  init_cfg.adc2_chan_mask = 0;
  ESP_ERROR_CHECK(adc_digi_initialize(&init_cfg));

  static adc_digi_pattern_config_t pat = {};
  pat.atten = ADC_ATTEN_DB_12;   // ~0..3.1V full-scale (was ADC_ATTEN_DB_11)
  pat.channel = MIC_ADC_CHANNEL;
  pat.unit = 0;                  // digi pattern wants 0=ADC1 (enum ADC_UNIT_1==1 means ADC2 here)
  pat.bit_width = SOC_ADC_DIGI_MAX_BITWIDTH;  // S3 digi controller = 12-bit

  adc_digi_configuration_t dig_cfg = {};
  dig_cfg.conv_limit_en = false;
  dig_cfg.pattern_num = 1;
  dig_cfg.adc_pattern = &pat;
  dig_cfg.sample_freq_hz = SAMPLE_RATE_HZ;
  dig_cfg.conv_mode = ADC_CONV_SINGLE_UNIT_1;
  dig_cfg.format = ADC_DIGI_OUTPUT_FORMAT_TYPE2;
  ESP_ERROR_CHECK(adc_digi_controller_configure(&dig_cfg));
  ESP_ERROR_CHECK(adc_digi_start());
}

// Producer: drain the DMA pool into app batch buffers, never touching the
// network. The only blocking call is adc_digi_read_bytes, which sleeps the task
// until DMA data is ready. When the uploader can't keep up and no free buffer is
// available, the oldest queued batch is reclaimed (dropped) so sampling never
// stalls -- this is the only path that can lose samples, and it is counted.
static void adcReaderTask(void *) {
  static uint8_t rd[BATCH_SAMPLES * 4];  // scratch for raw DMA results (4B each)
  uint16_t *cur = nullptr;
  size_t fill = 0;

  for (;;) {
    if (!cur) {
      if (xQueueReceive(g_freeQ, &cur, 0) != pdTRUE) {
        // Uploader is backed up: drop the oldest filled batch to free a buffer.
        if (xQueueReceive(g_readyQ, &cur, 0) == pdTRUE) g_dropCount++;
        else { vTaskDelay(1); continue; }  // momentary; should not happen
      }
      fill = 0;
    }

    uint32_t got = 0;
    esp_err_t r = adc_digi_read_bytes(rd, sizeof(rd), &got, portMAX_DELAY);
    if (r == ESP_ERR_INVALID_STATE) g_hwOverflow++;  // DMA pool overran
    if (got == 0) continue;

    for (uint32_t i = 0; i + ADC_RESULT_BYTES <= got; i += ADC_RESULT_BYTES) {
      adc_digi_output_data_t *p = (adc_digi_output_data_t *)&rd[i];
      if (p->type2.channel != MIC_ADC_CHANNEL) continue;  // ignore stray channels
      cur[fill++] = p->type2.data;
      if (fill >= BATCH_SAMPLES) {
        xQueueSend(g_readyQ, &cur, 0);  // length == pool size -> never blocks
        cur = nullptr;
        fill = 0;
        break;  // grab a fresh buffer on the next outer iteration
      }
    }
  }
}

// Consumer: pull filled batches and POST them. A slow request only lets the
// backlog grow; it cannot stall the reader.
static void uploaderTask(void *) {
  WiFiClient client;
  HTTPClient http;
  uint16_t *buf = nullptr;

  for (;;) {
    if (xQueueReceive(g_readyQ, &buf, portMAX_DELAY) != pdTRUE) continue;

    uint16_t mn, mx, mean;
    batchStats(buf, BATCH_SAMPLES, &mn, &mx, &mean);
    g_mn = mn; g_mx = mx; g_mean = mean;

    if (uploadBatch(http, client, (uint8_t *)buf, BATCH_SAMPLES * 2)) {
      g_okCount++; g_seq++;
    } else {
      g_failCount++;
    }

    xQueueSend(g_freeQ, &buf, portMAX_DELAY);  // return buffer to the free pool
    buf = nullptr;
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();

#if LED_SCAN
  // Sweep candidate RGB data pins: white 2s each, in this order, 0.6s gap.
  static const uint8_t scanPins[] = {
    1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 21,
    33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 45, 46, 47, 48};
  Serial.println("[ledscan] start");
  for (;;) {
    for (uint8_t i = 0; i < sizeof(scanPins) / sizeof(scanPins[0]); i++) {
      Serial.printf("[ledscan] #%u GPIO%u\n", (unsigned)(i + 1), scanPins[i]);
      neopixelWrite(scanPins[i], 90, 90, 90);
      delay(2000);
      neopixelWrite(scanPins[i], 0, 0, 0);
      delay(600);
    }
    Serial.println("[ledscan] --- sweep restart ---");
    delay(1500);
  }
#endif

  // Kill the onboard RGB LED (WS2812). Board clones wire it to various GPIOs, so
  // black out every plausible data pin. System pins (flash 26-32, PSRAM 33-37,
  // native-USB 19/20, UART0 43/44) and our mic 4 / gain 14 are deliberately
  // excluded. The red power LED is hardwired to 3V3 and not software-controllable.
  static const uint8_t rgbPins[] = {
    48, 47, 46, 45, 42, 41, 40, 39, 38, 21,
    18, 17, 16, 15, 13, 12, 11, 10, 9, 8, 7, 6, 5, 2, 1};
  for (uint8_t i = 0; i < sizeof(rgbPins) / sizeof(rgbPins[0]); i++)
    neopixelWrite(rgbPins[i], 0, 0, 0);

  setMicGain();

  g_boardId = deriveBoardId();
  g_url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/ingest/" + g_boardId;
  Serial.printf("[boot] board=%s\n", g_boardId.c_str());
  Serial.printf("[boot] mic out=GPIO%d (ADC1) gain=GPIO%d (%ddB)\n",
                MIC_OUT_PIN, MIC_GAIN_PIN, MIC_GAIN_DB);
#if DEBUG_PLOT
  // analogRead path for the web plot.
  analogReadResolution(12);
  analogSetPinAttenuation(MIC_OUT_PIN, ADC_11db);
  Serial.println("[boot] DEBUG_PLOT mode: streaming GPIO7, WiFi/upload disabled");
  return;  // skip WiFi setup; loop() streams ADC for the web plot
#endif
  Serial.printf("[boot] url=%s\n", g_url.c_str());
  Serial.printf("[boot] rate=%dHz batch=%dms (%u samples / %u bytes)\n",
                SAMPLE_RATE_HZ, BATCH_MS, (unsigned)BATCH_SAMPLES,
                (unsigned)(BATCH_SAMPLES * 2));

  connectWiFi();

  // Build the buffer pool + queues, then start sampling so no DMA data is
  // produced before there is somewhere to put it.
  g_freeQ = xQueueCreate(NUM_BATCH_BUFS, sizeof(uint16_t *));
  g_readyQ = xQueueCreate(NUM_BATCH_BUFS, sizeof(uint16_t *));
  configASSERT(g_freeQ && g_readyQ);
  for (int i = 0; i < NUM_BATCH_BUFS; i++) {
    uint16_t *p = g_pool[i];
    xQueueSend(g_freeQ, &p, 0);
  }

  adcDmaInit();
  Serial.printf("[boot] ADC DMA started: %dHz continuous, %ds pool, "
                "%d-batch backlog (%ums slack)\n",
                SAMPLE_RATE_HZ, ADC_DMA_SECONDS, NUM_BATCH_BUFS,
                (unsigned)(NUM_BATCH_BUFS * BATCH_MS));

  // Reader pinned to core 1 (APP_CPU) at high priority so DMA drains promptly;
  // uploader on core 0 (PRO_CPU) alongside the WiFi stack since it is I/O-bound.
  xTaskCreatePinnedToCore(adcReaderTask, "adcReader", 4096, nullptr, 10, nullptr, 1);
  xTaskCreatePinnedToCore(uploaderTask, "uploader", 8192, nullptr, 5, nullptr, 0);
}

void loop() {
#if DEBUG_PLOT
  debugPlotLoop();
  return;
#endif
  // Sampling + upload run in their own tasks now; loop() just watches the link
  // and reports. If WiFi drops, reboot so the whole ordered reconnect runs clean.
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] connection lost -> restarting");
    delay(100);
    ESP.restart();
  }

  Serial.printf("[stat] seq=%lu ok=%lu fail=%lu drop=%lu hwovf=%lu rssi=%d heap=%lu | "
                "adc min=%u max=%u mean=%u pp=%u\n",
                (unsigned long)g_seq, (unsigned long)g_okCount,
                (unsigned long)g_failCount, (unsigned long)g_dropCount,
                (unsigned long)g_hwOverflow, WiFi.RSSI(),
                (unsigned long)ESP.getFreeHeap(),
                g_mn, g_mx, g_mean, (unsigned)(g_mx - g_mn));
  delay(2000);
}

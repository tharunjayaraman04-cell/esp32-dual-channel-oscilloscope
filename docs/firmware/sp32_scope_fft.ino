#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <math.h>

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_ADDR 0x3C

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

const int CH1_PIN = 34;
const int CH2_PIN = 35;
const int DAC_PIN = 25;

const uint16_t NUM_SAMPLES = 512;
uint16_t ch1_buf[NUM_SAMPLES];
uint16_t ch2_buf[NUM_SAMPLES];
uint16_t oled_ch1[NUM_SAMPLES];
uint16_t oled_ch2[NUM_SAMPLES];
volatile bool oled_buf_ready = false;

uint8_t wave_type = 0;      // 0: Sine, 1: Square, 2: Triangle
uint16_t target_freq = 200; // Hz
uint8_t lut[256];
volatile uint32_t phase_acc = 0;
volatile uint32_t phase_inc = 0;
hw_timer_t *dac_timer = nullptr;

bool oled_present = false;

void build_lut(uint8_t type) {
  for (int i = 0; i < 256; i++) {
    if (type == 0) {
      lut[i] = (uint8_t)(128 + 98 * sinf((2.0f * PI * i) / 256.0f));
    } else if (type == 1) {
      lut[i] = (i < 128) ? 225 : 30;
    } else {
      lut[i] = (i < 128) ? (30 + (i * 195) / 128) : (225 - ((i - 128) * 195) / 128);
    }
  }
}

void update_phase_inc(uint16_t freq) {
  phase_inc = (uint32_t)(((uint64_t)freq << 32) / 50000ULL);
}

void IRAM_ATTR onDacTimer() {
  phase_acc += phase_inc;
  dacWrite(DAC_PIN, lut[phase_acc >> 24]);
}

void parse_serial_cmd() {
  static char cmd[16];
  static uint8_t idx = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      cmd[idx] = '\0';
      if (idx >= 2) {
        if (cmd[0] == 'W') {
          wave_type = (uint8_t)atoi(&cmd[1]);
          build_lut(wave_type);
        } else if (cmd[0] == 'F') {
          uint16_t f = (uint16_t)atoi(&cmd[1]);
          if (f >= 20 && f <= 2000) {
            target_freq = f;
            update_phase_inc(target_freq);
          }
        }
      }
      idx = 0;
    } else if (idx < sizeof(cmd) - 1) {
      cmd[idx++] = c;
    }
  }
}

void oledTask(void *pvParameters) {
  Wire.begin(21, 22);
  Wire.setClock(400000);
  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    oled_present = true;
    display.clearDisplay();
    display.display();
  }

  for (;;) {
    if (oled_present && oled_buf_ready) {
      display.clearDisplay();

      // Calculate CH1 and CH2 Vpp for OLED header
      uint16_t min1 = 4095, max1 = 0;
      uint16_t min2 = 4095, max2 = 0;
      for (uint16_t i = 0; i < NUM_SAMPLES / 2; i++) {
        if (oled_ch1[i] < min1) min1 = oled_ch1[i];
        if (oled_ch1[i] > max1) max1 = oled_ch1[i];
        if (oled_ch2[i] < min2) min2 = oled_ch2[i];
        if (oled_ch2[i] > max2) max2 = oled_ch2[i];
      }

      float vpp1 = (max1 - min1) * (3.3f / 4095.0f);
      float vpp2 = (max2 - min2) * (3.3f / 4095.0f);

      // Top text bar: CH1 solid | CH2 dotted
      display.setTextSize(1);
      display.setTextColor(SSD1306_WHITE);
      display.setCursor(0, 0);
      display.print("CH1:");
      display.print(vpp1, 1);
      display.print("V  CH2:");
      display.print(vpp2, 1);
      display.print("V");
      display.drawFastHLine(0, 10, SCREEN_WIDTH, SSD1306_WHITE);

      // Midpoint & bottom reference dots (y = 12 to 63 is waveform area)
      for (int x = 0; x < SCREEN_WIDTH; x += 4) {
        display.drawPixel(x, 37, SSD1306_WHITE);
        display.drawPixel(x, 63, SSD1306_WHITE);
      }

      // Rising-edge trigger on CH1
      uint16_t mid = (min1 + max1) / 2;
      uint16_t trig = 0;
      for (uint16_t i = 1; i < NUM_SAMPLES / 2; i++) {
        if (oled_ch1[i - 1] < mid && oled_ch1[i] >= mid) {
          trig = i;
          break;
        }
      }

      // Draw CH1 (solid) and CH2 (dashed) in y = 12..62
      for (int x = 0; x < SCREEN_WIDTH - 1; x++) {
        uint16_t idx0 = trig + (x * 2);
        uint16_t idx1 = trig + ((x + 1) * 2);
        if (idx1 >= NUM_SAMPLES) break;

        int y1_a = 62 - map(oled_ch1[idx0], 0, 4095, 0, 50);
        int y1_b = 62 - map(oled_ch1[idx1], 0, 4095, 0, 50);
        display.drawLine(x, y1_a, x + 1, y1_b, SSD1306_WHITE);

        int y2_a = 62 - map(oled_ch2[idx0], 0, 4095, 0, 50);
        int y2_b = 62 - map(oled_ch2[idx1], 0, 4095, 0, 50);
        if (x % 2 == 0) {
          display.drawLine(x, y2_a, x + 1, y2_b, SSD1306_WHITE);
        }
      }

      display.display();
      oled_buf_ready = false;
    }
    vTaskDelay(pdMS_TO_TICKS(60));
  }
}

void setup() {
  Serial.begin(921600);
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  build_lut(wave_type);
  update_phase_inc(target_freq);

  dac_timer = timerBegin(50000); // ESP32 Arduino Core 3.x API
  timerAttachInterrupt(dac_timer, &onDacTimer);
  timerAlarm(dac_timer, 1, true, 0);

  xTaskCreatePinnedToCore(oledTask, "OLED_Task", 4096, NULL, 1, NULL, 0);
}

void loop() {
  parse_serial_cmd();

  uint32_t t0 = micros();
  for (uint16_t i = 0; i < NUM_SAMPLES; i++) {
    ch1_buf[i] = analogRead(CH1_PIN);
    ch2_buf[i] = analogRead(CH2_PIN);
  }
  uint32_t dt_us = micros() - t0;

  uint8_t header[2] = {0x55, 0xAA};
  Serial.write(header, 2);
  Serial.write((uint8_t *)&dt_us, 4);
  Serial.write((uint8_t *)ch1_buf, NUM_SAMPLES * 2);
  Serial.write((uint8_t *)ch2_buf, NUM_SAMPLES * 2);

  if (!oled_buf_ready) {
    memcpy(oled_ch1, ch1_buf, sizeof(ch1_buf));
    memcpy(oled_ch2, ch2_buf, sizeof(ch2_buf));
    oled_buf_ready = true;
  }
}

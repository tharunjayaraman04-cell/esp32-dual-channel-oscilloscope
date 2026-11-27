import os
import sys
import struct
from datetime import datetime
import numpy as np
import serial
import serial.tools.list_ports
import pyqtgraph as pg
from PyQt5 import QtWidgets, QtCore

BAUD_RATE = 921600
NUM_SAMPLES = 512
FRAME_PAYLOAD_BYTES = 4 + (NUM_SAMPLES * 2 * 2)
V_REF = 3.3

def get_screenshots_dir():
    win_shots = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
    if os.path.isdir(win_shots):
        return win_shots
    fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
    os.makedirs(fallback, exist_ok=True)
    return fallback

def find_esp32_port():
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if any(k in p.description.upper() for k in ["CP210", "CH340", "CH341", "USB", "UART", "SERIAL"]):
            return p.device
    return ports[0].device if ports else None

class OscilloscopeApp(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ESP32 Dual-Channel Digital Storage Oscilloscope & FFT Analyzer")
        self.resize(1260, 810)

        self.ser = None
        self.current_port = None
        self.is_running = True
        self.trigger_mode = "RISING"
        self.coupling_mode = "DC"
        self.status_flash_until = 0
        self.status_flash_text = ""

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # --- 1. TOP LIVE METRICS BAR ---
        self.metrics_label = QtWidgets.QLabel("Waiting for ESP32 USB connection...")
        self.metrics_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #00FFAA; "
            "background: #141414; padding: 10px; border: 1px solid #333; border-radius: 5px;"
        )
        layout.addWidget(self.metrics_label)

        # --- 2. TIME-DOMAIN OSCILLOSCOPE PLOT ---
        self.time_plot = pg.PlotWidget(title="Time Domain: Input (Vin) vs Output (Vout)")
        self.time_plot.setBackground('#0B0B0B')
        self.time_plot.setYRange(-0.1, 3.45)
        self.time_plot.setLabel('left', 'Voltage', units='V')
        self.time_plot.setLabel('bottom', 'Time', units='ms')
        self.time_plot.showGrid(x=True, y=True, alpha=0.35)
        self.time_plot.addLegend()

        self.ref_zero = pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen('#444444', width=1, style=QtCore.Qt.DashLine))
        self.ref_mid = pg.InfiniteLine(pos=1.65, angle=0, pen=pg.mkPen('#383838', width=1, style=QtCore.Qt.DotLine))
        self.ref_top = pg.InfiniteLine(pos=3.3, angle=0, pen=pg.mkPen('#444444', width=1, style=QtCore.Qt.DashLine))
        self.time_plot.addItem(self.ref_zero)
        self.time_plot.addItem(self.ref_mid)
        self.time_plot.addItem(self.ref_top)

        self.curve_ch1 = self.time_plot.plot(pen=pg.mkPen('#FFD700', width=2.2), name="CH1: Vin (GPIO 34)")
        self.curve_ch2 = self.time_plot.plot(pen=pg.mkPen('#00BFFF', width=2.2), name="CH2: Vout (GPIO 35)")
        layout.addWidget(self.time_plot, stretch=3)

        # --- 3. FREQUENCY-DOMAIN FFT SPECTRUM PLOT ---
        self.fft_plot = pg.PlotWidget(title="Real-Time FFT Spectrum Analyzer")
        self.fft_plot.setBackground('#0B0B0B')
        self.fft_plot.setLabel('left', 'Magnitude', units='V')
        self.fft_plot.setLabel('bottom', 'Frequency', units='Hz')
        self.fft_plot.setXRange(0, 2000)
        self.fft_plot.showGrid(x=True, y=True, alpha=0.35)
        self.fft_plot.addLegend()

        self.fft_ch1 = self.fft_plot.plot(pen=pg.mkPen('#FFD700', width=1.6), name="CH1 Spectrum")
        self.fft_ch2 = self.fft_plot.plot(pen=pg.mkPen('#00BFFF', width=1.6), name="CH2 Spectrum")
        layout.addWidget(self.fft_plot, stretch=2)

        # --- 4. CLEAN BOTTOM CONTROL BAR (NO DUPLICATE COM BOX) ---
        ctrl_bar = QtWidgets.QHBoxLayout()

        self.btn_run = QtWidgets.QPushButton("RUN / FREEZE")
        self.btn_run.setFocusPolicy(QtCore.Qt.NoFocus)
        self.btn_run.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #2E7D32; color: white;")
        self.btn_run.clicked.connect(self.toggle_run)
        ctrl_bar.addWidget(self.btn_run)

        self.btn_trig = QtWidgets.QPushButton("TRIGGER: RISING")
        self.btn_trig.setFocusPolicy(QtCore.Qt.NoFocus)
        self.btn_trig.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #1565C0; color: white;")
        self.btn_trig.clicked.connect(self.cycle_trigger)
        ctrl_bar.addWidget(self.btn_trig)

        self.btn_coupling = QtWidgets.QPushButton("COUPLING: DC (0-3.3V)")
        self.btn_coupling.setFocusPolicy(QtCore.Qt.NoFocus)
        self.btn_coupling.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #EF6C00; color: white;")
        self.btn_coupling.clicked.connect(self.toggle_coupling)
        ctrl_bar.addWidget(self.btn_coupling)

        self.btn_shot = QtWidgets.QPushButton("SCREENSHOT (S)")
        self.btn_shot.setFocusPolicy(QtCore.Qt.NoFocus)
        self.btn_shot.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #6A1B9A; color: white;")
        self.btn_shot.clicked.connect(self.take_screenshot)
        ctrl_bar.addWidget(self.btn_shot)

        self.btn_reset = QtWidgets.QPushButton("RESET (SPACE)")
        self.btn_reset.setFocusPolicy(QtCore.Qt.NoFocus)
        self.btn_reset.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #455A64; color: white;")
        self.btn_reset.clicked.connect(self.reset_zoom)
        ctrl_bar.addWidget(self.btn_reset)

        ctrl_bar.addWidget(QtWidgets.QLabel(" Wave:"))
        self.wave_combo = QtWidgets.QComboBox()
        self.wave_combo.setFocusPolicy(QtCore.Qt.NoFocus)
        self.wave_combo.addItems(["Sine Wave", "Square Wave", "Triangle Wave"])
        self.wave_combo.setStyleSheet("padding: 6px; font-weight: bold;")
        self.wave_combo.currentIndexChanged.connect(self.on_wave_changed)
        ctrl_bar.addWidget(self.wave_combo)

        self.freq_label = QtWidgets.QLabel(" Freq: 200 Hz")
        self.freq_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        ctrl_bar.addWidget(self.freq_label)

        self.freq_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.freq_slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self.freq_slider.setMinimum(50)
        self.freq_slider.setMaximum(1000)
        self.freq_slider.setValue(200)
        self.freq_slider.setSingleStep(25)
        self.freq_slider.valueChanged.connect(self.on_freq_changed)
        ctrl_bar.addWidget(self.freq_slider)

        layout.addLayout(ctrl_bar)

        self.try_auto_connect()

        self.reconnect_timer = QtCore.QTimer()
        self.reconnect_timer.timeout.connect(self.check_connection)
        self.reconnect_timer.start(1000)

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_scope)
        self.timer.start(16)

    def try_auto_connect(self):
        port = find_esp32_port()
        if not port:
            return
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.ser = serial.Serial(port, BAUD_RATE, timeout=0.25)
            self.ser.reset_input_buffer()
            self.current_port = port
            self.metrics_label.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #00FFAA; "
                "background: #141414; padding: 10px; border: 1px solid #333; border-radius: 5px;"
            )
            self.metrics_label.setText(f"[{port}] Connected — streaming waveform data...")
        except serial.SerialException:
            self.ser = None
            self.current_port = None
            self.metrics_label.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #FF8A80; "
                "background: #141414; padding: 10px; border: 1px solid #333; border-radius: 5px;"
            )
            self.metrics_label.setText(f"[{port}] Port busy — close Arduino Serial Monitor if open")

    def check_connection(self):
        if self.ser is None or not self.ser.is_open:
            self.try_auto_connect()

    def toggle_coupling(self):
        if self.coupling_mode == "DC":
            self.coupling_mode = "AC"
            self.btn_coupling.setText("COUPLING: AC (0V)")
            self.btn_coupling.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #6A1B9A; color: white;")
            self.time_plot.setYRange(-1.85, 1.85, padding=0)
            self.ref_mid.setValue(0.0)
            self.ref_zero.setVisible(False)
            self.ref_top.setVisible(False)
        else:
            self.coupling_mode = "DC"
            self.btn_coupling.setText("COUPLING: DC (0-3.3V)")
            self.btn_coupling.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #EF6C00; color: white;")
            self.time_plot.setYRange(-0.1, 3.45, padding=0)
            self.ref_mid.setValue(1.65)
            self.ref_zero.setVisible(True)
            self.ref_top.setVisible(True)

    def take_screenshot(self):
        save_dir = get_screenshots_dir()
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        wave_tag = self.wave_combo.currentText().split()[0].lower()
        freq_tag = self.freq_slider.value()
        filename = f"ESP32_Scope_{self.coupling_mode}_{wave_tag}_{freq_tag}Hz_{ts}.png"
        full_path = os.path.join(save_dir, filename)

        if self.grab().save(full_path, "PNG"):
            self.status_flash_text = f"SAVED SCREENSHOT -> {full_path}"
            self.status_flash_until = QtCore.QDateTime.currentMSecsSinceEpoch() + 3000
            self.metrics_label.setText(self.status_flash_text)

    def reset_zoom(self):
        self.time_plot.enableAutoRange(axis='x', enable=True)
        if self.coupling_mode == "DC":
            self.time_plot.setYRange(-0.1, 3.45, padding=0)
        else:
            self.time_plot.setYRange(-1.85, 1.85, padding=0)
        self.fft_plot.setXRange(0, 2000, padding=0)
        self.fft_plot.enableAutoRange(axis='y', enable=True)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Space:
            self.reset_zoom()
            event.accept()
        elif event.key() == QtCore.Qt.Key_S:
            self.take_screenshot()
            event.accept()
        else:
            super().keyPressEvent(event)

    def toggle_run(self):
        self.is_running = not self.is_running
        if self.is_running:
            if self.ser and self.ser.is_open:
                self.ser.reset_input_buffer()
            self.btn_run.setText("RUN / FREEZE")
            self.btn_run.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #2E7D32; color: white;")
        else:
            self.btn_run.setText("FROZEN (RESUME)")
            self.btn_run.setStyleSheet("font-weight: bold; padding: 8px 14px; background: #C62828; color: white;")

    def cycle_trigger(self):
        modes = ["RISING", "FALLING", "ROLL"]
        self.trigger_mode = modes[(modes.index(self.trigger_mode) + 1) % len(modes)]
        self.btn_trig.setText(f"TRIGGER: {self.trigger_mode}")

    def on_wave_changed(self, idx):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(f"W{idx}\n".encode('ascii'))
            except serial.SerialException:
                self.ser = None

    def on_freq_changed(self, val):
        self.freq_label.setText(f" Freq: {val} Hz")
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(f"F{val}\n".encode('ascii'))
            except serial.SerialException:
                self.ser = None

    def read_frame(self):
        if self.ser is None or not self.ser.is_open:
            return None
        latest_frame = None
        try:
            while self.ser.in_waiting >= FRAME_PAYLOAD_BYTES + 2:
                if self.ser.read(1) == b'\x55':
                    if self.ser.read(1) == b'\xAA':
                        payload = self.ser.read(FRAME_PAYLOAD_BYTES)
                        if len(payload) == FRAME_PAYLOAD_BYTES:
                            dt_us = struct.unpack('<I', payload[:4])[0]
                            raw_adc = np.frombuffer(payload[4:], dtype=np.uint16)
                            ch1 = raw_adc[:NUM_SAMPLES] * (V_REF / 4095.0)
                            ch2 = raw_adc[NUM_SAMPLES:] * (V_REF / 4095.0)
                            latest_frame = (dt_us, ch1, ch2)
        except (serial.SerialException, OSError):
            self.ser = None
            self.metrics_label.setText("ESP32 disconnected — waiting to reconnect...")
            return None
        return latest_frame

    def update_scope(self):
        if not self.is_running:
            return

        frame = self.read_frame()
        if frame is None:
            return

        dt_us, ch1_raw, ch2_raw = frame
        if dt_us == 0:
            return

        fs = (NUM_SAMPLES * 1e6) / dt_us
        dc1 = np.mean(ch1_raw)
        dc2 = np.mean(ch2_raw)

        if self.coupling_mode == "AC":
            ch1 = ch1_raw - dc1
            ch2 = ch2_raw - dc2
        else:
            ch1 = ch1_raw
            ch2 = ch2_raw

        mid1 = (np.max(ch1) + np.min(ch1)) / 2.0
        trig_idx = 0
        if self.trigger_mode == "RISING":
            for i in range(1, NUM_SAMPLES // 2):
                if ch1[i - 1] < mid1 and ch1[i] >= mid1:
                    trig_idx = i
                    break
        elif self.trigger_mode == "FALLING":
            for i in range(1, NUM_SAMPLES // 2):
                if ch1[i - 1] > mid1 and ch1[i] <= mid1:
                    trig_idx = i
                    break

        disp_len = NUM_SAMPLES // 2
        v1 = ch1[trig_idx : trig_idx + disp_len]
        v2 = ch2[trig_idx : trig_idx + disp_len]
        t_ms = (np.arange(len(v1)) / fs) * 1000.0

        self.curve_ch1.setData(t_ms, v1)
        self.curve_ch2.setData(t_ms, v2)

        win = np.hanning(NUM_SAMPLES)
        fft_freqs = np.fft.rfftfreq(NUM_SAMPLES, d=1.0 / fs)
        mag1 = (2.0 / NUM_SAMPLES) * np.abs(np.fft.rfft((ch1_raw - dc1) * win)) * 2.0
        mag2 = (2.0 / NUM_SAMPLES) * np.abs(np.fft.rfft((ch2_raw - dc2) * win)) * 2.0

        self.fft_ch1.setData(fft_freqs[1:], mag1[1:])
        self.fft_ch2.setData(fft_freqs[1:], mag2[1:])

        vpp1 = np.max(v1) - np.min(v1)
        vpp2 = np.max(v2) - np.min(v2)
        vrms1 = np.sqrt(np.mean((ch1_raw - dc1) ** 2))
        gain = (vpp2 / vpp1) if vpp1 > 0.05 else 0.0

        fund_idx = np.argmax(mag1[1:]) + 1
        freq = fft_freqs[fund_idx] if vpp1 > 0.1 else 0.0

        phase_deg = 0.0
        if vpp1 > 0.1 and vpp2 > 0.1:
            c1_fft = np.fft.rfft(ch1_raw - dc1)[fund_idx]
            c2_fft = np.fft.rfft(ch2_raw - dc2)[fund_idx]
            phase_deg = np.degrees(np.angle(c2_fft / c1_fft))

        if QtCore.QDateTime.currentMSecsSinceEpoch() > self.status_flash_until:
            self.metrics_label.setText(
                f"[{self.current_port}]  CH1: {vpp1:.2f} Vpp (DC: {dc1:.2f}V, {vrms1:.2f} Vrms)   |   "
                f"CH2: {vpp2:.2f} Vpp (DC: {dc2:.2f}V)   |   "
                f"Gain: {gain:.2f}x ({20*np.log10(gain + 1e-6):.1f} dB)   |   "
                f"Phase: {phase_deg:.1f}°   |   Freq: {freq:.1f} Hz   |   Fs: {fs/1000:.1f} kSa/s"
            )

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = OscilloscopeApp()
    win.show()
    sys.exit(app.exec_())                    

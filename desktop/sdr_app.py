#!/usr/bin/env python3
"""
RTL-SDR Blog V4 用 シンプルSDRレシーバー
  - スペクトラム / ウォーターフォール表示（クリック・ドラッグで選局）
  - WFM（FM放送）/ NFM（アマチュア無線など）/ AM（航空無線など）の復調と音声出力
"""
import os
import sys
import threading
import collections
import queue

import numpy as np
from scipy import fft as sfft
from scipy.signal import firwin, fftconvolve, lfilter

# Windows: rtlsdr.dll を、このファイルと同じフォルダ または tools/windows/rtl-sdr-blog-x64 から読む
HERE = os.path.dirname(os.path.abspath(__file__))
if sys.platform == "win32":
    for d in (HERE, os.path.join(HERE, "..", "tools", "windows", "rtl-sdr-blog-x64")):
        d = os.path.normpath(d)
        if os.path.isdir(d):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
import pyqtgraph as pg  # noqa: E402

try:
    from rtlsdr import RtlSdr
    RTLSDR_ERR = None
except Exception as e:  # ドライバ(DLL)が見つからない場合など
    RtlSdr = None
    RTLSDR_ERR = e

try:
    import sounddevice as sd
    SD_ERR = None
except Exception as e:
    sd = None
    SD_ERR = e

VERSION = "1.4（気象衛星対応）"

# ---------------- 定数 ----------------
FS = 2_400_000          # サンプルレート (2.4 MS/s)
BLOCK = 256_000         # 1回の読み込みサンプル数（512バイト境界かつ50の倍数）
OFFSET = 300_000        # DCスパイク回避のため、実際のチューニングをずらす量（FS/OFFSET=8で発振器を使い回せる）
IF_FS = FS // 10        # 240 kHz
AUDIO_FS = IF_FS // 5   # 48 kHz
FFT_N = 2048
WF_ROWS = 300
GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7,
         22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4,
         43.9, 44.5, 48.0, 49.6]
PRESETS = [
    ("プリセットを選択…", None, None),
    ("FM放送 (76〜95MHz)", "WFM", 80.0),
    ("航空無線 (118〜137MHz)", "AM", 124.0),
    ("アマチュア無線 144MHz帯", "NFM", 145.0),
    ("ADS-B 1090MHz（表示のみ）", "AM", 1090.0),
]


# 局サーチの対象バンド: (表示名, モード, 開始Hz, 終了Hz, チャンネル間隔Hz, 測定帯域幅Hz, 検出しきい値dB)
SCAN_BANDS = [
    ("FM放送 76〜95MHz", "WFM", 76.0e6, 95.0e6, 100e3, 150e3, 10.0),
    ("航空無線 118〜137MHz", "AM", 118.0e6, 137.0e6, 25e3, 8e3, 8.0),
    ("アマチュア 144〜146MHz", "NFM", 144.0e6, 146.0e6, 20e3, 12e3, 8.0),
    ("アマチュア 430〜440MHz", "NFM", 430.0e6, 440.0e6, 20e3, 12e3, 8.0),
]
SCAN_STEP = 0.8e6         # サーチ時にハードの中心周波数を動かす間隔
SCAN_USE = (0.1e6, 0.9e6)  # 中心からこの範囲のオフセットだけを測定に使う（DCと帯域端を避ける）


# ---------------- 信号処理 ----------------
class FirDecimator:
    """ブロック間で状態を保つFIRフィルタ＋間引き"""

    def __init__(self, taps, decim):
        self.taps = np.asarray(taps, dtype=np.float32)
        self.decim = decim
        self.tail = None

    def __call__(self, x):
        n = len(self.taps) - 1
        if self.tail is None:
            self.tail = np.zeros(n, dtype=x.dtype)
        buf = np.concatenate((self.tail, x))
        self.tail = buf[-n:]
        y = fftconvolve(buf, self.taps, mode="valid")  # len(y) == len(x)
        return y[::self.decim]


class IIR1:
    """状態付き1次IIR（ディエンファシス、DCカット用）"""

    def __init__(self, b, a):
        self.b, self.a = b, a
        self.zi = np.zeros(max(len(a), len(b)) - 1)

    def __call__(self, x):
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y


# 周波数シフト用の発振器（BLOCKがFS/OFFSETの倍数なので毎回同じものを使える）
OSC = np.exp(-2j * np.pi * OFFSET / FS * np.arange(BLOCK)).astype(np.complex64)
assert BLOCK % (FS // OFFSET) == 0


class Demodulator:
    def __init__(self, mode):
        self.mode = mode
        self.last = np.complex64(0)
        # 2.4 MHz -> 240 kHz
        self.stage1 = FirDecimator(firwin(129, 110e3, fs=FS), 10)
        if mode == "WFM":
            self.audio = FirDecimator(firwin(129, 15e3, fs=IF_FS), 5)
            d = np.exp(-1.0 / (AUDIO_FS * 50e-6))  # 日本のFM放送は50µs
            self.deemph = IIR1([1 - d], [1, -d])
        else:
            cutoff = 5e3 if mode == "AM" else 6e3
            self.chan = FirDecimator(firwin(255, cutoff, fs=IF_FS), 5)
            self.dc = IIR1([1, -1], [1, -0.995])

    def _fm(self, x):
        ext = np.concatenate(([self.last], x))
        self.last = x[-1]
        return np.angle(ext[1:] * np.conj(ext[:-1]))

    def process(self, iq):
        # 目的の周波数（+OFFSET の位置）を 0Hz に移動
        x = self.stage1(iq * OSC)

        if self.mode == "WFM":
            a = self.deemph(self.audio(self._fm(x))) * 0.5
        elif self.mode == "NFM":
            a = self.dc(self._fm(self.chan(x))) * 1.5
        else:  # AM
            env = np.abs(self.chan(x))
            a = self.dc(env / (np.mean(env) + 1e-12)) * 0.8
        return a


class IQRecorder:
    """選局中の周波数を中心に、240kS/s・16bit IQ（SatDumpの s16 形式）でファイルに保存する"""
    RATE = IF_FS

    def __init__(self, path):
        self.lock = threading.Lock()
        self.f = open(path, "wb")
        self.dec = FirDecimator(firwin(129, 110e3, fs=FS), FS // IF_FS)
        self.bytes = 0

    def write(self, iq):
        x = self.dec(iq * OSC)
        out = np.empty(2 * len(x), dtype="<i2")
        out[0::2] = np.clip(x.real * 16384, -32767, 32767)
        out[1::2] = np.clip(x.imag * 16384, -32767, 32767)
        with self.lock:
            if self.f:
                self.f.write(out.tobytes())
                self.bytes += out.nbytes

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None


# ---------------- 音声出力 ----------------
class AudioOut:
    def __init__(self):
        self.lock = threading.Lock()
        self.q = collections.deque()
        self.buffered = 0
        self.volume = 0.5
        self.ready = False   # 一定量たまるまで再生を待つ（プリバッファ）
        self.underruns = 0   # 音声が足りなくなった回数
        self.stream = None
        if sd is not None:
            self.stream = sd.OutputStream(samplerate=AUDIO_FS, channels=1,
                                          dtype="float32", blocksize=2048,
                                          latency="high", callback=self._cb)

    def start(self):
        if self.stream:
            self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
        self.clear()

    def clear(self):
        with self.lock:
            self.q.clear()
            self.buffered = 0
            self.ready = False

    def push(self, a):
        a = np.clip(a * self.volume, -1, 1).astype(np.float32)
        with self.lock:
            self.q.append(a)
            self.buffered += len(a)
            while self.buffered > AUDIO_FS * 0.8 and len(self.q) > 1:  # 遅延が溜まったら捨てる
                self.buffered -= len(self.q.popleft())

    def _cb(self, outdata, frames, time_info, status):
        out = outdata[:, 0]
        i = 0
        with self.lock:
            if not self.ready:
                if self.buffered < AUDIO_FS * 0.3:   # 0.3秒たまるまで無音
                    out[:] = 0
                    return
                self.ready = True
            while i < frames and self.q:
                chunk = self.q[0]
                take = min(frames - i, len(chunk))
                out[i:i + take] = chunk[:take]
                if take == len(chunk):
                    self.q.popleft()
                else:
                    self.q[0] = chunk[take:]
                self.buffered -= take
                i += take
            if i < frames:          # 足りなくなったら再びためてから再生
                self.ready = False
                self.underruns += 1
        out[i:] = 0


# ---------------- SDR受信スレッド ----------------
class SdrWorker(QtCore.QThread):
    spectrum = QtCore.Signal(object, float)  # dB配列, ハードの中心周波数
    failed = QtCore.Signal(str)
    scan_progress = QtCore.Signal(int)       # 0〜100
    scan_done = QtCore.Signal(object, str)   # [(周波数Hz, 強さdB), ...], モード

    def __init__(self, audio, freq, mode, gain):
        super().__init__()
        self.audio = audio
        self.lock = threading.Lock()
        self.freq, self.mode, self.gain = freq, mode, gain
        self._dirty = True
        self._running = True
        self.dropped = 0   # 処理が追いつかず捨てたブロック数
        self._scan_req = None
        self._scan_cancel = False
        self.recorder = None   # IQRecorder（衛星録音中のみ）

    def request_scan(self, band):
        with self.lock:
            self._scan_cancel = False
            self._scan_req = band

    def cancel_scan(self):
        self._scan_cancel = True

    def set_params(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self._dirty = True

    def stop(self):
        self._running = False

    def run(self):
        try:
            sdr = RtlSdr()
        except Exception as e:
            self.failed.emit(f"RTL-SDRを開けませんでした。\n接続とドライバを確認してください。\n\n{e}")
            return

        # V4からのデータを途切れなく受け取る受信専用スレッド（非同期読み込み）
        blocks = queue.Queue(maxsize=20)

        def on_bytes(values, _ctx):
            data = np.frombuffer(values, dtype=np.uint8).copy()
            try:
                blocks.put_nowait(data)
            except queue.Full:          # 処理が追いつかないときだけ捨てる
                self.dropped += 1

        def reader():
            try:
                sdr.read_bytes_async(on_bytes, BLOCK * 2)
            except Exception as e:
                if self._running:
                    self.failed.emit(f"受信中にエラーが発生しました。\n\n{e}")
                    self._running = False

        reader_thread = None
        try:
            sdr.sample_rate = FS
            with self.lock:
                self._dirty = False
                freq, mode, gain = self.freq, self.mode, self.gain
            hw = freq - OFFSET
            sdr.center_freq = hw
            sdr.gain = gain
            demod, cur_mode = Demodulator(mode), mode

            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()

            win = np.hanning(FFT_N).astype(np.float32)
            win_pow = float(np.sum(win ** 2))
            while self._running:
                with self.lock:
                    dirty, self._dirty = self._dirty, False
                    freq, mode, gain = self.freq, self.mode, self.gain
                if dirty:
                    if freq - OFFSET != hw:
                        hw = freq - OFFSET
                        sdr.center_freq = hw
                    sdr.gain = gain
                    if mode != cur_mode:
                        demod, cur_mode = Demodulator(mode), mode
                    self.audio.clear()

                with self.lock:
                    scan, self._scan_req = self._scan_req, None
                if scan:
                    self.audio.clear()
                    found = self._scan(sdr, blocks, win, win_pow, scan)
                    sdr.center_freq = hw            # 元の周波数に戻す
                    self._drain(blocks)
                    demod = Demodulator(cur_mode)
                    self.audio.clear()
                    self.scan_done.emit(found, scan[1])
                    continue

                try:
                    raw = blocks.get(timeout=1.0)
                except queue.Empty:
                    continue
                iq = ((raw.astype(np.float32) - 127.5) / 127.5).view(np.complex64)
                if len(iq) != BLOCK:
                    continue

                rec = self.recorder
                if rec:
                    rec.write(iq)

                # スペクトラムは間引いて計算（音声処理を優先）
                frames = iq[:16 * FFT_N].reshape(16, FFT_N) * win
                spec = sfft.fft(frames, axis=1)
                p = np.mean(spec.real ** 2 + spec.imag ** 2, axis=0) / win_pow
                db = 10 * np.log10(sfft.fftshift(p) + 1e-12)
                self.spectrum.emit(db.astype(np.float32), hw)

                self.audio.push(demod.process(iq))
        except Exception as e:
            self.failed.emit(f"受信中にエラーが発生しました。\n\n{e}")
        finally:
            try:
                sdr.cancel_read_async()
            except Exception:
                pass
            if reader_thread:
                reader_thread.join(timeout=3)
            sdr.close()


    @staticmethod
    def _drain(blocks):
        while True:
            try:
                blocks.get_nowait()
            except queue.Empty:
                return

    def _scan(self, sdr, blocks, win, win_pow, band):
        _, mode, f_start, f_stop, step, bw, thresh = band
        chans = np.arange(round(f_start / step), round(f_stop / step) + 1) * step
        level = np.full(len(chans), -np.inf)
        centers = np.arange(f_start - SCAN_STEP / 2, f_stop + SCAN_STEP, SCAN_STEP)
        offsets = np.fft.fftshift(np.fft.fftfreq(FFT_N, 1 / FS))
        n_fr = BLOCK // FFT_N

        for k, c in enumerate(centers):
            if not self._running or self._scan_cancel:
                return []
            sdr.center_freq = c
            self._drain(blocks)
            try:
                blocks.get(timeout=1.0)              # 切り替え直後のブロックは捨てる
                raw = blocks.get(timeout=1.0)
            except queue.Empty:
                continue
            iq = ((raw.astype(np.float32) - 127.5) / 127.5).view(np.complex64)
            spec = sfft.fft(iq[:n_fr * FFT_N].reshape(n_fr, FFT_N) * win, axis=1)
            p = sfft.fftshift(np.mean(spec.real ** 2 + spec.imag ** 2, axis=0) / win_pow)
            freqs = c + offsets

            d = np.abs(chans - c)
            for i in np.where((d >= SCAN_USE[0]) & (d <= SCAN_USE[1]))[0]:
                lo = np.searchsorted(freqs, chans[i] - bw / 2)
                hi = max(lo + 1, np.searchsorted(freqs, chans[i] + bw / 2))
                level[i] = max(level[i], 10 * np.log10(np.mean(p[lo:hi]) + 1e-12))
            self.scan_progress.emit(int((k + 1) * 100 / len(centers)))

        ok = np.isfinite(level)
        if not ok.any():
            return []
        snr = level - np.median(level[ok])          # 空きチャンネルの多さを利用して雑音レベルを推定
        guard = 0.15e6 if mode == "WFM" else max(1.5 * step, bw)
        found = []
        for i in np.argsort(-snr):                   # 強い順に、近くの重複を除いて採用
            if not ok[i] or snr[i] < thresh:
                break
            if all(abs(chans[i] - f) > guard for f, _ in found):
                found.append((float(chans[i]), float(snr[i])))
        return sorted(found)


# ---------------- 画面 ----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"RTL-SDR V4 レシーバー  v{VERSION}")
        self.resize(1100, 760)
        self.audio = AudioOut()
        self.worker = None
        self.hw = None
        self.avg = None
        self.wf = np.full((WF_ROWS, FFT_N), -100, dtype=np.float32)
        self.offsets = np.fft.fftshift(np.fft.fftfreq(FFT_N, 1 / FS))

        # --- 操作パネル ---
        self.freq = QtWidgets.QDoubleSpinBox()
        self.freq.setRange(0.5, 1766.0)
        self.freq.setDecimals(3)
        self.freq.setSingleStep(0.1)
        self.freq.setSuffix(" MHz")
        self.freq.setKeyboardTracking(False)
        self.freq.setValue(80.0)
        self.freq.setMinimumWidth(140)

        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["WFM", "NFM", "AM"])

        self.gain = QtWidgets.QComboBox()
        self.gain.addItem("自動")
        self.gain.addItems([f"{g:.1f} dB" for g in GAINS])
        self.gain.setCurrentIndex(GAINS.index(29.7) + 1)

        self.vol = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(50)
        self.vol.setMaximumWidth(140)

        self.preset = QtWidgets.QComboBox()
        for name, _, _ in PRESETS:
            self.preset.addItem(name)

        self.run_btn = QtWidgets.QPushButton("▶ 受信開始")
        self.run_btn.setCheckable(True)
        self.run_btn.setMinimumWidth(120)

        bar = QtWidgets.QHBoxLayout()
        for label, w in [("周波数", self.freq), ("モード", self.mode), ("ゲイン", self.gain),
                         ("音量", self.vol), (None, self.preset)]:
            if label:
                bar.addWidget(QtWidgets.QLabel(label))
            bar.addWidget(w)
            bar.addSpacing(10)
        bar.addStretch()
        bar.addWidget(self.run_btn)

        # --- 局サーチ ---
        self.scan_band = QtWidgets.QComboBox()
        for b in SCAN_BANDS:
            self.scan_band.addItem(b[0])
        self.scan_btn = QtWidgets.QPushButton("📡 局サーチ")
        self.prev_btn = QtWidgets.QPushButton("◀ 前の局")
        self.next_btn = QtWidgets.QPushButton("次の局 ▶")
        bar2 = QtWidgets.QHBoxLayout()
        bar2.addWidget(QtWidgets.QLabel("サーチ範囲"))
        bar2.addWidget(self.scan_band)
        bar2.addWidget(self.scan_btn)
        bar2.addSpacing(20)
        bar2.addWidget(self.prev_btn)
        bar2.addWidget(self.next_btn)
        bar2.addStretch()
        self.sat_btn = QtWidgets.QPushButton("🛰 気象衛星")
        bar2.addWidget(self.sat_btn)
        self.sat_window = None

        self.station_list = QtWidgets.QListWidget()
        self.station_list.setMinimumWidth(200)
        station_box = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(station_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(QtWidgets.QLabel("見つかった局（クリックで選局）"))
        sl.addWidget(self.station_list)
        self.stations = []        # [(周波数Hz, 強さdB)]
        self.scan_mode = None
        self.scanning = False

        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

        # --- グラフ ---
        pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
        self.spec_plot = pg.PlotWidget()
        self.spec_plot.setLabel("left", "強度", units="dB")
        self.spec_plot.setMouseEnabled(x=True, y=False)
        self.spec_plot.showGrid(x=True, y=True, alpha=0.25)
        self.curve = self.spec_plot.plot(pen=pg.mkPen("#4fc3f7", width=1))
        self.marker = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#ff5252", width=2))
        self.spec_plot.addItem(self.marker)

        self.wf_plot = pg.PlotWidget()
        self.wf_plot.setLabel("bottom", "周波数", units="MHz")
        self.wf_plot.setMouseEnabled(x=True, y=False)
        self.wf_plot.hideAxis("left")
        self.wf_plot.setXLink(self.spec_plot)
        self.img = pg.ImageItem()
        self.img.setLookupTable(pg.colormap.get("inferno").getLookupTable(nPts=256))
        self.wf_plot.addItem(self.img)
        self.wf_marker = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ff5252", width=1, style=QtCore.Qt.DashLine))
        self.wf_plot.addItem(self.wf_marker)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(self.spec_plot)
        split.addWidget(self.wf_plot)
        split.setSizes([300, 420])

        hint = QtWidgets.QLabel("グラフをクリック、または赤い線をドラッグして選局できます。")
        hint.setStyleSheet("color: gray;")

        hsplit = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        hsplit.addWidget(split)
        hsplit.addWidget(station_box)
        hsplit.setStretchFactor(0, 1)
        hsplit.setSizes([880, 220])

        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.addLayout(bar)
        lay.addLayout(bar2)
        lay.addWidget(hsplit, 1)
        lay.addWidget(hint)
        self.setCentralWidget(central)
        self.statusBar().showMessage("停止中")

        # --- イベント ---
        self.freq.valueChanged.connect(self.apply_params)
        self.mode.currentIndexChanged.connect(self.apply_params)
        self.gain.currentIndexChanged.connect(self.apply_params)
        self.vol.valueChanged.connect(lambda v: setattr(self.audio, "volume", v / 100))
        self.preset.currentIndexChanged.connect(self.apply_preset)
        self.run_btn.toggled.connect(self.toggle_run)
        self.scan_btn.clicked.connect(self.toggle_scan)
        self.sat_btn.clicked.connect(self.open_satellite)
        self.prev_btn.clicked.connect(lambda: self.seek(-1))
        self.next_btn.clicked.connect(lambda: self.seek(+1))
        self.station_list.itemClicked.connect(
            lambda item: self.goto_station(self.station_list.row(item)))
        self.marker.sigPositionChangeFinished.connect(lambda: self.tune_to(self.marker.value()))
        for plot in (self.spec_plot, self.wf_plot):
            plot.scene().sigMouseClicked.connect(lambda ev, p=plot: self.on_click(ev, p))

        self.status_timer = QtCore.QTimer(self)
        self.status_timer.timeout.connect(lambda: self.worker and self.update_status())
        self.status_timer.start(1000)

        self.update_marker()
        if SD_ERR:
            self.statusBar().showMessage(f"音声出力が使えません（{SD_ERR}）")

    # --- 設定 ---
    def current_gain(self):
        i = self.gain.currentIndex()
        return "auto" if i == 0 else GAINS[i - 1]

    def apply_params(self):
        self.update_marker()
        if self.worker:
            self.worker.set_params(freq=self.freq.value() * 1e6,
                                   mode=self.mode.currentText(),
                                   gain=self.current_gain())
            self.update_status()

    def apply_preset(self, idx):
        _, mode, f = PRESETS[idx]
        if mode:
            self.mode.setCurrentText(mode)
            self.freq.setValue(f)
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(0)
        self.preset.blockSignals(False)

    def tune_to(self, mhz):
        step = 0.1 if self.mode.currentText() == "WFM" else 0.005
        self.freq.setValue(round(mhz / step) * step)

    def on_click(self, ev, plot):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        vb = plot.getViewBox()
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        self.tune_to(vb.mapSceneToView(ev.scenePos()).x())

    def update_marker(self):
        f = self.freq.value()
        self.marker.setValue(f)
        self.wf_marker.setValue(f)

    # --- 気象衛星 ---
    def open_satellite(self):
        if self.sat_window is None:
            import satellite
            self.sat_window = satellite.SatelliteWindow(self)
        self.sat_window.show()
        self.sat_window.raise_()

    def start_recording(self, freq_hz, path):
        """衛星ウィンドウから呼ばれる。受信を開始し、指定周波数で録音を始める"""
        if self.scanning:
            return "局サーチ中は録音できません"
        if not self.worker:
            self.run_btn.setChecked(True)
            if not self.worker:
                return "SDRを開始できませんでした"
        self.freq.setValue(freq_hz / 1e6)
        self.apply_params()
        self.worker.recorder = IQRecorder(path)
        for w in (self.freq, self.scan_btn, self.prev_btn, self.next_btn, self.preset, self.run_btn):
            w.setEnabled(False)
        self.station_list.setEnabled(False)
        return None

    def stop_recording(self):
        rec = self.worker.recorder if self.worker else None
        if self.worker:
            self.worker.recorder = None
        if rec:
            rec.close()
        for w in (self.freq, self.scan_btn, self.prev_btn, self.next_btn, self.preset, self.run_btn):
            w.setEnabled(True)
        self.station_list.setEnabled(True)
        return rec.bytes if rec else 0

    # --- 局サーチ ---
    def toggle_scan(self):
        if self.scanning:
            if self.worker:
                self.worker.cancel_scan()
            return
        if not self.worker:
            self.run_btn.setChecked(True)     # 受信していなければ開始する
            if not self.worker:
                return
        band = SCAN_BANDS[self.scan_band.currentIndex()]
        self.scanning = True
        self.scan_btn.setText("■ サーチ中止")
        self.progress.setValue(0)
        self.progress.show()
        self.statusBar().showMessage(f"サーチ中… {band[0]}")
        self.worker.request_scan(band)

    def on_scan_progress(self, pct):
        self.progress.setValue(pct)

    def on_scan_done(self, found, mode):
        self.scanning = False
        self.scan_btn.setText("📡 局サーチ")
        self.progress.hide()
        self.stations, self.scan_mode = found, mode
        self.station_list.clear()
        for f, snr in found:
            self.station_list.addItem(f"{f / 1e6:8.3f} MHz   +{snr:4.1f} dB")
        if found:
            best = max(range(len(found)), key=lambda i: found[i][1])
            self.goto_station(best)
            self.statusBar().showMessage(f"{len(found)}局見つかりました", 5000)
        else:
            self.statusBar().showMessage("局が見つかりませんでした（中止、または電波が弱い）", 5000)

    def goto_station(self, i):
        if not (0 <= i < len(self.stations)):
            return
        self.station_list.setCurrentRow(i)
        if self.scan_mode:
            self.mode.setCurrentText(self.scan_mode)
        self.freq.setValue(self.stations[i][0] / 1e6)

    def seek(self, direction):
        if not self.stations:
            self.statusBar().showMessage("先に「局サーチ」を実行してください", 3000)
            return
        cur = self.freq.value() * 1e6
        fs = [f for f, _ in self.stations]
        if direction > 0:
            idx = next((i for i, f in enumerate(fs) if f > cur + 1), 0)
        else:
            idx = next((i for i in reversed(range(len(fs))) if fs[i] < cur - 1), len(fs) - 1)
        self.goto_station(idx)

    def update_status(self):
        if self.scanning:
            return
        msg = f"受信中  {self.freq.value():.3f} MHz  {self.mode.currentText()}  ゲイン {self.gain.currentText()}"
        if self.worker:
            msg += f"  ／ 音切れ {self.audio.underruns}回  処理落ち {self.worker.dropped}回"
        if SD_ERR:
            msg += f"  ⚠ 音声出力が使えません（{SD_ERR}）"
        self.statusBar().showMessage(msg)

    # --- 開始 / 停止 ---
    def toggle_run(self, on):
        if on:
            if RtlSdr is None:
                QtWidgets.QMessageBox.critical(
                    self, "ドライバが見つかりません",
                    f"rtlsdr ライブラリを読み込めませんでした。\n"
                    f"Windows では setup_windows.bat を実行してから起動してください（docs/install-windows.md）。\n\n{RTLSDR_ERR}")
                self.run_btn.setChecked(False)
                return
            self.worker = SdrWorker(self.audio, self.freq.value() * 1e6,
                                    self.mode.currentText(), self.current_gain())
            self.worker.spectrum.connect(self.on_spectrum)
            self.worker.failed.connect(self.on_failed)
            self.worker.scan_progress.connect(self.on_scan_progress)
            self.worker.scan_done.connect(self.on_scan_done)
            self.audio.underruns = 0
            self.worker.start()
            self.audio.start()
            self.run_btn.setText("■ 停止")
            self.update_status()
        else:
            self.stop_worker()
            self.run_btn.setText("▶ 受信開始")
            self.statusBar().showMessage("停止中")

    def stop_worker(self):
        if self.scanning:
            self.scanning = False
            self.scan_btn.setText("📡 局サーチ")
            self.progress.hide()
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
        self.audio.stop()

    def on_failed(self, msg):
        QtWidgets.QMessageBox.critical(self, "エラー", msg)
        self.run_btn.setChecked(False)

    # --- 描画 ---
    def on_spectrum(self, db, hw):
        x = (hw + self.offsets) / 1e6
        if hw != self.hw:  # 周波数が変わったら表示範囲をリセット
            self.hw = hw
            self.avg = db.copy()
            self.wf[:] = np.median(db)
            self.spec_plot.setXRange(x[0], x[-1], padding=0)
            floor = float(np.median(db))
            self.spec_plot.setYRange(floor - 10, floor + 60, padding=0)
            self.img.setRect(QtCore.QRectF(x[0], 0, x[-1] - x[0], WF_ROWS))
            self.wf_plot.setYRange(0, WF_ROWS, padding=0)
        else:
            self.avg = 0.6 * self.avg + 0.4 * db
        self.curve.setData(x, self.avg)

        self.wf[:-1] = self.wf[1:]
        self.wf[-1] = db
        floor = float(np.median(db))
        self.img.setImage(self.wf, autoLevels=False, levels=(floor - 3, floor + 40))

    def closeEvent(self, ev):
        if self.sat_window:
            self.sat_window.shutdown()
        self.stop_recording()
        self.stop_worker()
        super().closeEvent(ev)


def main():
    app = QtWidgets.QApplication(sys.argv)
    families = QtGui.QFontDatabase.families()
    for name in ("BIZ UDPGothic", "BIZ UDPゴシック"):
        if name in families:
            app.setFont(QtGui.QFont(name, 10))
            break
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

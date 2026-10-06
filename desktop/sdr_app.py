#!/usr/bin/env python3
"""
RTL-SDR Blog V4 用 シンプルSDRレシーバー
  - スペクトラム / ウォーターフォール表示（クリック・ドラッグで選局）
  - WFM（FM放送）/ NFM（アマチュア無線など）/ AM（航空無線など）の復調と音声出力
"""
import os
import sys
import threading
import time
import collections
import queue

import numpy as np
from scipy import fft as sfft
from scipy.signal import firwin, fftconvolve, lfilter
from scipy.ndimage import median_filter

# Windows: rtlsdr.dll を、このファイルと同じフォルダ または tools/windows/rtl-sdr-blog-x64 から読む
HERE = os.path.dirname(os.path.abspath(__file__))
if sys.platform == "win32":
    for d in (HERE, os.path.join(HERE, "..", "tools", "windows", "rtl-sdr-blog-x64")):
        d = os.path.normpath(d)
        if os.path.isdir(d):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

from PySide6 import QtCore, QtWidgets  # noqa: E402
import pyqtgraph as pg  # noqa: E402

import theme  # noqa: E402

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

VERSION = "1.5（ADS-B対応）"

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
    ("盗聴器チェック A 398.605MHz", "NFM", 398.605),
    ("盗聴器チェック B 399.030MHz", "NFM", 399.030),
    ("盗聴器チェック C 399.455MHz", "NFM", 399.455),
]


# 局サーチの対象バンド: (表示名, モード, 開始Hz, 終了Hz, チャンネル間隔Hz, 測定帯域幅Hz, 検出しきい値dB)
SCAN_BANDS = [
    ("市民ラジオ(CB) 26.968〜27.144MHz", "AM", 26.968e6, 27.144e6, 8e3, 6e3, 8.0),
    ("FM放送 76〜95MHz", "WFM", 76.0e6, 95.0e6, 100e3, 150e3, 6.0),
    ("航空無線 118〜137MHz", "AM", 118.0e6, 137.0e6, 25e3, 8e3, 8.0),
    ("アマチュア 144〜146MHz", "NFM", 144.0e6, 146.0e6, 20e3, 12e3, 8.0),
    ("国際VHF(船舶) 156〜162MHz", "NFM", 156.025e6, 162.025e6, 25e3, 12e3, 8.0),
    ("盗聴器 398.4〜399.7MHz", "NFM", 398.4e6, 399.7e6, 5e3, 12e3, 8.0),
    ("特定小電力 421.575〜422.3MHz", "NFM", 421.575e6, 422.3e6, 12.5e3, 8e3, 8.0),
    ("アマチュア 430〜440MHz", "NFM", 430.0e6, 440.0e6, 20e3, 12e3, 8.0),
]
SCAN_STEP = 0.8e6         # サーチ時にハードの中心周波数を動かす間隔
SCAN_USE = (0.1e6, 0.9e6)  # 中心からこの範囲のオフセットだけを測定に使う（DCと帯域端を避ける）
SCAN_FLOOR_SPAN = 1.5e6   # 雑音レベルは各チャンネルの前後この範囲の中央値で推定する
SCAN_LOG = os.path.join(HERE, "scan_last.csv")   # 直近のサーチの測定値（うまく見つからないときの確認用）
SCAN_SETTLE_SEC = 0.3     # 中心周波数を変えてから、この時間が経つまでのデータは捨てる
SCAN_SKIP_BLOCKS = 3      # 同じく、少なくともこのブロック数は捨てる
SCAN_AVG_BLOCKS = 2       # 1ステップで平均するブロック数
SCAN_SEC_PER_STEP = 0.55  # 1ステップあたりのおおよその所要時間（所要時間の目安表示用）
SCAN_WARN_HZ = 50e6       # これより広い範囲は確認してからサーチする
# カスタム範囲のモード別既定値: (ch間隔Hz, 測定帯域幅Hz, 検出しきい値dB)
SCAN_MODE_DEFAULTS = {"WFM": (100e3, 150e3, 6.0), "NFM": (12.5e3, 12e3, 8.0), "AM": (25e3, 8e3, 8.0)}
SCAN_CH_STEPS = [8e3, 9e3, 10e3, 12.5e3, 20e3, 25e3, 50e3, 100e3, 200e3]


def scan_steps(band):
    """サーチで中心周波数を動かす回数"""
    return int(np.ceil((band[3] - band[2] + 1.5 * SCAN_STEP) / SCAN_STEP))


def scan_eta(band):
    """「Nステップ・約M秒/分」の目安"""
    n = scan_steps(band)
    sec = n * SCAN_SEC_PER_STEP
    return f"{n}ステップ・約{max(1, round(sec))}秒" if sec < 90 else f"{n}ステップ・約{round(sec / 60)}分"


def custom_band(mode, f_start, f_stop, step):
    """カスタム範囲のバンド定義を作る。測定帯域幅としきい値はモード別の既定値を使う"""
    _, bw, thresh = SCAN_MODE_DEFAULTS[mode]
    if mode != "WFM":
        bw = min(bw, 0.8 * step)    # 隣のチャンネルを拾わないよう、ch間隔より狭くする
    name = f"カスタム {f_start / 1e6:g}〜{f_stop / 1e6:g}MHz {mode}"
    return (name, mode, f_start, f_stop, step, bw, thresh)


class CustomScanDialog(QtWidgets.QDialog):
    """局サーチのカスタム範囲（開始・終了周波数、モード、ch間隔）を入力する"""

    def __init__(self, parent, band=None):
        super().__init__(parent)
        self.setWindowTitle("カスタム範囲")
        self.start = QtWidgets.QDoubleSpinBox()
        self.stop = QtWidgets.QDoubleSpinBox()
        for s in (self.start, self.stop):
            s.setRange(1.0, 1766.0)
            s.setDecimals(3)
            s.setSuffix(" MHz")
            s.setKeyboardTracking(False)
        self.mode = theme.Segmented(["WFM", "NFM", "AM"])
        self.step = QtWidgets.QComboBox()
        for st in SCAN_CH_STEPS:
            self.step.addItem(f"{st / 1e3:g} kHz", st)
        self.info = theme.caption("", "hint")

        if band:
            _, mode, f_start, f_stop, step = band[:5]
        else:
            mode, f_start, f_stop, step = "NFM", 430e6, 440e6, SCAN_MODE_DEFAULTS["NFM"][0]
        self.mode.setCurrentText(mode)
        self.start.setValue(f_start / 1e6)
        self.stop.setValue(f_stop / 1e6)
        self.step.setCurrentIndex(SCAN_CH_STEPS.index(step))

        form = QtWidgets.QFormLayout()
        form.addRow("開始", self.start)
        form.addRow("終了", self.stop)
        form.addRow("モード", self.mode)
        form.addRow("ch間隔", self.step)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.ok = buttons.button(QtWidgets.QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.info)
        lay.addWidget(buttons)

        self.mode.currentIndexChanged.connect(self.on_mode)
        for s in (self.start, self.stop):
            s.valueChanged.connect(self.update_info)
        self.step.currentIndexChanged.connect(self.update_info)
        self.update_info()

    def on_mode(self):
        self.step.setCurrentIndex(SCAN_CH_STEPS.index(SCAN_MODE_DEFAULTS[self.mode.currentText()][0]))
        self.update_info()

    def band(self):
        return custom_band(self.mode.currentText(), self.start.value() * 1e6,
                           self.stop.value() * 1e6, self.step.currentData())

    def update_info(self):
        b = self.band()
        if b[3] - b[2] < b[4]:
            self.info.setText("終了周波数は開始周波数より ch間隔以上 大きくしてください")
            self.ok.setEnabled(False)
            return
        self.ok.setEnabled(True)
        text = scan_eta(b)
        if b[3] - b[2] > SCAN_WARN_HZ:
            text += f"\n⚠ {SCAN_WARN_HZ / 1e6:g}MHzを超える範囲は時間がかかります"
        self.info.setText(text)


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
        self.device = None   # 出力デバイス番号（None = OSの既定）
        self.running = False
        self.stream = None
        self.error = None
        self._open()

    def _open(self):
        self.stream, self.error = None, None
        if sd is None:
            return
        try:
            self.stream = sd.OutputStream(device=self.device, samplerate=AUDIO_FS, channels=1,
                                          dtype="float32", blocksize=2048,
                                          latency="high", callback=self._cb)
        except Exception as e:
            self.error = str(e)

    @staticmethod
    def outputs():
        """選べる出力デバイス [(番号, 名前)]。OS既定のホストAPI（WindowsはMME）のものだけ"""
        if sd is None:
            return []
        try:
            api = sd.default.hostapi
            return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                    if d["max_output_channels"] > 0 and d["hostapi"] == api]
        except Exception:
            return []

    def set_device(self, device):
        """出力先を切り替える。開けなければ既定に戻してエラー文を返す"""
        if self.stream:
            self.stream.stop()
            self.stream.close()
        self.device = device
        self._open()
        err = self.error
        if err and device is not None:
            self.device = None
            self._open()
        if self.running and self.stream:
            self.clear()
            self.stream.start()
        return err

    def start(self):
        self.running = True
        if self.stream:
            self.stream.start()

    def stop(self):
        self.running = False
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
            # USB側には切り替え前のデータが溜まっているので、時間とブロック数の両方で十分に捨ててから測る
            t0 = time.monotonic()
            try:
                skipped = 0
                while skipped < SCAN_SKIP_BLOCKS or time.monotonic() - t0 < SCAN_SETTLE_SEC:
                    blocks.get(timeout=1.0)
                    skipped += 1
                raws = [blocks.get(timeout=1.0) for _ in range(SCAN_AVG_BLOCKS)]
            except queue.Empty:
                continue
            p = 0
            for raw in raws:
                iq = ((raw.astype(np.float32) - 127.5) / 127.5).view(np.complex64)
                spec = sfft.fft(iq[:n_fr * FFT_N].reshape(n_fr, FFT_N) * win, axis=1)
                p = p + np.mean(spec.real ** 2 + spec.imag ** 2, axis=0) / win_pow
            p = sfft.fftshift(p / len(raws))
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
        # 雑音レベルは近くのチャンネルの中央値で推定する（空きチャンネルの方が多いことを利用）。
        # 範囲全体の中央値にしないのは、V4のFMノッチ（85MHz未満でオン）や測定位置で
        # 雑音レベルが場所ごとに変わり、弱い側の局がしきい値を超えなくなるため
        filled = np.where(ok, level, np.median(level[ok]))
        size = 2 * max(1, int(SCAN_FLOOR_SPAN / step)) + 1
        floor = median_filter(filled, size=size, mode="nearest")
        snr = level - floor
        self._save_scan_log(chans, level, floor, snr)
        guard = 0.15e6 if mode == "WFM" else max(1.5 * step, bw)
        found = []
        for i in np.argsort(-snr):                   # 強い順に、近くの重複を除いて採用
            if not ok[i] or snr[i] < thresh:
                break
            if all(abs(chans[i] - f) > guard for f, _ in found):
                found.append((float(chans[i]), float(snr[i])))
        return sorted(found)

    @staticmethod
    def _save_scan_log(chans, level, floor, snr):
        try:
            with open(SCAN_LOG, "w", encoding="utf-8") as f:
                f.write("freq_mhz,level_db,floor_db,snr_db\n")
                for row in zip(chans / 1e6, level, floor, snr):
                    f.write("%.4f,%.1f,%.1f,%.1f\n" % row)
        except OSError:
            pass


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

        # --- チューナーパネル ---
        self.freq = QtWidgets.QDoubleSpinBox()
        self.freq.setObjectName("FreqSpin")
        self.freq.setRange(0.5, 1766.0)
        self.freq.setDecimals(3)
        self.freq.setSingleStep(0.1)
        self.freq.setKeyboardTracking(False)
        self.freq.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.freq.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.freq.setFont(theme.font(theme.MONO_FONTS, 24, bold=True))
        self.freq.setMinimumWidth(170)
        self.freq.setValue(80.0)
        self.freq.setToolTip("数値を入力して Enter、またはホイールで選局")
        self.down_btn = QtWidgets.QPushButton("−")
        self.up_btn = QtWidgets.QPushButton("＋")
        for b in (self.down_btn, self.up_btn):
            b.setProperty("kind", "step")
            b.setAutoRepeat(True)
        mhz = QtWidgets.QLabel("MHz")
        mhz.setProperty("role", "heading")
        freq_row = QtWidgets.QHBoxLayout()
        freq_row.setSpacing(6)
        freq_row.addWidget(self.down_btn)
        freq_row.addWidget(self.freq)
        freq_row.addWidget(mhz)
        freq_row.addWidget(self.up_btn)

        self.mode = theme.Segmented(["WFM", "NFM", "AM"])

        self.gain = QtWidgets.QComboBox()
        self.gain.addItem("自動")
        self.gain.addItems([f"{g:.1f} dB" for g in GAINS])
        self.gain.setCurrentIndex(GAINS.index(29.7) + 1)

        self.vol = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(50)
        self.vol.setFixedWidth(120)
        self.vol_label = QtWidgets.QLabel("50%")
        self.vol_label.setProperty("role", "caption")
        self.vol_label.setMinimumWidth(34)
        vol_row = QtWidgets.QHBoxLayout()
        vol_row.addWidget(self.vol)
        vol_row.addWidget(self.vol_label)

        self.preset = QtWidgets.QComboBox()
        for name, _, _ in PRESETS:
            self.preset.addItem(name)

        self.run_btn = QtWidgets.QPushButton("▶ 受信開始")
        self.run_btn.setProperty("kind", "primary")
        self.run_btn.setCheckable(True)
        self.run_btn.setMinimumWidth(130)
        self.run_btn.setMinimumHeight(40)

        self.state_label = QtWidgets.QLabel()
        self.state_label.setAlignment(QtCore.Qt.AlignCenter)

        # キャプション行と部品行に分けたグリッドで、各項目の高さを揃える
        tuner = theme.panel()
        grid = QtWidgets.QGridLayout(tuner)
        grid.setContentsMargins(16, 10, 16, 12)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(2)
        items = (("周波数", freq_row), ("モード", self.mode), ("ゲイン", self.gain),
                 ("音量", vol_row), ("プリセット", self.preset))
        for col, (label, w) in enumerate(items):
            grid.addWidget(theme.caption(label), 0, col)
            if isinstance(w, QtWidgets.QLayout):
                grid.addLayout(w, 1, col, QtCore.Qt.AlignVCenter)
            else:
                grid.addWidget(w, 1, col, QtCore.Qt.AlignVCenter)
        grid.setColumnStretch(len(items), 1)
        grid.addWidget(self.state_label, 0, len(items) + 1)
        grid.addWidget(self.run_btn, 1, len(items) + 1, QtCore.Qt.AlignVCenter)

        # --- 局サーチ ---
        self.scan_band = QtWidgets.QComboBox()
        for b in SCAN_BANDS:
            self.scan_band.addItem(b[0], b)
        self.scan_band.addItem("カスタム…", None)
        self.scan_band.setToolTip("「カスタム…」で開始・終了周波数とモードを指定できます")
        self.scan_band_idx = self.scan_band.currentIndex()   # カスタム入力をやめたときに戻す先
        self.scan_btn = QtWidgets.QPushButton("📡 局サーチ")
        self.prev_btn = QtWidgets.QPushButton("◀ 前の局")
        self.next_btn = QtWidgets.QPushButton("次の局 ▶")
        bar2 = QtWidgets.QHBoxLayout()
        bar2.setSpacing(8)
        bar2.addWidget(theme.caption("サーチ範囲"))
        bar2.addWidget(self.scan_band)
        bar2.addWidget(self.scan_btn)
        bar2.addSpacing(16)
        bar2.addWidget(self.prev_btn)
        bar2.addWidget(self.next_btn)
        bar2.addStretch()
        self.out_dev = QtWidgets.QComboBox()
        self.out_dev.addItem("OSの既定", None)
        for i, name in AudioOut.outputs():
            self.out_dev.addItem(name, i)
        self.out_dev.setToolTip("音が聞こえないときは、スピーカー／ヘッドホンを選び直してください")
        self.out_dev.setMinimumWidth(200)
        self.out_dev.setMaximumWidth(300)
        self.out_dev.setEnabled(sd is not None)
        bar2.addWidget(theme.caption("音声出力"))
        bar2.addWidget(self.out_dev)
        bar2.addSpacing(16)
        self.sat_btn = QtWidgets.QPushButton("🛰 気象衛星")
        bar2.addWidget(self.sat_btn)
        self.sat_window = None
        self.adsb_btn = QtWidgets.QPushButton("✈ ADS-B")
        self.adsb_btn.setToolTip("航空機の位置を地図に表示します（受信中は SDR を ADS-B 専用で使います）")
        bar2.addWidget(self.adsb_btn)
        self.adsb_window = None
        self.region_btn = QtWidgets.QPushButton("🗾 地域統計")
        self.region_btn.setToolTip("人口・世帯・住宅・自動車・所得などの市区町村別の統計を地図で表示します（SDR は使いません）")
        bar2.addWidget(self.region_btn)
        self.region_window = None

        self.station_list = QtWidgets.QListWidget()
        self.station_list.setFont(theme.font(theme.MONO_FONTS, 10))
        self.station_count = theme.caption("未サーチ")
        station_box = theme.panel()
        sl = QtWidgets.QVBoxLayout(station_box)
        sl.setContentsMargins(10, 10, 10, 10)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(theme.caption("見つかった局", "heading"))
        head.addStretch()
        head.addWidget(self.station_count)
        sl.addLayout(head)
        sl.addWidget(self.station_list, 1)
        station_hint = theme.caption("「局サーチ」で受信できる局を探し、\nクリックで選局します。", "hint")
        station_hint.setWordWrap(True)
        sl.addWidget(station_hint)
        station_box.setMinimumWidth(210)
        self.stations = []        # [(周波数Hz, 強さdB)]
        self.scan_mode = None
        self.scanning = False

        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.hide()
        hint = theme.caption("クリック／赤線ドラッグで選局　ホイールで拡大", "hint")
        self.statusBar().addPermanentWidget(hint)
        self.statusBar().addPermanentWidget(self.progress)

        # --- グラフ ---
        pg.setConfigOptions(imageAxisOrder="row-major", antialias=False,
                            background=theme.C["plot"], foreground=theme.C["muted"])
        axis_font = theme.font(theme.MONO_FONTS, 8)
        self.spec_plot = pg.PlotWidget()
        self.spec_plot.setLabel("left", "強度", units="dB")
        self.spec_plot.setMouseEnabled(x=True, y=False)
        self.spec_plot.showGrid(x=True, y=True, alpha=0.15)
        self.spec_plot.getAxis("bottom").setStyle(showValues=False)
        self.curve = self.spec_plot.plot(pen=pg.mkPen(theme.C["accent"], width=1.2),
                                         fillLevel=-300, brush=pg.mkBrush(79, 195, 247, 30))
        self.marker = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen(theme.C["marker"], width=2),
            hoverPen=pg.mkPen("#ff8a80", width=3),
            label="{value:.3f} MHz",
            labelOpts={"position": 0.93, "color": "#ff8a80", "fill": (11, 13, 17, 200), "movable": True})
        self.marker.label.setFont(theme.font(theme.MONO_FONTS, 10, bold=True))
        self.spec_plot.addItem(self.marker)

        self.wf_plot = pg.PlotWidget()
        self.wf_plot.setLabel("bottom", "周波数", units="MHz")
        self.wf_plot.setMouseEnabled(x=True, y=False)
        self.wf_plot.hideAxis("left")
        self.wf_plot.setXLink(self.spec_plot)
        self.img = pg.ImageItem()
        self.img.setLookupTable(pg.colormap.get("inferno").getLookupTable(nPts=256))
        self.wf_plot.addItem(self.img)
        self.wf_marker = pg.InfiniteLine(angle=90, pen=pg.mkPen(theme.C["marker"], width=1, style=QtCore.Qt.DashLine))
        self.wf_plot.addItem(self.wf_marker)
        for plot in (self.spec_plot, self.wf_plot):
            plot.setFrameShape(QtWidgets.QFrame.NoFrame)
            for ax in ("left", "bottom"):
                plot.getAxis(ax).setTickFont(axis_font)
                plot.getAxis(ax).setPen(theme.C["line"])
                plot.getAxis(ax).setTextPen(theme.C["muted"])
        # 左の軸幅を揃えて、スペクトラムとウォーターフォールの横位置を合わせる
        self.spec_plot.getAxis("left").setWidth(56)
        self.wf_plot.getPlotItem().layout.setColumnFixedWidth(0, 56)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(self.spec_plot)
        split.addWidget(self.wf_plot)
        split.setSizes([300, 420])
        split.setChildrenCollapsible(False)

        hsplit = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        hsplit.addWidget(split)
        hsplit.addWidget(station_box)
        hsplit.setStretchFactor(0, 1)
        hsplit.setSizes([880, 230])
        hsplit.setChildrenCollapsible(False)

        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(12, 12, 12, 8)
        lay.setSpacing(10)
        lay.addWidget(tuner)
        lay.addLayout(bar2)
        lay.addWidget(hsplit, 1)
        self.setCentralWidget(central)
        self.set_state("stopped")

        # --- イベント ---
        self.freq.valueChanged.connect(self.apply_params)
        self.mode.currentIndexChanged.connect(self.apply_params)
        self.gain.currentIndexChanged.connect(self.apply_params)
        self.vol.valueChanged.connect(self.on_volume)
        self.out_dev.currentIndexChanged.connect(self.on_output_device)
        self.down_btn.clicked.connect(lambda: self.step_freq(-1))
        self.up_btn.clicked.connect(lambda: self.step_freq(+1))
        self.preset.currentIndexChanged.connect(self.apply_preset)
        self.run_btn.toggled.connect(self.toggle_run)
        self.scan_btn.clicked.connect(self.toggle_scan)
        self.scan_band.activated.connect(self.on_scan_band)
        self.sat_btn.clicked.connect(self.open_satellite)
        self.adsb_btn.clicked.connect(self.open_adsb)
        self.region_btn.clicked.connect(self.open_region_stats)
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

    # --- 状態表示 ---
    STATES = {"stopped": ("● 停止中", "dim"), "running": ("● 受信中", "ok"),
              "scanning": ("● サーチ中", "accent"), "recording": ("● 録音中", "rec")}

    def set_state(self, key):
        text, color = self.STATES[key]
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {theme.C[color]}; font-weight: bold;")

    # --- 設定 ---
    def on_volume(self, v):
        self.audio.volume = v / 100
        self.vol_label.setText(f"{v}%")

    def on_output_device(self, idx):
        err = self.audio.set_device(self.out_dev.itemData(idx))
        if err:
            self.out_dev.blockSignals(True)
            self.out_dev.setCurrentIndex(0)
            self.out_dev.blockSignals(False)
            QtWidgets.QMessageBox.warning(self, "音声出力", f"このデバイスは使えません。既定に戻しました。\n\n{err}")
        else:
            self.statusBar().showMessage(f"音声出力：{self.out_dev.currentText()}", 4000)

    def freq_step(self):
        return 0.1 if self.mode.currentText() == "WFM" else 0.005

    def step_freq(self, direction):
        self.tune_to(self.freq.value() + direction * self.freq_step())

    def current_gain(self):
        i = self.gain.currentIndex()
        return "auto" if i == 0 else GAINS[i - 1]

    def apply_params(self):
        self.freq.setSingleStep(self.freq_step())
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
        step = self.freq_step()
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
        # ADS-B 受信中なら止めて SDR を空ける（録音が終わったら再開する）
        adsb_was_running = self.adsb_running()
        if adsb_was_running:
            self.adsb_window.stop("気象衛星の録音のため一時停止中（録音が終わると再開します）")
        # 録音が終わったら元に戻せるよう、直前の状態を覚えておく
        self.before_rec = (self.freq.value(), self.mode.currentText(), self.worker is not None, adsb_was_running)
        if not self.worker:
            self.run_btn.setChecked(True)
            if not self.worker:
                return "SDRを開始できませんでした"
        self.freq.setValue(freq_hz / 1e6)
        self.apply_params()
        self.worker.recorder = IQRecorder(path)
        for w in self.tuning_widgets():
            w.setEnabled(False)
        self.set_state("recording")
        return None

    # --- ADS-B ---
    def open_adsb(self):
        if self.adsb_window is None:
            import adsb
            self.adsb_window = adsb.AdsbWindow(self, GAINS)
        self.adsb_window.show()
        self.adsb_window.raise_()

    # --- 地域統計 ---
    def open_region_stats(self):
        if self.region_window is None:
            import region_stats
            self.region_window = region_stats.RegionStatsWindow()
        self.region_window.show()
        self.region_window.raise_()

    def adsb_running(self):
        return bool(self.adsb_window and self.adsb_window.running)

    def tuning_widgets(self):
        """録音中は触れないようにする操作部品"""
        return (self.freq, self.down_btn, self.up_btn, self.mode, self.scan_btn, self.prev_btn,
                self.next_btn, self.preset, self.run_btn, self.station_list)

    def stop_recording(self):
        rec = self.worker.recorder if self.worker else None
        if self.worker:
            self.worker.recorder = None
        if rec:
            rec.close()
        for w in self.tuning_widgets():
            w.setEnabled(True)
        self.set_state("running" if self.worker else "stopped")
        # 録音前の状態に戻す（衛星の周波数のまま残らないように）
        before, self.before_rec = getattr(self, "before_rec", None), None
        if before:
            freq, mode, was_running, adsb_was_running = before
            self.mode.setCurrentText(mode)
            self.freq.setValue(freq)
            if not was_running and self.worker:
                self.run_btn.setChecked(False)      # 録音のために開始した受信は止める
            if adsb_was_running and self.adsb_window:
                self.adsb_window.start()
            self.statusBar().showMessage(f"録音前の {freq:.3f} MHz {mode} に戻しました", 5000)
        return rec.bytes if rec else 0

    # --- 局サーチ ---
    def toggle_scan(self):
        if self.scanning:
            if self.worker:
                self.worker.cancel_scan()
            return
        band = self.scan_band.currentData()
        if band is None:
            band = self.edit_custom_band()
            if band is None:
                return
        if band[3] - band[2] > SCAN_WARN_HZ:
            ans = QtWidgets.QMessageBox.question(
                self, "局サーチ",
                f"範囲が {(band[3] - band[2]) / 1e6:g}MHz と広いため、{scan_eta(band)}かかります。\nサーチしますか？")
            if ans != QtWidgets.QMessageBox.Yes:
                return
        if not self.worker:
            self.run_btn.setChecked(True)     # 受信していなければ開始する
            if not self.worker:
                return
        self.scanning = True
        self.scan_btn.setText("■ サーチ中止")
        self.progress.setValue(0)
        self.progress.show()
        self.statusBar().showMessage(f"{band[0]} をサーチしています…")
        self.set_state("scanning")
        self.worker.request_scan(band)

    def on_scan_band(self, idx):
        """「カスタム…」を選んだら（選び直したときも）範囲を入力してもらう"""
        if idx == self.scan_band.count() - 1:
            if self.edit_custom_band() is None:
                self.scan_band.setCurrentIndex(self.scan_band_idx)
                return
        self.scan_band_idx = self.scan_band.currentIndex()

    def edit_custom_band(self):
        """カスタム範囲を入力して最後の項目に保存する。キャンセルなら None"""
        last = self.scan_band.count() - 1
        dlg = CustomScanDialog(self, self.scan_band.itemData(last))
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None
        band = dlg.band()
        self.scan_band.setItemText(last, band[0])
        self.scan_band.setItemData(last, band)
        self.scan_band.setCurrentIndex(last)
        return band

    def on_scan_progress(self, pct):
        self.progress.setValue(pct)

    def on_scan_done(self, found, mode):
        self.scanning = False
        self.scan_btn.setText("📡 局サーチ")
        self.progress.hide()
        self.stations, self.scan_mode = found, mode
        self.set_state("running" if self.worker else "stopped")
        self.station_list.clear()
        self.station_count.setText(f"{len(found)}局")
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
        msg = f"{self.freq.value():.3f} MHz   {self.mode.currentText()}   ゲイン {self.gain.currentText()}"
        if self.worker:
            msg += f"   ｜ 音切れ {self.audio.underruns}回 ・ 処理落ち {self.worker.dropped}回"
        if SD_ERR or self.audio.error:
            msg += f"  ⚠ 音声出力が使えません（{SD_ERR or self.audio.error}）"
        self.statusBar().showMessage(msg)

    # --- 開始 / 停止 ---
    def toggle_run(self, on):
        if on and self.adsb_running():
            QtWidgets.QMessageBox.information(
                self, "ADS-B 受信中", "ADS-B の受信で SDR を使っています。\nADS-B ウィンドウで停止してから受信を開始してください。")
            self.run_btn.setChecked(False)
            return
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
            self.set_state("running")
            self.update_status()
        else:
            self.stop_worker()
            self.run_btn.setText("▶ 受信開始")
            self.set_state("stopped")
            self.statusBar().clearMessage()

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
        # 録音中に受信が止まった場合は、録音を終わらせて操作できる状態に戻す（録れた分はデコードする）
        if self.sat_window and self.sat_window.recording:
            self.sat_window.stop_record()
        elif self.worker and self.worker.recorder:
            self.stop_recording()
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
        if self.adsb_window:
            self.adsb_window.shutdown()
            self.adsb_window.close()
        if self.sat_window:
            self.sat_window.shutdown()
        if self.region_window:
            self.region_window.shutdown()
        self.stop_recording()
        self.stop_worker()
        super().closeEvent(ev)


def main():
    # ADS-B の地図（QtWebEngine）は QApplication より前にこの設定が必要
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    app = QtWidgets.QApplication(sys.argv)
    theme.apply(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

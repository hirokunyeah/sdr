#!/usr/bin/env python3
"""
RTL-SDR V4 Webレシーバー（Raspberry Pi 用サーバー）

Raspberry Pi で受信・復調し、スペクトラムと音声を WebSocket でブラウザへ配信する。
  python server.py              # 実機で起動
  python server.py --demo       # SDRなしの動作確認（疑似信号）
"""
import argparse
import asyncio
import json
import logging
import os
import struct
import threading
import time

import numpy as np
from scipy import fft as sfft
from scipy.signal import firwin, fftconvolve, lfilter
from aiohttp import web, WSMsgType

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("sdr-web")

# ---------------- 定数（Raspberry Pi 4 の負荷を考えて 1.2 MS/s） ----------------
FS = 1_200_000
OFFSET = 300_000          # DCスパイク回避。FS/OFFSET=4 なので発振器を使い回せる
BLOCK = 128_000           # 約0.107秒分。512バイト境界・各間引き率の倍数
IF_FS = 240_000
AUDIO_FS = 48_000
FFT_N = 2048
MODES = ("WFM", "NFM", "AM")
GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7,
         22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4,
         43.9, 44.5, 48.0, 49.6]
FREQ_MIN, FREQ_MAX = 1.0e6, 1766.0e6

MSG_SPECTRUM = 1
MSG_AUDIO = 2


# ---------------- 信号処理 ----------------
class FirDecimator:
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
        return fftconvolve(buf, self.taps, mode="valid")[::self.decim]


class IIR1:
    def __init__(self, b, a):
        self.b, self.a = b, a
        self.zi = np.zeros(max(len(a), len(b)) - 1)

    def __call__(self, x):
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y


OSC = np.exp(-2j * np.pi * OFFSET / FS * np.arange(BLOCK)).astype(np.complex64)
assert BLOCK % (FS // OFFSET) == 0


class Demodulator:
    def __init__(self, mode):
        self.mode = mode
        self.last = np.complex64(0)
        self.stage1 = FirDecimator(firwin(129, 110e3, fs=FS), FS // IF_FS)
        if mode == "WFM":
            self.audio = FirDecimator(firwin(129, 15e3, fs=IF_FS), IF_FS // AUDIO_FS)
            d = np.exp(-1.0 / (AUDIO_FS * 50e-6))
            self.deemph = IIR1([1 - d], [1, -d])
        else:
            cutoff = 5e3 if mode == "AM" else 6e3
            self.chan = FirDecimator(firwin(255, cutoff, fs=IF_FS), IF_FS // AUDIO_FS)
            self.dc = IIR1([1, -1], [1, -0.995])

    def _fm(self, x):
        ext = np.concatenate(([self.last], x))
        self.last = x[-1]
        return np.angle(ext[1:] * np.conj(ext[:-1]))

    def process(self, iq):
        x = self.stage1(iq * OSC)
        if self.mode == "WFM":
            return self.deemph(self.audio(self._fm(x))) * 0.5
        if self.mode == "NFM":
            return self.dc(self._fm(self.chan(x))) * 1.5
        env = np.abs(self.chan(x))
        return self.dc(env / (np.mean(env) + 1e-12)) * 0.8


# ---------------- デモ用の疑似SDR ----------------
class FakeSdr:
    """80.0 MHz に 1kHz トーンのFM局がある想定の疑似デバイス"""

    def __init__(self):
        self.sample_rate = FS
        self.center_freq = 80e6
        self.gain = "auto"
        self.t0 = 0
        self.next_time = time.monotonic()

    def read_bytes(self, nbytes):
        n = nbytes // 2
        t = (self.t0 + np.arange(n)) / FS
        self.t0 += n
        sig = (np.random.randn(n) + 1j * np.random.randn(n)) * 0.02
        df = 80e6 - self.center_freq
        if abs(df) < FS / 2:
            ph = 2 * np.pi * df * t + (75e3 / 1e3) * np.sin(2 * np.pi * 1e3 * t)
            sig += 0.5 * np.exp(1j * ph)
        iq = np.empty(2 * n, dtype=np.float32)
        iq[0::2], iq[1::2] = sig.real, sig.imag
        self.next_time += n / FS
        time.sleep(max(0.0, self.next_time - time.monotonic()))
        return np.clip(iq * 127.5 + 127.5, 0, 255).astype(np.uint8).tobytes()

    def close(self):
        pass


# ---------------- SDRエンジン（別スレッド） ----------------
class SdrEngine:
    def __init__(self, hub, demo=False):
        self.hub = hub
        self.demo = demo
        self.lock = threading.Lock()
        self.ctl = threading.Lock()
        self.state = {"freq": 80.0e6, "mode": "WFM", "gain": 29.7}
        self._dirty = True
        self._running = False
        self.thread = None
        self.error = None

    def start(self):
        with self.ctl:
            if self.thread and self.thread.is_alive():
                if self._running:
                    return
                self.thread.join()
            self._running = True
            self._dirty = True
            self.error = None
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self):
        with self.ctl:
            self._running = False
            if self.thread:
                self.thread.join(timeout=5)

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive() and self._running)

    def update(self, data):
        with self.lock:
            if "freq" in data:
                self.state["freq"] = float(min(max(float(data["freq"]), FREQ_MIN), FREQ_MAX))
            if data.get("mode") in MODES:
                self.state["mode"] = data["mode"]
            if "gain" in data:
                g = data["gain"]
                self.state["gain"] = "auto" if g == "auto" else min(GAINS, key=lambda v: abs(v - float(g)))
            self._dirty = True

    def _open(self):
        if self.demo:
            return FakeSdr()
        from rtlsdr import RtlSdr
        return RtlSdr()

    def _run(self):
        try:
            sdr = self._open()
        except Exception as e:
            self.error = f"RTL-SDRを開けませんでした: {e}"
            log.error(self.error)
            self._running = False
            self.hub.post_state()
            return
        log.info("SDR opened")
        self.hub.post_state()
        try:
            sdr.sample_rate = FS
            win = np.hanning(FFT_N).astype(np.float32)
            win_pow = float(np.sum(win ** 2))
            demod, cur_mode, hw = None, None, 0.0
            while self._running:
                with self.lock:
                    dirty, self._dirty = self._dirty, False
                    freq, mode, gain = self.state["freq"], self.state["mode"], self.state["gain"]
                if dirty:
                    hw = freq - OFFSET
                    sdr.center_freq = hw
                    sdr.gain = gain
                    if mode != cur_mode:
                        demod, cur_mode = Demodulator(mode), mode

                raw = np.frombuffer(sdr.read_bytes(BLOCK * 2), dtype=np.uint8)
                iq = ((raw.astype(np.float32) - 127.5) / 127.5).view(np.complex64)
                if len(iq) != BLOCK:
                    continue

                frames = iq[:(BLOCK // FFT_N) * FFT_N].reshape(-1, FFT_N)
                spec = sfft.fft(frames * win, axis=1, workers=2)
                p = np.mean(spec.real ** 2 + spec.imag ** 2, axis=0) / win_pow
                db = (10 * np.log10(sfft.fftshift(p) + 1e-12)).astype("<f4")
                self.hub.post(struct.pack("<B7xd", MSG_SPECTRUM, hw) + db.tobytes())

                audio = demod.process(iq)
                pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
                self.hub.post(struct.pack("<B7x", MSG_AUDIO) + pcm.tobytes())
        except Exception as e:
            self.error = f"受信中にエラー: {e}"
            log.exception("SDR error")
        finally:
            sdr.close()
            self._running = False
            log.info("SDR closed")
            self.hub.post_state()

    def snapshot(self):
        with self.lock:
            s = dict(self.state)
        s.update(type="state", fs=FS, offset=OFFSET, fftN=FFT_N, audioFs=AUDIO_FS,
                 gains=GAINS, running=self.running, error=self.error, demo=self.demo)
        return s


# ---------------- 接続クライアント管理 ----------------
class Hub:
    def __init__(self):
        self.loop = None
        self.clients = {}  # ws -> asyncio.Queue
        self.engine = None

    def post(self, data):
        """SDRスレッドから呼ばれる"""
        self.loop.call_soon_threadsafe(self._broadcast, data)

    def post_state(self):
        self.loop.call_soon_threadsafe(self._broadcast_state)

    def _broadcast(self, data):
        for q in self.clients.values():
            if q.full():  # 遅いクライアントは古いデータを捨てる
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)

    def _broadcast_state(self):
        s = self.engine.snapshot()
        s["clients"] = len(self.clients)
        self._broadcast(json.dumps(s))


async def ws_handler(request):
    hub = request.app["hub"]
    engine = hub.engine
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    q = asyncio.Queue(maxsize=40)
    hub.clients[ws] = q
    log.info("client connected (%d)", len(hub.clients))
    loop = asyncio.get_running_loop()

    async def sender():
        while True:
            data = await q.get()
            if isinstance(data, str):
                await ws.send_str(data)
            else:
                await ws.send_bytes(data)

    send_task = asyncio.create_task(sender())
    await loop.run_in_executor(None, engine.start)
    hub._broadcast_state()
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            cmd = data.get("cmd")
            if cmd == "set":
                engine.update(data)
                hub._broadcast_state()
            elif cmd == "restart":
                await loop.run_in_executor(None, engine.stop)
                await loop.run_in_executor(None, engine.start)
                hub._broadcast_state()
    finally:
        send_task.cancel()
        hub.clients.pop(ws, None)
        log.info("client disconnected (%d)", len(hub.clients))
        if not hub.clients:  # 誰も見ていなければSDRを止める
            await loop.run_in_executor(None, engine.stop)
        else:
            hub._broadcast_state()
    return ws


async def index(request):
    return web.FileResponse(os.path.join(HERE, "static", "index.html"))


async def on_startup(app):
    app["hub"].loop = asyncio.get_running_loop()


async def on_cleanup(app):
    await asyncio.get_running_loop().run_in_executor(None, app["hub"].engine.stop)


def main():
    ap = argparse.ArgumentParser(description="RTL-SDR V4 Webレシーバー")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true", help="SDRなしで疑似信号を流す")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    hub = Hub()
    hub.engine = SdrEngine(hub, demo=args.demo)
    app = web.Application()
    app["hub"] = hub
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    log.info("http://<このPiのIPアドレス>:%d/ をブラウザで開いてください", args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

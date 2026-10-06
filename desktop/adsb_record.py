"""
ADS-B の受信データの記録と再生。

  Recorder      … 自局で受信したメッセージ（CRC が正しい 14バイト）と、インターネットから取得した機体を
                  時刻つきでファイルに書く（gzip 圧縮。1時間で数MB〜十数MB）
  load()        … 記録ファイルを読み込む
  ReplayWorker  … 記録したときの時間の流れどおりに Tracker へ入れ直して、受信中と同じ形で表示用のデータを返す。
                  一時停止・速度の変更・好きな時刻への移動ができる

ファイルの形式（1行1件の UTF-8 テキストを gzip 圧縮したもの）
  H {"format": "adsb-rec", "version": 1, "home": [緯度, 経度], ...}   先頭の1行
  M <UNIX時刻> <メッセージの16進28文字>                                自局で受信したメッセージ
  N <UNIX時刻> <機体のリストの JSON>                                   ネットから取得した機体（adsb_net.parse() の結果）
"""
import bisect
import gzip
import json
import os
import threading
import time
from array import array

import numpy as np
from PySide6 import QtCore

from adsb_decoder import Tracker
from adsb_net import add_trails
from adsb_net import snapshot as net_snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "adsb_data")
FORMAT = "adsb-rec"
VERSION = 1
MSG_BYTES = 14
FLUSH_SEC = 5          # 異常終了しても失うのはこの秒数分まで（gzip を区切りまで書き出す間隔）
REWIND_SEC = 120       # 移動（シーク）したとき、機体と航跡を作り直すために、移動先のこの秒数前から読み直す
TICK = 0.05            # 再生スレッドが時刻を進める間隔（秒）
SPEEDS = (1, 2, 5, 10, 30, 60)


# ---------------- 記録 ----------------
class Recorder:
    """受信スレッド（messages）とメインスレッド（net）の両方から書き込める"""

    def __init__(self, home):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, time.strftime("adsb_%Y%m%d_%H%M%S.log.gz"))
        self.lock = threading.Lock()
        self.count = 0
        self.last_flush = time.monotonic()
        self.f = gzip.open(self.path, "wt", encoding="utf-8")
        header = {"format": FORMAT, "version": VERSION, "home": list(home),
                  "start": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        self._write(f"H {json.dumps(header, ensure_ascii=False)}\n", 0)

    @property
    def name(self):
        return os.path.basename(self.path)

    def messages(self, now, msgs):
        self._write("".join(f"M {now:.3f} {m.hex()}\n" for m in msgs), len(msgs))

    def net(self, now, planes):
        self._write(f"N {now:.3f} {json.dumps(planes, ensure_ascii=False, separators=(',', ':'))}\n", 1)

    def _write(self, text, n):
        with self.lock:
            if self.f is None:
                return
            self.f.write(text)
            self.count += n
            if time.monotonic() - self.last_flush > FLUSH_SEC:
                self.f.flush()
                self.last_flush = time.monotonic()

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None

    def status(self):
        return f"記録中：{self.name}（{self.count:,} 件）"


# ---------------- 読み込み ----------------
class Recording:
    def __init__(self, header, times, data, nets):
        if len(times) and np.any(np.diff(times) < 0):    # 時計の調整などで前後していたら並べ直す
            order = np.argsort(times, kind="stable")
            times = times[order]
            data = b"".join(data[i * MSG_BYTES:(i + 1) * MSG_BYTES] for i in order)
        nets.sort(key=lambda x: x[0])
        self.header = header
        self.times = times          # メッセージの時刻（numpy 配列）
        self.data = data            # メッセージを順につなげた bytes
        self.nets = nets            # [(時刻, 機体のリスト)]
        self.net_times = [t for t, _ in nets]
        ends = [t for t in (times[:1].tolist() + times[-1:].tolist() + self.net_times[:1] + self.net_times[-1:])]
        self.start, self.end = min(ends), max(ends)

    @property
    def home(self):
        h = self.header.get("home")
        return (float(h[0]), float(h[1])) if h else None

    def message(self, i):
        return self.data[i * MSG_BYTES:(i + 1) * MSG_BYTES]


def load(path, cancel=None, progress=None):
    """記録ファイルを読み込んで Recording を返す。cancel() が真になったら None を返す"""
    times, data, nets = array("d"), bytearray(), []
    opener = gzip.open if path.lower().endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as f:
            line = f.readline()
            if not line.startswith("H "):
                raise ValueError("ADS-B の記録ファイルではありません")
            header = json.loads(line[2:])
            if header.get("format") != FORMAT:
                raise ValueError("ADS-B の記録ファイルではありません")
            for n, line in enumerate(f):
                if n % 50000 == 0:
                    if cancel and cancel():
                        return None
                    if progress:
                        progress(n)
                try:
                    kind = line[:1]
                    if kind == "M":
                        _, t, h = line.split()
                        if len(h) == MSG_BYTES * 2:
                            msg = bytes.fromhex(h)
                            times.append(float(t))
                            data += msg
                    elif kind == "N":
                        _, t, js = line.split(" ", 2)
                        nets.append((float(t), json.loads(js)))
                except (ValueError, IndexError):     # 途中で切れた行などは飛ばす
                    continue
    except EOFError:
        pass     # 記録中に異常終了したファイル。最後に書き出したところまで使う
    if not times and not nets:
        raise ValueError("記録されたデータがありません")
    return Recording(header, np.frombuffer(times, dtype=np.float64).copy(), bytes(data), nets)


# ---------------- 再生 ----------------
class ReplayWorker(QtCore.QThread):
    """記録を読み込み、再生位置（記録の時刻 vt）を進めながら Tracker にメッセージを入れる。
    snapshot() は受信中の AdsbWorker / NetFeed と同じ形の機体のリストを返す"""
    failed = QtCore.Signal(str)
    loaded = QtCore.Signal()
    ended = QtCore.Signal()      # 最後まで再生して止まった

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.lock = threading.Lock()
        self.rec = None
        self.ready = False
        self.lines = 0           # 読み込み中の進み具合
        self.speed = 1
        self.paused = False
        self._seek = None
        self._running = True
        self.tracker = Tracker()
        self.net = ([], {})      # (機体のリスト, 航跡)
        self.vt = 0.0
        self.i = self.j = 0      # 次に入れるメッセージ / ネットのデータの番号

    def stop(self):
        self._running = False

    def set_speed(self, speed):
        self.speed = speed

    def set_paused(self, paused):
        with self.lock:
            if not paused and self.rec and self.vt >= self.rec.end:
                self._seek = self.rec.start          # 最後まで再生した後は最初から
            self.paused = paused

    def seek(self, t):
        with self.lock:
            self._seek = t

    def run(self):
        try:
            rec = load(self.path, cancel=lambda: not self._running,
                       progress=lambda n: setattr(self, "lines", n))
        except Exception as e:
            if self._running:
                self.failed.emit(f"記録を読み込めませんでした。\n\n{e}")
            return
        if rec is None:
            return
        self.rec = rec
        self.vt = rec.start
        self.ready = True
        self.loaded.emit()
        last = time.monotonic()
        while self._running:
            time.sleep(TICK)
            with self.lock:
                seek, self._seek = self._seek, None
                paused = self.paused
            if seek is not None:
                self._jump(seek)
                last = time.monotonic()      # 作り直しにかかった時間の分は進めない
                continue
            now = time.monotonic()
            dt, last = now - last, now
            if paused:
                continue
            t = min(self.vt + dt * self.speed, rec.end)
            with self.lock:
                self.i, self.j, self.net = self._feed(self.tracker, self.net, self.i, self.j, t)
                self.vt = t
                at_end = t >= rec.end
                if at_end:
                    self.paused = True
            if at_end:
                self.ended.emit()

    def _feed(self, tracker, net, i, j, t):
        """メッセージ i 番目以降、ネットのデータ j 番目以降のうち、時刻 t までのものを入れる"""
        rec = self.rec
        k = int(np.searchsorted(rec.times, t, "right"))
        for n in range(i, k):
            tracker.update(rec.message(n), float(rec.times[n]))
        while j < len(rec.nets) and rec.nets[j][0] <= t:
            planes = rec.nets[j][1]
            net = (planes, add_trails(net[1], planes))
            j += 1
        return k, j, net

    def _jump(self, t):
        """時刻 t へ移動する。少し前から読み直して、機体と航跡を作り直す（表示中の機体は止めずに置き換える）"""
        rec = self.rec
        t = min(max(t, rec.start), rec.end)
        t0 = t - REWIND_SEC
        i = int(np.searchsorted(rec.times, t0, "left"))
        j = bisect.bisect_left(rec.net_times, t0)
        tracker = Tracker()
        i, j, net = self._feed(tracker, ([], {}), i, j, t)
        with self.lock:
            self.tracker, self.net, self.i, self.j, self.vt = tracker, net, i, j, t

    def snapshot(self):
        """(自局の機体, ネットの機体)。時刻は再生位置で計算する"""
        with self.lock:
            vt = self.vt
            self.tracker.prune(vt)
            return self.tracker.snapshot(vt), net_snapshot(*self.net, vt)

    def rate(self):
        """再生位置の直前1秒のメッセージ数"""
        times, vt = self.rec.times, self.vt
        return int(np.searchsorted(times, vt, "right") - np.searchsorted(times, vt - 1, "left"))


def clock(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def duration(sec):
    sec = int(max(sec, 0))
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

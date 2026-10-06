"""
インターネットの ADS-B 共有サービスから、受信地点の周辺を飛んでいる機体を取得する。

  無料・キー不要の adsb.lol / adsb.fi（readsb 形式の JSON）を一定間隔で取得し、
  自分の SDR で受信した機体と ICAO 番号で照合してまとめる（自局で受信した位置を優先）。
  どちらも非商用・個人利用向けの無料サービスで、利用条件や上限は変わることがある。
"""
import json
import threading
import time
import urllib.error
import urllib.request

from PySide6 import QtCore

from adsb_decoder import AIRCRAFT_TIMEOUT, TRAIL_MAX

SOURCES = {
    "adsb.lol": {"url": "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{dist}",
                 "credit": '<a href="https://adsb.lol/">adsb.lol</a> (ODbL)'},
    "adsb.fi": {"url": "https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{dist}",
                "credit": '<a href="https://adsb.fi/">adsb.fi</a>'},
}
RADIUS_NM = 150           # 受信地点からこの範囲（海里）の機体を取得する（約280km）
INTERVAL = 10             # 取得間隔（秒）
MAX_INTERVAL = 60         # 混雑（HTTP 429）などで失敗したときは、この秒数まで間隔を延ばす
USER_AGENT = "rtl-sdr-v4-receiver/1.5 (personal, non-commercial)"
MERGE_KEYS = ("callsign", "alt", "speed", "track", "vrate", "type", "reg")


def parse(data, now):
    """API の JSON を、Tracker.snapshot() と同じ形の機体の辞書のリストにする（位置のない機体は除く）"""
    out = []
    for a in data.get("ac") or data.get("aircraft") or []:
        hex_ = str(a.get("hex", ""))
        if not hex_ or hex_.startswith("~") or a.get("lat") is None:   # "~" は ICAO 番号のない機体（TIS-B など）
            continue
        alt = a.get("alt_baro")
        alt = 0 if alt == "ground" else round(alt) if isinstance(alt, (int, float)) else None
        seen = float(a.get("seen", 0))
        track = a.get("track", a.get("true_heading"))
        vrate = a.get("baro_rate", a.get("geom_rate"))
        out.append({
            "icao": hex_.upper(), "callsign": (a.get("flight") or "").strip(), "alt": alt,
            "speed": round(a["gs"]) if a.get("gs") is not None else None,
            "track": track, "vrate": round(vrate) if vrate is not None else None,
            "lat": float(a["lat"]), "lon": float(a["lon"]), "msgs": None,
            "t_seen": now - seen, "t_pos": now - float(a.get("seen_pos", seen)),
            "type": a.get("t", ""), "reg": a.get("r", ""),
        })
    return out


def add_trails(trails, planes):
    """取得した機体の位置を航跡（ICAO → 点のリスト）に加える。今回いなかった機体の航跡は捨てる"""
    out = {}
    for p in planes:
        t = trails.get(p["icao"], [])
        pt = (round(p["lat"], 5), round(p["lon"], 5))
        if not t or t[-1] != pt:
            t = (t + [pt])[-TRAIL_MAX:]
        out[p["icao"]] = t
    return out


def snapshot(planes, trails, now):
    """画面表示用（Tracker.snapshot() と同じ形）。古くなった機体は除く"""
    out = []
    for p in planes:
        age = now - p["t_seen"]
        if age > AIRCRAFT_TIMEOUT:
            continue
        s = {k: v for k, v in p.items() if k not in ("t_seen", "t_pos")}
        s.update(age=age, pos_age=now - p["t_pos"], trail=list(trails.get(p["icao"], [])),
                 seen_pos=p["t_pos"], src="net")
        out.append(s)
    return out


def merge(local, net):
    """自局の機体（local）にネットの機体（net）を ICAO 番号で照合して加える。
    src は位置の出どころ："local"（自局で受信）／"net"（インターネット）"""
    by_icao = {}
    for a in local:
        a.setdefault("type", "")
        a.setdefault("reg", "")
        a["src"] = "local"
        by_icao[a["icao"]] = a
    out = list(local)
    for n in net:
        a = by_icao.get(n["icao"])
        if a is None:
            out.append(n)
            continue
        for k in MERGE_KEYS:                       # 自局で分からなかった項目だけ補う
            if a.get(k) in (None, "") and n.get(k) not in (None, ""):
                a[k] = n[k]
        if a["lat"] is None:                       # 自局で位置が取れていなければネットの位置を使う
            a.update(lat=n["lat"], lon=n["lon"], pos_age=n["pos_age"], trail=n["trail"], src="net")
    return out


class NetFeed(QtCore.QObject):
    """一定間隔でネットから取得する。取得は別スレッドで行い、結果はメインスレッドで受け取る"""
    _fetched = QtCore.Signal(object, str)    # (JSON, エラー)
    received = QtCore.Signal(object, float)  # 取得できたとき (parse() の結果, 取得時刻)。記録用

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = "adsb.lol"
        self.center = (35.68, 139.77)
        self.planes = []
        self.trails = {}
        self.error = ""
        self.fetched_at = None
        self.busy = False
        self.on = False          # 取得中か（start〜stop の間）
        self.interval = INTERVAL
        self.gen = 0             # 取得の世代。停止・切り替え前に出した取得の結果を捨てるのに使う
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._fetch)
        self._fetched.connect(self._on_fetched)

    @property
    def active(self):
        return self.timer.isActive() or self.busy

    def start(self, source, center):
        self.stop()
        self.source, self.center = source, center
        self.interval = INTERVAL
        self.on = True
        self.timer.start(0)

    def stop(self):
        self.timer.stop()
        self.on = False
        self.planes, self.trails = [], {}
        self.error, self.fetched_at = "", None
        self.gen += 1

    def _fetch(self):
        if self.busy:
            return
        self.busy = True
        url = SOURCES[self.source]["url"].format(lat=self.center[0], lon=self.center[1], dist=RADIUS_NM)
        gen = self.gen

        def job():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=15) as r:
                    self._fetched.emit((gen, json.load(r)), "")
            except urllib.error.HTTPError as e:
                self._fetched.emit((gen, None), f"HTTP {e.code}" + ("（アクセスが多すぎます）" if e.code == 429 else ""))
            except Exception as e:
                self._fetched.emit((gen, None), str(e))

        threading.Thread(target=job, daemon=True).start()

    def _on_fetched(self, result, err):
        gen, data = result
        self.busy = False
        if gen != self.gen:      # 停止・切り替え済み
            # 取得中に切り替えたときは、新しい取得が busy で飛ばされているので、ここで取得し直す
            if self.on and not self.timer.isActive():
                self.timer.start(0)
            return
        if err:
            self.error = err
            self.interval = min(self.interval * 2, MAX_INTERVAL)
        else:
            now = time.time()
            planes = parse(data, now)
            self.planes, self.trails = planes, add_trails(self.trails, planes)
            self.error, self.fetched_at = "", now
            self.interval = INTERVAL
            self.received.emit(planes, now)
        self.timer.start(int(self.interval * 1000))

    def snapshot(self, now=None):
        """画面表示用（Tracker.snapshot() と同じ形）。古くなった機体は除く"""
        return snapshot(self.planes, self.trails, time.time() if now is None else now)

    def status(self):
        if self.error:
            return f"ネット（{self.source}）：取得失敗 {self.error}、{self.interval}秒後に再試行"
        if self.fetched_at is None:
            return f"ネット（{self.source}）：取得中…"
        return f"ネット（{self.source}）：{len(self.planes)}機・{time.time() - self.fetched_at:.0f}秒前に取得"

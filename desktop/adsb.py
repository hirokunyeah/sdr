"""
ADS-B（1090MHz）受信ウィンドウ

  航空機が送信している ADS-B を受信して、機体の位置・便名・高度・速度を地図と一覧に表示する。
  受信中は SDR を 1090MHz・2MS/s で使うため、メイン画面の受信とは同時に使えない。
  地図は Leaflet（インターネット接続が必要）。QtWebEngine がない環境では一覧だけ表示する。
  受信したデータはファイルに記録でき、後から記録の時間の流れどおりに再生できる（adsb_record.py）。
"""
import json
import os
import queue
import sys
import threading
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import adsb_record
import theme
from adsb_decoder import BLOCK, CENTER, FS, Demodulator, Tracker, distance_km
from adsb_net import INTERVAL as NET_INTERVAL
from adsb_net import RADIUS_NM, SOURCES, NetFeed, merge
from adsb_profile import ProfilePanel
from adsb_record import Recorder, ReplayWorker

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows で \\wsl.localhost\... などのネットワークパスから起動すると、地図を描く
# QtWebEngineProcess が Chromium のサンドボックス内で起動できず、地図が真っ黒になる
if sys.platform == "win32" and HERE.startswith("\\\\"):
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
# 高解像度の画面では、GPU でタイルを描く（GPUラスタライズ）と地図の一部が黒い四角に抜けることがある。
# 描くのは CPU、画面への合成は GPU にすると、抜けずに速く動く（GPU を全く使わないと遅い）。
# 自分で環境変数 QTWEBENGINE_CHROMIUM_FLAGS を設定した場合はそちらを使う
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-rasterization --log-level=3")

try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_ERR = None
except Exception as e:  # QtWebEngine が入っていない・使えない環境
    QWebEnginePage = object
    WEBENGINE_ERR = e

DEFAULT_GAIN = 49.6
STALE_SEC = 15            # 位置がこれだけ更新されない機体は地図上で薄く表示する

MAP_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body, #map { margin: 0; height: 100%%; background: %(plot)s; }
  body { font-family: sans-serif; color: %(text)s; }
  #err { padding: 24px; color: %(muted)s; }
  .lbl { background: rgba(15,17,21,.75); color: %(text)s; border: 1px solid %(line)s;
         border-radius: 4px; padding: 1px 4px; font: 11px/1.25 monospace; box-shadow: none; }
  .lbl::before { display: none; }
  .lbl .net { color: %(muted)s; font-size: 10px; }
  .leaflet-container { background: %(plot)s; }
  .leaflet-control-attribution { background: rgba(15,17,21,.7) !important; color: %(muted)s; }
  .leaflet-control-attribution a { color: %(accent)s; }
  .leaflet-control-layers { background: %(panel)s; color: %(text)s; border: 1px solid %(line)s !important; }
</style></head>
<body><div id="map"></div>
<script>
if (typeof L === "undefined") {
  document.body.innerHTML = '<div id="err">地図を読み込めませんでした（インターネット接続が必要です）。一覧は使えます。</div>';
}
const map = L.map("map", {zoomControl: true, minZoom: 2, maxZoom: 18}).setView([%(lat)f, %(lon)f], 8);
// 背景地図は国土地理院の地理院タイル（APIキー不要）。淡色地図は色を反転して暗い画面に合わせる。
// 反転は CSS の filter だと描画のたびに画面全体で計算されて重いので、タイルを読み込んだときに1回だけ行う
const DarkTileLayer = L.TileLayer.extend({
  createTile(coords, done) {
    const size = this.getTileSize();
    const canvas = document.createElement("canvas");
    canvas.width = size.x;
    canvas.height = size.y;
    const img = new Image();
    img.onload = () => {
      const ctx = canvas.getContext("2d");
      ctx.filter = "invert(1) hue-rotate(180deg) brightness(0.9) contrast(0.85)";
      ctx.drawImage(img, 0, 0, size.x, size.y);
      done(null, canvas);
    };
    img.onerror = e => done(e, canvas);
    img.src = this.getTileUrl(coords);
    return canvas;
  }
});
const gsi = (id, ext, dark) => new (dark ? DarkTileLayer : L.TileLayer)(
  `https://cyberjapandata.gsi.go.jp/xyz/${id}/{z}/{x}/{y}.${ext}`, {
    maxNativeZoom: 18,
    attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">地理院タイル</a>'
  });
const bases = {
  "淡色地図（暗）": gsi("pale", "png", true),
  "標準地図": gsi("std", "png"),
  "写真": gsi("seamlessphoto", "jpg"),
};
bases["淡色地図（暗）"].addTo(map);
L.control.layers(bases, null, {position: "topright"}).addTo(map);

let home = L.layerGroup().addTo(map);
function setHome(lat, lon) {
  home.clearLayers();
  for (const km of [50, 100, 200, 300]) {
    // 点線は地図を動かすたびの描き直しが重いので、細く薄い実線にする
    L.circle([lat, lon], {radius: km * 1000, color: "%(dim)s", weight: 1, opacity: 0.6, fill: false,
                          interactive: false}).addTo(home);
  }
  L.circleMarker([lat, lon], {radius: 5, color: "%(marker)s", fillOpacity: 1}).bindTooltip("受信地点").addTo(home);
  map.setView([lat, lon], 8);
}
setHome(%(lat)f, %(lon)f);

// 高度で色分け（低い：橙 → 高い：紫）。1000ft 単位にして、少しの高度変化では色を変えない
function altColor(alt) {
  if (alt == null) return "#9aa3b5";
  const t = Math.max(0, Math.min(1, Math.round(alt / 1000) / 40));
  return `hsl(${Math.round(30 + t * 250)}, 85%%, 60%%)`;
}
const PLANE_ICON = L.divIcon({className: "", iconSize: [26, 26], iconAnchor: [13, 13], html:
  `<svg width="26" height="26" viewBox="0 0 24 24">
     <path d="M12 2 L13.4 9 L21 13 L21 14.6 L13.4 12.6 L13 18 L15.6 20 L15.6 21.2 L12 20.2 L8.4 21.2
              L8.4 20 L11 18 L10.6 12.6 L3 14.6 L3 13 L10.6 9 Z" stroke-width="0.8"/></svg>`});

// 毎秒呼ばれる。地図の再描画を減らすため、要素は作り直さず、変わった値だけを書き換える
const planes = {};
let latest = {};
function setIf(p, key, value, apply) {
  if (p[key] !== value) {
    p[key] = value;
    apply(value);
  }
}
function update(list, sel, showTrail) {
  const seen = new Set();
  latest = {};
  for (const a of list) {
    if (a.lat == null) continue;
    seen.add(a.icao);
    latest[a.icao] = a;
    let p = planes[a.icao];
    if (!p) {
      const m = L.marker([a.lat, a.lon], {icon: PLANE_ICON}).addTo(map);
      m.bindTooltip("", {permanent: true, direction: "right", offset: [12, 0], className: "lbl"});
      m.on("click", () => console.log("select:" + a.icao));
      const el = m.getElement();
      p = planes[a.icao] = {m, svg: el.querySelector("svg"), path: el.querySelector("path"),
                            t: L.polyline([], {weight: 1.5, opacity: 0.7, interactive: false}).addTo(map)};
    }
    const selected = a.icao === sel;
    const color = altColor(a.alt);
    const alt = a.alt == null ? "" : `<br>${a.alt.toLocaleString()} ft`;
    setIf(p, "pos", `${a.lat},${a.lon}`, () => p.m.setLatLng([a.lat, a.lon]));
    setIf(p, "track", Math.round(a.track || 0), v => p.svg.style.transform = `rotate(${v}deg)`);
    setIf(p, "color", color, v => { p.path.setAttribute("fill", v); p.t.setStyle({color: v}); });
    setIf(p, "sel", selected, v => {
      p.path.setAttribute("stroke", v ? "#ffffff" : "#0b0d11");
      p.path.setAttribute("stroke-width", v ? "1.4" : "0.8");
      p.m.setZIndexOffset(v ? 1000 : 0);
    });
    // 薄く表示：位置が古い機体、インターネットのデータの機体
    setIf(p, "opacity", a.pos_age > %(stale)d ? 0.4 : a.src === "net" ? 0.6 : 1, v => p.m.setOpacity(v));
    const net = a.src === "net" ? ' <span class="net">ネット</span>' : "";
    setIf(p, "label", `${a.callsign || a.icao}${net}${alt}`, v => p.m.setTooltipContent(v));
    // 航跡は点が増えたときだけ描き直す（上限に達した後は先頭が消えるので末尾の点も見る）
    setIf(p, "trail", showTrail ? `${a.trail.length},${a.trail[a.trail.length - 1]}` : "",
          () => p.t.setLatLngs(showTrail ? a.trail : []));
  }
  for (const k in planes) {
    if (!seen.has(k)) {
      map.removeLayer(planes[k].m);
      map.removeLayer(planes[k].t);
      delete planes[k];
    }
  }
}
let credit = null;
function setCredit(html) {   // インターネットのデータの出典表示
  if (credit) map.attributionControl.removeAttribution(credit);
  credit = html;
  if (credit) map.attributionControl.addAttribution(credit);
}
function focusPlane(icao) {
  const a = latest[icao];
  if (a) map.panTo([a.lat, a.lon]);
}
</script></body></html>
"""


# ---------------- 受信スレッド ----------------
class AdsbWorker(QtCore.QThread):
    failed = QtCore.Signal(str)

    def __init__(self, gain):
        super().__init__()
        self.lock = threading.Lock()
        self.tracker = Tracker()
        self.demod = Demodulator()
        self.gain = gain
        self._gain_dirty = False
        self._running = True
        self.dropped = 0
        self.recorder = None     # 記録中は Recorder（メインスレッドが付け外しする）

    def set_gain(self, gain):
        with self.lock:
            self.gain = gain
            self._gain_dirty = True

    def stop(self):
        self._running = False

    def snapshot(self):
        with self.lock:
            self.tracker.prune()
            return self.tracker.snapshot(), self.tracker.messages

    def run(self):
        try:
            from rtlsdr import RtlSdr
            sdr = RtlSdr()
        except Exception as e:
            self.failed.emit(f"RTL-SDRを開けませんでした。\n接続とドライバを確認してください。\n\n{e}")
            return

        blocks = queue.Queue(maxsize=20)

        def on_bytes(values, _ctx):
            try:
                blocks.put_nowait(np.frombuffer(values, dtype=np.uint8).copy())
            except queue.Full:
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
            sdr.center_freq = CENTER
            sdr.gain = self.gain
            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()
            while self._running:
                with self.lock:
                    if self._gain_dirty:
                        self._gain_dirty = False
                        sdr.gain = self.gain
                try:
                    raw = blocks.get(timeout=1.0)
                except queue.Empty:
                    continue
                msgs = self.demod.process(raw)
                if msgs:
                    now = time.time()
                    with self.lock:
                        for m in msgs:
                            self.tracker.update(m, now)
                    rec = self.recorder
                    if rec:
                        rec.messages(now, msgs)
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


class MapPage(QWebEnginePage):
    """地図上の機体クリックを console.log("select:ICAO") で受け取る"""

    def __init__(self, on_select, parent):
        super().__init__(parent)
        self.on_select = on_select

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith("select:"):
            self.on_select(message[7:])


# ---------------- ウィンドウ ----------------
class AdsbWindow(QtWidgets.QWidget):
    COLUMNS = ["ICAO", "便名", "位置", "高度 ft", "速度 kt", "方位", "昇降 ft/分", "距離 km", "機種", "受信数", "最終"]

    def __init__(self, main, gains):
        super().__init__(None, QtCore.Qt.Window)
        self.main = main
        self.gains = gains
        self.setWindowTitle("ADS-B（航空機の位置）")
        self.resize(1280, 800)
        self.worker = None
        self.recorder = None     # 記録中の Recorder
        self.replay = None       # 再生中の ReplayWorker
        self.selected = None
        self.map_ready = False
        self.closed = False
        self.home = self._load_home()
        self.last_msgs, self.last_t = 0, time.monotonic()
        self.shown = False   # ネットの機体を表示中か（表示をやめたときに消すため）

        # --- 操作 ---
        self.run_btn = QtWidgets.QPushButton("▶ 受信開始")
        self.run_btn.setProperty("kind", "primary")
        self.run_btn.setCheckable(True)
        self.run_btn.setMinimumWidth(130)
        self.gain = QtWidgets.QComboBox()
        self.gain.addItem("自動", "auto")
        for g in gains:
            self.gain.addItem(f"{g:.1f} dB", g)
        self.gain.setCurrentIndex(self.gain.findData(DEFAULT_GAIN))
        self.trail = QtWidgets.QCheckBox("航跡を表示")
        self.net_chk = QtWidgets.QCheckBox("インターネットのデータも表示")
        self.net_chk.setToolTip(f"受信地点の周辺（{RADIUS_NM}海里）の機体を、ADS-B 共有サービスから"
                                f"{NET_INTERVAL}秒ごとに取得して表示します（SDR がなくても使えます）")
        self.net_src = QtWidgets.QComboBox()
        self.net_src.addItems(list(SOURCES))
        self.net_src.setToolTip("取得先（どちらも無料・非商用向け）")
        self.net = NetFeed(self)
        self.trail.setChecked(True)
        self.rec_chk = QtWidgets.QCheckBox("記録")
        self.rec_chk.setToolTip("受信したデータ（自局・インターネット）を adsb_data フォルダに保存します。"
                                "「記録を再生」で後から見られます")
        self.open_btn = QtWidgets.QPushButton("記録を再生…")
        self.open_btn.setToolTip("保存した記録を、記録したときの時間の流れどおりに再生します（SDR は使いません）")
        self.home_btn = QtWidgets.QPushButton("⌂ 受信地点に戻る")
        self.home_btn.setToolTip("受信地点は気象衛星ウィンドウの「観測地点」（緯度・経度）を使います")
        self.state = QtWidgets.QLabel("")
        self.state.setObjectName("StatusCard")

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.run_btn)
        bar.addSpacing(12)
        bar.addWidget(theme.caption("ゲイン"))
        bar.addWidget(self.gain)
        bar.addSpacing(12)
        bar.addWidget(self.trail)
        bar.addSpacing(12)
        bar.addWidget(self.net_chk)
        bar.addWidget(self.net_src)
        bar.addSpacing(12)
        bar.addWidget(self.rec_chk)
        bar.addStretch()
        bar.addWidget(self.open_btn)
        bar.addWidget(self.home_btn)

        # --- 再生の操作（再生中だけ表示） ---
        self.pause_btn = QtWidgets.QPushButton()
        self.pause_btn.setMinimumWidth(110)
        self.seek = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.seek.setPageStep(60)
        self.pos_lbl = QtWidgets.QLabel("")
        self.pos_lbl.setFont(theme.font(theme.MONO_FONTS, 9))
        self.speed = QtWidgets.QComboBox()
        for v in adsb_record.SPEEDS:
            self.speed.addItem(f"×{v}", v)
        self.speed.setToolTip("再生速度")
        self.end_btn = QtWidgets.QPushButton("■ 再生を終了")
        self.play_bar = QtWidgets.QWidget()
        pb = QtWidgets.QHBoxLayout(self.play_bar)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.addWidget(self.pause_btn)
        pb.addWidget(self.seek, 1)
        pb.addWidget(self.pos_lbl)
        pb.addWidget(theme.caption("速度"))
        pb.addWidget(self.speed)
        pb.addWidget(self.end_btn)
        self.play_bar.hide()

        # --- 地図 ---
        if WEBENGINE_ERR is None:
            self.map = QWebEngineView()
            page = MapPage(self.select, self.map)
            page.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            self.map.setPage(page)
            self.map.loadFinished.connect(self.on_map_loaded)
            page.renderProcessTerminated.connect(self.on_map_crashed)
            html = MAP_HTML % dict(theme.C, lat=self.home[0], lon=self.home[1], stale=STALE_SEC)
            self.map.setHtml(html, QtCore.QUrl.fromLocalFile(HERE + "/"))
        else:
            self.map = QtWidgets.QLabel(f"地図を表示できません（QtWebEngine が使えません）。\n一覧は使えます。\n\n{WEBENGINE_ERR}")
            self.map.setObjectName("ImageView")
            self.map.setAlignment(QtCore.Qt.AlignCenter)
            self.map.setWordWrap(True)

        # --- 一覧 ---
        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setFont(theme.font(theme.MONO_FONTS, 9))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.count = theme.caption("")
        list_box = QtWidgets.QGroupBox("受信中の機体")
        ll = QtWidgets.QVBoxLayout(list_box)
        ll.addWidget(self.count)
        ll.addWidget(self.table, 1)
        hint = theme.caption("行のクリックで地図をその機体へ移動します。灰色：位置不明、"
                             "薄い文字：位置がインターネットのデータ、"
                             f"{STALE_SEC}秒以上位置が更新されない機体は地図で薄く表示", "hint")
        hint.setWordWrap(True)
        ll.addWidget(hint)

        # --- 断面図（地図の下） ---
        self.profile = ProfilePanel()
        left = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        left.addWidget(self.map)
        left.addWidget(self.profile)
        left.setSizes([520, 320])
        left.setChildrenCollapsible(False)

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(list_box)
        split.setSizes([800, 480])
        split.setStretchFactor(0, 1)
        split.setChildrenCollapsible(False)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.addWidget(self.state)
        lay.addLayout(bar)
        lay.addWidget(self.play_bar)
        lay.addWidget(split, 1)

        # --- イベント ---
        self.run_btn.toggled.connect(self.on_run)
        self.gain.currentIndexChanged.connect(
            lambda: self.worker and self.worker.set_gain(self.gain.currentData()))
        self.trail.toggled.connect(self.refresh)
        self.home_btn.clicked.connect(self.go_home)
        self.net_chk.toggled.connect(self.on_net)
        self.net_src.currentIndexChanged.connect(lambda: self.net_chk.isChecked() and self.on_net(True))
        self.table.itemClicked.connect(lambda it: self.select(self.table.item(it.row(), 0).text(), pan=True))
        self.profile.selected.connect(lambda icao: self.select(icao, pan=True))
        self.rec_chk.toggled.connect(lambda: self._sync_recorder())
        self.net.received.connect(lambda planes, now: self.recorder and self.recorder.net(now, planes))
        self.open_btn.clicked.connect(self.open_replay)
        self.pause_btn.clicked.connect(self.on_pause)
        self.speed.currentIndexChanged.connect(lambda: self.replay and self.replay.set_speed(self.speed.currentData()))
        self.seek.sliderMoved.connect(self._show_seek_pos)
        self.seek.sliderReleased.connect(self.on_seek)
        self.seek.valueChanged.connect(lambda: not self.seek.isSliderDown() and self.on_seek())
        self.end_btn.clicked.connect(lambda: self.stop_replay())

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.set_status("停止中　「受信開始」で 1090MHz の受信を始めます", "idle")

    # ---------- ユーティリティ ----------
    @staticmethod
    def _load_home():
        """受信地点（気象衛星ウィンドウの観測地点）"""
        try:
            import satellite
            s = satellite.load_settings()
            return float(s["lat"]), float(s["lon"])
        except Exception:
            return 35.68, 139.77

    def set_status(self, text, tone=""):
        self.state.setText(text)
        if self.state.property("tone") != tone:
            theme.set_prop(self.state, "tone", tone)

    def js(self, code):
        if self.map_ready:
            self.map.page().runJavaScript(code)

    def on_map_loaded(self, ok):
        self.map_ready = ok
        self.refresh()

    def on_map_crashed(self, status, code):
        self.map_ready = False
        self.map.setHtml(f"<body style='background:{theme.C['plot']};color:{theme.C['muted']};"
                         "font-family:sans-serif;padding:24px'>地図の表示処理が異常終了しました"
                         f"（コード {code}）。一覧は使えます。<br>docs/troubleshooting.md の「ADS-B」を参照してください。</body>")

    def go_home(self):
        if self.replay and self.replay.ready and self.replay.rec.home:
            self.home = self.replay.rec.home      # 再生中は記録したときの受信地点
        else:
            self.home = self._load_home()
        self.js(f"setHome({self.home[0]}, {self.home[1]})")
        if self.net_chk.isChecked():
            self.on_net(True)    # 取得範囲の中心も変える
        self.refresh()

    def on_net(self, on):
        if on:
            src = self.net_src.currentText()
            self.net.start(src, self.home)
            self.js(f"setCredit({json.dumps(SOURCES[src]['credit'])})")
        else:
            self.net.stop()
            self.js("setCredit(null)")
        self._sync_recorder()
        self.refresh()

    def select(self, icao, pan=False):
        self.selected = icao
        if pan:
            self.js(f"focusPlane({json.dumps(icao)})")
        self.refresh()

    # ---------- 開始 / 停止 ----------
    @property
    def running(self):
        return self.worker is not None

    def on_run(self, on):
        if on:
            self.start()
        else:
            # 「■ 停止」を押したときは、インターネットからの取得も止める
            # （気象衛星の録音などによる一時停止では止めない）
            self.net_chk.setChecked(False)
            self.stop()

    def start(self):
        if self.closed or self.worker:
            return
        self.stop_replay()
        main = self.main
        if main.worker and main.worker.recorder:
            self.set_status("気象衛星の録音中は ADS-B を受信できません", "idle")
            self._set_btn(False)
            return
        if main.worker:   # SDR は1台なので、メイン画面の受信を止めて使う
            main.run_btn.setChecked(False)
            main.statusBar().showMessage("ADS-B 受信のため、メイン画面の受信を停止しました", 5000)
        self.worker = AdsbWorker(self.gain.currentData())
        self.worker.failed.connect(self.on_failed)
        self._sync_recorder()
        self.worker.start()
        self.last_msgs, self.last_t = 0, time.monotonic()
        self._set_btn(True)
        self.set_status("受信中（1090MHz）… 機体を探しています")

    def stop(self, reason=""):
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
        saved = self._sync_recorder()
        self._set_btn(False)
        self.set_status(reason or ("停止中" + saved), "idle")

    def _set_btn(self, on):
        self.run_btn.blockSignals(True)
        self.run_btn.setChecked(on)
        self.run_btn.blockSignals(False)
        self.run_btn.setText("■ 停止" if on else "▶ 受信開始")

    def on_failed(self, msg):
        self.stop("エラーで停止しました")
        QtWidgets.QMessageBox.critical(self, "ADS-B", msg)

    # ---------- 記録 ----------
    def _sync_recorder(self):
        """「記録」が入っていて受信中（自局かネット）なら記録し、そうでなければ閉じる。
        閉じたときは保存先を知らせる文字列を返す"""
        want = self.rec_chk.isChecked() and (self.worker is not None or self.net_chk.isChecked())
        saved = ""
        if want and not self.recorder:
            try:
                self.recorder = Recorder(self.home)
            except OSError as e:
                self.rec_chk.setChecked(False)
                QtWidgets.QMessageBox.critical(self, "ADS-B", f"記録用のファイルを作れませんでした。\n\n{e}")
        elif not want and self.recorder:
            self.recorder.close()
            saved = f"　｜ 記録を保存しました：{self.recorder.name}（{self.recorder.count:,} 件）"
            self.recorder = None
            if not self.worker and not self.net_chk.isChecked():
                self.set_status("停止中" + saved, "idle")
        if self.worker:
            self.worker.recorder = self.recorder
        return saved

    # ---------- 再生 ----------
    def open_replay(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "ADS-B の記録を再生", adsb_record.DATA_DIR if os.path.isdir(adsb_record.DATA_DIR) else HERE,
            "ADS-B の記録 (*.log.gz *.log);;すべて (*)")
        if not path:
            return
        self.stop_replay(clear=False)
        self.stop()                    # SDR とネットの受信は止めて、記録だけを表示する
        self.net_chk.setChecked(False)
        self.replay = ReplayWorker(path)
        self.replay.set_speed(self.speed.currentData())
        self.replay.failed.connect(self.on_replay_failed)
        self.replay.loaded.connect(self.on_replay_loaded)
        self.replay.ended.connect(self._update_play_bar)
        self.replay.start()
        for w in (self.run_btn, self.net_chk, self.net_src, self.rec_chk):
            w.setEnabled(False)
        self.play_bar.show()
        self.play_bar.setEnabled(False)
        self.replay_name = os.path.basename(path)
        self.set_status(f"記録を読み込み中… {self.replay_name}", "idle")

    def on_replay_loaded(self):
        rec = self.replay.rec
        self.seek.blockSignals(True)
        self.seek.setRange(0, max(int(rec.end - rec.start), 1))
        self.seek.setValue(0)
        self.seek.blockSignals(False)
        self.play_bar.setEnabled(True)
        self.go_home()
        self._update_play_bar()

    def on_replay_failed(self, msg):
        self.stop_replay()
        QtWidgets.QMessageBox.critical(self, "ADS-B", msg)

    def stop_replay(self, clear=True):
        if not self.replay:
            return
        self.replay.stop()
        self.replay.wait(5000)
        self.replay = None
        self.play_bar.hide()
        for w in (self.run_btn, self.net_chk, self.net_src, self.rec_chk):
            w.setEnabled(True)
        if clear:
            self.go_home()          # 受信地点を元に戻し、再生していた機体を消す
            self.set_status("停止中", "idle")

    def on_pause(self):
        if self.replay and self.replay.ready:
            self.replay.set_paused(not self.replay.paused)
            self._update_play_bar()

    def on_seek(self):
        if self.replay and self.replay.ready:
            self.replay.seek(self.replay.rec.start + self.seek.value())
            self._show_seek_pos(self.seek.value())

    def _show_seek_pos(self, value):
        rec = self.replay.rec
        self.pos_lbl.setText(f"{adsb_record.clock(rec.start + value)}　"
                             f"{adsb_record.duration(value)} / {adsb_record.duration(rec.end - rec.start)}")

    def _update_play_bar(self):
        r = self.replay
        if not r or not r.ready:
            return
        self.pause_btn.setText("▶ 再生" if r.paused else "⏸ 一時停止")
        if not self.seek.isSliderDown():
            self.seek.blockSignals(True)
            self.seek.setValue(int(r.vt - r.rec.start))
            self.seek.blockSignals(False)
            self._show_seek_pos(r.vt - r.rec.start)

    # ---------- 表示更新 ----------
    def refresh(self):
        net_on = self.net_chk.isChecked()
        if not self.worker and not net_on and not self.replay and not self.shown:
            return      # 何も更新するものがない（停止後は最後の表示を残す）
        status = []
        local, net = [], []
        if self.replay:
            if not self.replay.ready:
                self.set_status(f"記録を読み込み中… {self.replay_name}（{self.replay.lines:,} 行）", "idle")
                return
            local, net = self.replay.snapshot()
            with_pos = sum(a["lat"] is not None for a in local)
            r = self.replay
            state = ("再生終了（▶ で最初から）" if r.vt >= r.rec.end else "一時停止") if r.paused else f"再生中 ×{r.speed}"
            status.append(f"{state}：{self.replay_name}"
                          f"　｜ 自局の機体 {len(local)}（位置あり {with_pos}）　{r.rate()} メッセージ/秒"
                          + (f"　｜ ネット {len(net)}機" if r.rec.nets else ""))
            self._update_play_bar()
        if self.worker:
            local, total = self.worker.snapshot()
            now = time.monotonic()
            rate = (total - self.last_msgs) / max(now - self.last_t, 1e-3)
            self.last_msgs, self.last_t = total, now
            with_pos = sum(a["lat"] is not None for a in local)
            status.append(f"受信中：機体 {len(local)}（位置あり {with_pos}）　{rate:.0f} メッセージ/秒"
                          f"　｜ 累計 {total}　処理落ち {self.worker.dropped}回")
        if net_on:
            status.append(self.net.status())
            net = self.net.snapshot()
        if self.recorder:
            status.append(self.recorder.status())
        # ネットの表示・再生をやめたときは、その機体を消すために1回だけ更新する
        self.shown = net_on or self.replay is not None
        planes = merge(local, net)

        home = self.home
        for a in planes:
            a["dist"] = distance_km(home[0], home[1], a["lat"], a["lon"]) if a["lat"] is not None else None
        planes.sort(key=lambda a: (a["dist"] is None, a["dist"] or 0, a["icao"]))
        if status:
            self.set_status("　｜ ".join(status))
        n_local = sum(a["src"] == "local" and a["lat"] is not None for a in planes)
        n_net = sum(a["src"] == "net" for a in planes)
        self.count.setText(f"{len(planes)} 機（位置：自局 {n_local}・ネット {n_net}）" if net else f"{len(planes)} 機")

        self.js(f"update({json.dumps(planes)}, {json.dumps(self.selected)}, {json.dumps(self.trail.isChecked())})")
        self._fill_table(planes)
        self.profile.update_planes(planes, self.selected, self.home)

    def _fill_table(self, planes):
        t = self.table
        t.setUpdatesEnabled(False)
        t.setRowCount(len(planes))
        fmt = lambda v, f="{:,}": "" if v is None else f.format(v)  # noqa: E731
        sel_row = None
        for r, a in enumerate(planes):
            src = "" if a["lat"] is None else "自局" if a["src"] == "local" else "ネット"
            vals = [a["icao"], a["callsign"], src, fmt(a["alt"]), fmt(a["speed"]),
                    fmt(a["track"], "{:.0f}°"), fmt(a["vrate"], "{:+,}"), fmt(a["dist"], "{:.0f}"),
                    a["type"], fmt(a["msgs"]), f"{a['age']:.0f}秒前"]
            color = theme.C["dim" if a["lat"] is None else "muted" if a["src"] == "net" else "text"]
            for c, v in enumerate(vals):
                it = t.item(r, c)
                if it is None:
                    it = QtWidgets.QTableWidgetItem()
                    t.setItem(r, c, it)
                it.setText(v)
                left = c < 3 or c == 8
                it.setTextAlignment((QtCore.Qt.AlignLeft if left else QtCore.Qt.AlignRight) | QtCore.Qt.AlignVCenter)
                it.setForeground(QtGui.QColor(color))
            if a["icao"] == self.selected:
                sel_row = r
        t.blockSignals(True)
        if sel_row is None:
            t.clearSelection()
        else:
            t.selectRow(sel_row)
        t.blockSignals(False)
        t.setUpdatesEnabled(True)

    # ---------- 終了処理 ----------
    def closeEvent(self, ev):
        # 閉じたら SDR を空け、ネットからの取得も止める
        self.stop()
        self.net_chk.setChecked(False)
        self.stop_replay()
        super().closeEvent(ev)

    def shutdown(self):
        self.closed = True
        self.stop()
        self.net.stop()
        self.stop_replay()
        self.rec_chk.setChecked(False)

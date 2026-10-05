"""
ADS-B（1090MHz）受信ウィンドウ

  航空機が送信している ADS-B を受信して、機体の位置・便名・高度・速度を地図と一覧に表示する。
  受信中は SDR を 1090MHz・2MS/s で使うため、メイン画面の受信とは同時に使えない。
  地図は Leaflet（インターネット接続が必要）。QtWebEngine がない環境では一覧だけ表示する。
"""
import json
import os
import queue
import sys
import threading
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import theme
from adsb_decoder import BLOCK, CENTER, FS, Demodulator, Tracker, distance_km

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
    setIf(p, "stale", a.pos_age > %(stale)d, v => p.m.setOpacity(v ? 0.4 : 1));
    setIf(p, "label", `${a.callsign || a.icao}${alt}`, v => p.m.setTooltipContent(v));
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
    COLUMNS = ["ICAO", "便名", "高度 ft", "速度 kt", "方位", "昇降 ft/分", "距離 km", "受信数", "最終"]

    def __init__(self, main, gains):
        super().__init__(None, QtCore.Qt.Window)
        self.main = main
        self.gains = gains
        self.setWindowTitle("ADS-B（航空機の位置）")
        self.resize(1280, 800)
        self.worker = None
        self.selected = None
        self.map_ready = False
        self.closed = False
        self.home = self._load_home()
        self.last_msgs, self.last_t = 0, time.monotonic()

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
        self.trail.setChecked(True)
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
        bar.addStretch()
        bar.addWidget(self.home_btn)

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
                             f"{STALE_SEC}秒以上位置が更新されない機体は地図で薄く表示", "hint")
        hint.setWordWrap(True)
        ll.addWidget(hint)

        split = QtWidgets.QSplitter()
        split.addWidget(self.map)
        split.addWidget(list_box)
        split.setSizes([800, 480])
        split.setStretchFactor(0, 1)
        split.setChildrenCollapsible(False)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.addWidget(self.state)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        # --- イベント ---
        self.run_btn.toggled.connect(self.on_run)
        self.gain.currentIndexChanged.connect(
            lambda: self.worker and self.worker.set_gain(self.gain.currentData()))
        self.trail.toggled.connect(self.refresh)
        self.home_btn.clicked.connect(self.go_home)
        self.table.itemClicked.connect(lambda it: self.select(self.table.item(it.row(), 0).text(), pan=True))

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
        self.home = self._load_home()
        self.js(f"setHome({self.home[0]}, {self.home[1]})")
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
            self.stop()

    def start(self):
        if self.closed or self.worker:
            return
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
        self.worker.start()
        self.last_msgs, self.last_t = 0, time.monotonic()
        self._set_btn(True)
        self.set_status("受信中（1090MHz）… 機体を探しています")

    def stop(self, reason=""):
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
        self._set_btn(False)
        self.set_status(reason or "停止中", "idle")

    def _set_btn(self, on):
        self.run_btn.blockSignals(True)
        self.run_btn.setChecked(on)
        self.run_btn.blockSignals(False)
        self.run_btn.setText("■ 停止" if on else "▶ 受信開始")

    def on_failed(self, msg):
        self.stop("エラーで停止しました")
        QtWidgets.QMessageBox.critical(self, "ADS-B", msg)

    # ---------- 表示更新 ----------
    def refresh(self):
        if not self.worker:
            return
        planes, total = self.worker.snapshot()
        now = time.monotonic()
        rate = (total - self.last_msgs) / max(now - self.last_t, 1e-3)
        self.last_msgs, self.last_t = total, now

        home = self.home
        for a in planes:
            a["dist"] = distance_km(home[0], home[1], a["lat"], a["lon"]) if a["lat"] is not None else None
        planes.sort(key=lambda a: (a["dist"] is None, a["dist"] or 0, a["icao"]))
        with_pos = sum(a["lat"] is not None for a in planes)
        self.set_status(f"受信中：機体 {len(planes)}（位置あり {with_pos}）　{rate:.0f} メッセージ/秒"
                        f"　｜ 累計 {total}　処理落ち {self.worker.dropped}回")
        self.count.setText(f"{len(planes)} 機")

        self.js(f"update({json.dumps(planes)}, {json.dumps(self.selected)}, {json.dumps(self.trail.isChecked())})")
        self._fill_table(planes)

    def _fill_table(self, planes):
        t = self.table
        t.setUpdatesEnabled(False)
        t.setRowCount(len(planes))
        fmt = lambda v, f="{:,}": "" if v is None else f.format(v)  # noqa: E731
        sel_row = None
        for r, a in enumerate(planes):
            vals = [a["icao"], a["callsign"], fmt(a["alt"]), fmt(a["speed"]),
                    fmt(a["track"], "{:.0f}°"), fmt(a["vrate"], "{:+,}"), fmt(a["dist"], "{:.0f}"),
                    str(a["msgs"]), f"{a['age']:.0f}秒前"]
            for c, v in enumerate(vals):
                it = t.item(r, c)
                if it is None:
                    it = QtWidgets.QTableWidgetItem()
                    t.setItem(r, c, it)
                it.setText(v)
                it.setTextAlignment((QtCore.Qt.AlignLeft if c < 2 else QtCore.Qt.AlignRight) | QtCore.Qt.AlignVCenter)
                it.setForeground(QtGui.QColor(theme.C["text" if a["lat"] is not None else "dim"]))
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
        # 閉じたら SDR を空ける
        self.stop()
        super().closeEvent(ev)

    def shutdown(self):
        self.closed = True
        self.stop()

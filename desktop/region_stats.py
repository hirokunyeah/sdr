"""
地域統計ウィンドウ

  人口・世帯・住宅・自動車・所得などの市区町村別の統計を、地図の色分け（コロプレス図）と順位の一覧で表示する。
  データの取得とキャッシュは region_data.py。地図は Leaflet（インターネット接続が必要）。
  単独でも起動できる：python desktop/region_stats.py
"""
import html
import json
import math
import sys
import threading

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

import adsb    # 地図（QtWebEngine）の起動設定と MapPage を共用する
import region_data as rd
import theme

ALL = "全国"
SPEEDS = [("速い（0.5秒）", 500), ("ふつう（1秒）", 1000), ("ゆっくり（2秒）", 2000)]   # 1年分を表示する時間

MAP_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/topojson-client@3.1.0/dist/topojson-client.min.js"></script>
<style>
  html, body, #map { margin: 0; height: 100%%; background: %(plot)s; }
  body { font-family: sans-serif; color: %(text)s; }
  #err { padding: 24px; color: %(muted)s; }
  .leaflet-container { background: %(plot)s; }
  .leaflet-control-attribution { background: rgba(15,17,21,.7) !important; color: %(muted)s; }
  .leaflet-control-attribution a { color: %(accent)s; }
  .leaflet-control-layers { background: %(panel)s; color: %(text)s; border: 1px solid %(line)s !important; }
  .leaflet-tooltip.tip { background: %(panel)s; color: %(text)s; border: 1px solid %(line)s;
                         font: 12px/1.5 sans-serif; box-shadow: none; }
  .leaflet-tooltip.tip::before { display: none; }
  .tip .v { font: bold 13px monospace; }
  .tip .r { color: %(muted)s; }
  .legend { background: rgba(24,27,34,.92); border: 1px solid %(line)s; border-radius: 8px;
            padding: 8px 10px; font: 11px/1.6 sans-serif; color: %(text)s; min-width: 120px; }
  .legend .t { color: %(muted)s; margin-bottom: 2px; }
  .legend .row { display: flex; align-items: center; gap: 6px; font-family: monospace; }
  .legend .sw { width: 14px; height: 10px; border-radius: 2px; flex: none; }
</style></head>
<body><div id="map"></div>
<script>
if (typeof L === "undefined" || typeof topojson === "undefined") {
  document.body.innerHTML = '<div id="err">地図を読み込めませんでした（インターネット接続が必要です）。一覧は使えます。</div>';
}
const map = L.map("map", {minZoom: 4, maxZoom: 14, preferCanvas: true}).setView([%(lat)f, %(lon)f], 9);
// 背景は地理院タイル。淡色地図は読み込み時に1回だけ色を反転して暗い画面に合わせる（adsb.py と同じ方法）
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
const bases = {"淡色地図（暗）": gsi("pale", "png", true), "標準地図": gsi("std", "png")};
bases["淡色地図（暗）"].addTo(map);
L.control.layers(bases, null, {position: "topright"}).addTo(map);
map.attributionControl.addAttribution(%(credit)s);
L.circleMarker([%(lat)f, %(lon)f], {radius: 5, color: "%(marker)s", fillOpacity: 1, interactive: false})
  .addTo(map);

const layers = {};     // 市区町村コード → 図形（Leaflet のレイヤー）
let shown = {};        // 市区町村コード → [色, ツールチップ]。含まれないコードは表示の対象外
let sel = null;
function styleOf(code) {
  const s = shown[code];
  const selected = code === sel;
  if (!s) return {stroke: true, color: "%(line)s", weight: 0.3, fillOpacity: 0, opacity: 0.5};
  return {stroke: true, color: selected ? "#ffffff" : "%(plot)s", weight: selected ? 2.5 : 0.5,
          opacity: 1, fillColor: s[0], fillOpacity: s[0] === "%(nodata)s" ? 0.5 : 0.82};
}
function setBoundaries(topo) {
  const fc = topojson.feature(topo, Object.values(topo.objects)[0]);
  L.geoJSON(fc, {
    filter: f => !!f.properties.N03_007,
    style: f => styleOf(f.properties.N03_007),
    onEachFeature: (f, layer) => {
      const code = f.properties.N03_007;
      layers[code] = layer;
      layer.bindTooltip(() => (shown[code] || [null, ""])[1] ||
                              `${f.properties.N03_001} ${f.properties.N03_003 || f.properties.N03_004 || ""}`,
                        {sticky: true, className: "tip", direction: "top", offset: [0, -8]});
      layer.on("click", () => console.log("select:" + code));
    }
  }).addTo(map);
}
function setData(data) {
  shown = data;
  for (const code in layers) layers[code].setStyle(styleOf(code));
  if (sel && layers[sel]) layers[sel].bringToFront();
}
function select(code) {
  const prev = sel;
  sel = code;
  if (prev && layers[prev]) layers[prev].setStyle(styleOf(prev));
  if (sel && layers[sel]) {
    layers[sel].setStyle(styleOf(sel));
    layers[sel].bringToFront();
  }
}
function focusCode(code) {
  if (layers[code]) map.flyToBounds(layers[code].getBounds(), {maxZoom: 11, duration: 0.6});
}
function fitCodes(codes) {
  // 小笠原などの遠い離島まで入れると本土が小さくなるので、中心から離れた市区町村は除いて合わせる
  const centers = codes.filter(c => layers[c]).map(c => [c, layers[c].getBounds().getCenter()]);
  if (!centers.length) return;
  const median = a => a.slice().sort((x, y) => x - y)[Math.floor(a.length / 2)];
  const lat = median(centers.map(([, p]) => p.lat)), lng = median(centers.map(([, p]) => p.lng));
  const dist = ([, p]) => Math.hypot(p.lat - lat, p.lng - lng);
  const limit = Math.max(1.5, 2.5 * median(centers.map(dist)));
  let b = null;
  for (const [c, p] of centers) {
    if (dist([c, p]) > limit) continue;
    b = b ? b.extend(layers[c].getBounds()) : L.latLngBounds(layers[c].getBounds());
  }
  if (b) map.flyToBounds(b, {padding: [16, 16], duration: 0.6});
}
const legend = L.control({position: "bottomright"});
legend.onAdd = () => { const d = L.DomUtil.create("div", "legend"); d.style.display = "none"; return d; };
legend.addTo(map);
function setLegend(html) {
  const d = legend.getContainer();
  d.innerHTML = html;
  d.style.display = html ? "" : "none";
}
</script></body></html>
"""


class NumItem(QtWidgets.QTableWidgetItem):
    """数値で並べ替える表のセル（値は UserRole に入れる）"""

    def __lt__(self, other):
        a, b = self.data(QtCore.Qt.UserRole), other.data(QtCore.Qt.UserRole)
        if a is None or b is None:
            return (a is None) < (b is None)
        return a < b


class NumAxis(pg.AxisItem):
    """目盛りを 1,880,000 のような3桁区切りで表示する軸（既定の 1.88e+06 の表記を避ける）

    log10 に変換した値を描くときは logMode を True にすると、目盛りを元の値（10 の何乗か）で表示する
    """

    def tickStrings(self, values, scale, spacing):
        if self.logMode:
            # 1・2・5 の位置だけ数字を付ける（間の目盛りまで書くと重なって読めない）
            out = []
            for v in values:
                x = 10 ** v
                lead = round(x / 10 ** math.floor(math.log10(x)), 6)
                if lead not in (1, 2, 5):
                    out.append("")
                else:
                    out.append(f"{x:,.0f}" if x >= 1 else f"{x:.3g}")
            return out
        digits = max(0, min(3, -int(f"{spacing * scale:e}".split("e")[1])))
        return [f"{v * scale:,.{digits}f}" for v in values]


def indicator_combo(key="pop"):
    """指標を選ぶコンボボックス（分類の見出し付き。currentData() が指標のキー）"""
    combo = QtWidgets.QComboBox()
    combo.setMaxVisibleItems(30)
    group = None
    for ind in rd.INDICATORS:
        if ind.group != group:
            group = ind.group
            combo.addItem(f"―― {group} ――")
            combo.model().item(combo.count() - 1).setEnabled(False)
        combo.addItem(f"  {ind.name}", ind.key)
    combo.setCurrentIndex(combo.findData(key))
    return combo


class Loader(QtCore.QObject):
    """データの取得を別スレッドで行い、結果をメインスレッドで受け取る"""
    done = QtCore.Signal(str, object, str)   # (要求の名前, 結果, エラー)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.gen = {}     # 要求の名前 → 世代。新しい要求を出したら古い結果は捨てる

    def request(self, name, func):
        gen = self.gen[name] = self.gen.get(name, 0) + 1

        def run():
            try:
                result, err = func(), ""
            except Exception as e:
                result, err = None, f"{type(e).__name__}: {e}"
            if self.gen.get(name) == gen:
                self.done.emit(name, result, err)

        threading.Thread(target=run, daemon=True).start()


class RegionStatsWindow(QtWidgets.QWidget):
    COLUMNS = ["順位", "都道府県", "市区町村", "値"]

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle("地域統計（市区町村別）")
        self.resize(1280, 800)
        self.home = adsb.AdsbWindow._load_home()
        self.topo = None
        self.regions = {}          # 市区町村コード → (都道府県名, 市区町村名)
        self.series = {}           # 年 → {市区町村コード: 値}（表示中の指標）
        self.years = []            # データのある年（古い順）。スライダーの位置と対応
        self.rows = []             # 一覧の行：(順位, コード, 値)
        self.summary_base = ""
        self.selected = None
        self.map_ready = False
        self.topo_sent = False
        self.loader = Loader(self)
        self.loader.done.connect(self.on_loaded)

        # --- 操作 ---
        self.indicator = indicator_combo()
        self.year = QtWidgets.QComboBox()
        self.year.setMinimumWidth(90)
        self.pref = QtWidgets.QComboBox()
        self.pref.addItem(ALL)
        self.pref.setMinimumWidth(110)
        self.pref.setToolTip("都道府県を選ぶと、その中で色分け・順位付けします")
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("市区町村名で探す")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.refresh_btn = QtWidgets.QPushButton("⟳ 取り直す")
        self.refresh_btn.setToolTip(f"統計値は {rd.CACHE_DAYS} 日間キャッシュします。最新のデータを今すぐ取り直します")
        self.chart_btn = QtWidgets.QPushButton("📈 グラフで分析")
        self.chart_btn.setToolTip("推移の比較・将来の推計、2つの指標の関係（散布図）をグラフで見ます")
        self.chart_window = None
        self.home_btn = QtWidgets.QPushButton("⌂ 観測地点")
        self.home_btn.setToolTip("気象衛星ウィンドウの「観測地点」のある市区町村を選びます")
        self.state = QtWidgets.QLabel("")
        self.state.setObjectName("StatusCard")

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(theme.caption("指標"))
        bar.addWidget(self.indicator)
        bar.addSpacing(8)
        bar.addWidget(theme.caption("年"))
        bar.addWidget(self.year)
        bar.addSpacing(8)
        bar.addWidget(theme.caption("範囲"))
        bar.addWidget(self.pref)
        bar.addSpacing(8)
        bar.addWidget(self.search)
        bar.addStretch()
        bar.addWidget(self.chart_btn)
        bar.addWidget(self.home_btn)
        bar.addWidget(self.refresh_btn)

        # --- 推移の再生 ---
        self.play_btn = QtWidgets.QPushButton("▶ 推移を再生")
        self.play_btn.setProperty("kind", "primary")
        self.play_btn.setCheckable(True)
        self.play_btn.setMinimumWidth(130)
        self.play_btn.setToolTip("最初の年から順に、年ごとの地図を切り替えて表示します")
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setPageStep(1)
        self.slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.slider.setTickInterval(1)
        self.year_lbl = QtWidgets.QLabel("")
        self.year_lbl.setFont(theme.font(theme.MONO_FONTS, 12, bold=True))
        self.year_lbl.setMinimumWidth(80)
        self.speed = QtWidgets.QComboBox()
        for label, ms in SPEEDS:
            self.speed.addItem(label, ms)
        self.speed.setCurrentIndex(1)
        self.speed.setToolTip("1年分を表示する時間")
        self.fixed_chk = QtWidgets.QCheckBox("全年で同じ色分け")
        self.fixed_chk.setChecked(True)
        self.fixed_chk.setToolTip("色の区切りを全部の年の値から決めます（年ごとの増減が色の変化で分かる）。\n"
                                  "外すと年ごとに区切り直します（その年の中での差が見やすい）")
        self.play_timer = QtCore.QTimer(self)
        self.play_timer.timeout.connect(self.step_play)

        play = QtWidgets.QHBoxLayout()
        play.addWidget(self.play_btn)
        play.addSpacing(8)
        play.addWidget(self.slider, 1)
        play.addWidget(self.year_lbl)
        play.addSpacing(8)
        play.addWidget(theme.caption("速さ"))
        play.addWidget(self.speed)
        play.addSpacing(8)
        play.addWidget(self.fixed_chk)

        # --- 地図 ---
        if adsb.WEBENGINE_ERR is None:
            self.map = adsb.QWebEngineView()
            page = adsb.MapPage(lambda code: self.select(code, from_map=True), self.map)
            page.settings().setAttribute(adsb.QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            self.map.setPage(page)
            self.map.loadFinished.connect(self.on_map_loaded)
            html_text = MAP_HTML % dict(theme.C, lat=self.home[0], lon=self.home[1], nodata=rd.NO_DATA,
                                        credit=json.dumps(rd.CREDIT))
            self.map.setHtml(html_text, QtCore.QUrl.fromLocalFile(adsb.HERE + "/"))
        else:
            self.map = QtWidgets.QLabel(f"地図を表示できません（QtWebEngine が使えません）。\n一覧は使えます。\n\n{adsb.WEBENGINE_ERR}")
            self.map.setObjectName("ImageView")
            self.map.setAlignment(QtCore.Qt.AlignCenter)
            self.map.setWordWrap(True)

        # --- 右側：要約と順位の一覧 ---
        self.title = QtWidgets.QLabel("")
        self.title.setFont(theme.font(theme.UI_FONTS, 13, bold=True))
        self.title.setWordWrap(True)
        self.summary = QtWidgets.QLabel("")
        self.summary.setFont(theme.font(theme.MONO_FONTS, 9))
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        # 推移のグラフ（選んだ市区町村と、表示範囲の中央値）
        self.trend = pg.PlotWidget(background=theme.C["plot"], axisItems={"left": NumAxis("left")})
        self.trend.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.trend.setFixedHeight(170)
        self.trend.showGrid(x=True, y=True, alpha=0.15)
        self.trend.setMenuEnabled(False)
        self.trend.hideButtons()
        self.trend.setMouseEnabled(x=False, y=False)
        axis_font = theme.font(theme.MONO_FONTS, 8)
        for ax in ("left", "bottom"):
            a = self.trend.getAxis(ax)
            a.enableAutoSIPrefix(False)
            a.setTickFont(axis_font)
            a.setPen(theme.C["line"])
            a.setTextPen(theme.C["muted"])
        self.median_curve = self.trend.plot([], [], pen=pg.mkPen(theme.C["muted"], width=2, style=QtCore.Qt.DashLine))
        self.sel_curve = self.trend.plot([], [], pen=pg.mkPen(theme.C["accent"], width=2),
                                         symbol="o", symbolSize=8, symbolBrush=theme.C["accent"],
                                         symbolPen=pg.mkPen(theme.C["plot"], width=2))
        self.year_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(theme.C["marker"], width=1, style=QtCore.Qt.DashLine))
        self.trend.addItem(self.year_line)
        # 凡例は線に重ならないようグラフの上に置く
        self.trend_legend = theme.caption(
            f"推移　<span style='color:{theme.C['accent']}'>━●</span> 選んだ市区町村　"
            f"<span style='color:{theme.C['muted']}'>╍╍</span> 範囲の中央値　"
            f"<span style='color:{theme.C['marker']}'>┆</span> 表示中の年")
        self.trend_legend.setTextFormat(QtCore.Qt.RichText)

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        # 列幅の自動調整（ResizeToContents）はセルを入れるたびに全行を測り直して遅いので、
        # 一覧を作り終えたときに1回だけ合わせる
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setSortingEnabled(True)
        self.note = theme.caption("", "hint")
        self.note.setWordWrap(True)
        self.note.setTextFormat(QtCore.Qt.RichText)
        self.note.setOpenExternalLinks(True)

        side = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.title)
        sl.addWidget(self.summary)
        sl.addWidget(self.trend_legend)
        sl.addWidget(self.trend)
        sl.addWidget(self.table, 1)
        sl.addWidget(self.note)

        split = QtWidgets.QSplitter()
        split.addWidget(self.map)
        split.addWidget(side)
        split.setSizes([860, 420])
        split.setStretchFactor(0, 1)
        split.setChildrenCollapsible(False)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.addWidget(self.state)
        lay.addLayout(bar)
        lay.addLayout(play)
        lay.addWidget(split, 1)

        # --- イベント ---
        self.indicator.currentIndexChanged.connect(lambda: self.load_indicator())
        self.year.currentIndexChanged.connect(self.on_year)
        self.slider.valueChanged.connect(self.on_slider)
        self.play_btn.toggled.connect(self.on_play)
        self.speed.currentIndexChanged.connect(lambda: self.play_timer.setInterval(self.speed.currentData()))
        self.fixed_chk.toggled.connect(self.redraw)
        self.pref.currentIndexChanged.connect(self.on_pref)
        self.search.returnPressed.connect(self.on_search)
        self.refresh_btn.clicked.connect(lambda: self.load_indicator(refresh=True))
        self.home_btn.clicked.connect(self.go_home)
        self.chart_btn.clicked.connect(self.open_charts)
        self.table.itemSelectionChanged.connect(self.on_table_select)

        self.set_status("市区町村の境界を読み込み中…", "busy")
        self.loader.request("topo", rd.boundaries)

    # ---------- ユーティリティ ----------
    def set_status(self, text, tone=""):
        self.state.setText(text)
        if self.state.property("tone") != tone:
            theme.set_prop(self.state, "tone", tone)

    def js(self, code):
        if self.map_ready:
            self.map.page().runJavaScript(code)

    def current(self):
        return rd.BY_KEY[self.indicator.currentData()]

    def fmt(self, v, ind=None):
        ind = ind or self.current()
        return f"{v:,.{ind.digits}f}"

    def name_of(self, code):
        pref, name = self.regions.get(code, ("", code))
        return f"{pref} {name}"

    # ---------- 読み込み ----------
    def on_map_loaded(self, ok):
        self.map_ready = ok
        self.send_topo()

    def send_topo(self):
        if self.map_ready and self.topo and not self.topo_sent:
            self.topo_sent = True
            self.js(f"setBoundaries({json.dumps(self.topo, ensure_ascii=False)})")
            self.redraw()

    def load_indicator(self, refresh=False):
        key = self.indicator.currentData()
        if key is None or not self.topo:
            return
        ind = rd.BY_KEY[key]
        self.play_btn.setChecked(False)
        self.set_status(f"「{ind.name}」を取得中…" + ("（最新のデータを取り直しています）" if refresh else ""), "busy")
        self.indicator.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.loader.request("data", lambda: (key, rd.load(key, refresh)))

    def on_loaded(self, name, result, err):
        if name == "topo":
            if err:
                self.set_status(f"市区町村の境界を取得できませんでした：{err}", "rec")
                return
            self.topo = result
            self.regions = rd.regions(result)
            prefs = sorted({p for p, _ in self.regions.values()}, key=lambda p: min(
                c for c, (q, _) in self.regions.items() if q == p))   # コード順（北から）
            self.pref.blockSignals(True)
            self.pref.addItems(prefs)
            self.pref.blockSignals(False)
            names = [self.name_of(c) for c in sorted(self.regions)]
            completer = QtWidgets.QCompleter(names, self)
            completer.setFilterMode(QtCore.Qt.MatchContains)
            completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
            completer.activated.connect(lambda text: self.on_search(text))
            self.search.setCompleter(completer)
            self.send_topo()
            self.load_indicator()
            return
        self.indicator.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        if err:
            self.set_status(f"データを取得できませんでした：{err}", "rec")
            return
        key, series = result
        if key != self.indicator.currentData():
            return
        self.series = series
        prev = self.year.currentText()
        years = sorted(series, reverse=True)
        self.year.blockSignals(True)
        self.year.clear()
        self.year.addItems(years)
        if prev in years:
            self.year.setCurrentText(prev)
        self.year.blockSignals(False)
        self.years = sorted(series)
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, len(self.years) - 1))
        self.slider.blockSignals(False)
        self.play_btn.setEnabled(len(self.years) > 1)
        self.slider.setEnabled(len(self.years) > 1)
        self.on_year()

    # ---------- 年の切り替え・推移の再生 ----------
    def on_year(self):
        """年のコンボボックスを変えたとき（スライダーを合わせて描き直す）"""
        year = self.year.currentText()
        if year in self.years:
            self.slider.blockSignals(True)
            self.slider.setValue(self.years.index(year))
            self.slider.blockSignals(False)
        self.year_lbl.setText(f"{year}年" if year else "")
        self.redraw()

    def on_slider(self, i):
        if 0 <= i < len(self.years):
            self.year.setCurrentText(self.years[i])   # → on_year

    def on_play(self, on):
        if on:
            if self.slider.value() >= self.slider.maximum():   # 最後の年なら最初から
                self.slider.setValue(0)
            self.play_btn.setText("⏸ 一時停止")
            self.play_timer.start(self.speed.currentData())
        else:
            self.play_timer.stop()
            self.play_btn.setText("▶ 推移を再生")
            if self.series:
                self.redraw()      # 状態の表示から「再生中」を消す

    def step_play(self):
        i = self.slider.value() + 1
        if i > self.slider.maximum():
            self.play_btn.setChecked(False)
            return
        self.slider.setValue(i)
        if i >= self.slider.maximum():    # 最後の年を表示したら止める
            self.play_btn.setChecked(False)

    # ---------- 表示 ----------
    def target_codes(self):
        """表示の対象（全国、または選んだ都道府県）の市区町村コード"""
        pref = self.pref.currentText()
        return [c for c, (p, _) in self.regions.items() if pref == ALL or p == pref]

    def redraw(self):
        if not self.series or not self.year.currentText():
            return
        ind = self.current()
        year = self.year.currentText()
        values = self.series.get(year, {})
        codes = self.target_codes()
        have = [(values[c], c) for c in codes if c in values]
        have.sort(reverse=True)
        fixed = self.fixed_chk.isChecked()
        if fixed:   # 全部の年の値から区切りを決めて、年が変わっても同じ色が同じ値を表すようにする
            pool = [s[c] for s in self.series.values() for c in codes if c in s]
        else:
            pool = [v for v, _ in have]
        breaks, colors = rd.classify(pool, ind.diverging)

        # 順位（同じ値は同順位）
        self.rows, rank, prev = [], 0, None
        for i, (v, c) in enumerate(have):
            if v != prev:
                rank, prev = i + 1, v
            self.rows.append((rank, c, v))
        rank_of = {c: r for r, c, _ in self.rows}
        scope = self.pref.currentText()
        n = len(self.rows)

        # 地図
        shown = {}
        for c in codes:
            name = html.escape(self.name_of(c))
            if c in values:
                v = values[c]
                tip = (f"<b>{name}</b><br><span class='v'>{self.fmt(v)}</span> {html.escape(ind.unit)}"
                       f"<br><span class='r'>{scope} {rank_of[c]:,}位 / {n:,}</span>")
                shown[c] = [rd.color_of(v, breaks, colors), tip]
            else:
                shown[c] = [rd.NO_DATA, f"<b>{name}</b><br><span class='r'>データなし</span>"]
        self.js(f"setData({json.dumps(shown, ensure_ascii=False)})")
        self.js(f"setLegend({json.dumps(self.legend_html(ind, year, breaks, colors, fixed), ensure_ascii=False)})")

        # 一覧
        t = self.table
        t.setSortingEnabled(False)
        t.setUpdatesEnabled(False)
        t.blockSignals(True)
        t.setRowCount(len(self.rows))
        mono = theme.font(theme.MONO_FONTS, 9)
        for row, (r, c, v) in enumerate(self.rows):
            pref, name = self.regions[c]
            items = [NumItem(f"{r:,}"), QtWidgets.QTableWidgetItem(pref),
                     QtWidgets.QTableWidgetItem(name), NumItem(self.fmt(v))]
            items[0].setData(QtCore.Qt.UserRole, r)
            items[3].setData(QtCore.Qt.UserRole, v)
            for k in (0, 3):
                items[k].setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                items[k].setFont(mono)
            items[0].setData(QtCore.Qt.UserRole + 1, c)
            for k, it in enumerate(items):
                t.setItem(row, k, it)
        t.setHorizontalHeaderItem(3, QtWidgets.QTableWidgetItem(f"{ind.unit}"))
        t.setSortingEnabled(True)
        t.sortItems(0, QtCore.Qt.AscendingOrder)
        for k in (0, 1, 3):
            t.resizeColumnToContents(k)
        t.blockSignals(False)
        t.setUpdatesEnabled(True)

        # 要約
        self.title.setText(f"{ind.name}（{ind.unit}）　{year}年")
        missing = len(codes) - n
        lines = []
        if have:
            vs = sorted(v for v, _ in have)
            med = rd.median(vs)
            lines.append(f"{scope} {n:,} 市区町村　中央値 {self.fmt(med)}　"
                         f"最大 {self.fmt(vs[-1])}　最小 {self.fmt(vs[0])}")
        if missing:
            lines.append(f"データなし {missing:,} 市区町村（地図では灰色）")
        self.summary_base = "\n".join(lines)
        note = html.escape(ind.note) + "<br>" if ind.note else ""
        credit = rd.CREDIT.replace("<a ", f"<a style='color:{theme.C['accent']}' ")
        self.note.setText(f"{note}{credit}<br>政令指定都市は区をまとめた市全体の値です。"
                          "地図・一覧のクリックで選択できます")
        self.update_summary()
        if self.selected:
            self.select(self.selected, pan=False)
        playing = self.play_btn.isChecked()
        self.set_status(f"{ind.name}　{year}年　{scope}" + ("　▶ 推移を再生中" if playing else ""),
                        "busy" if playing else "")

    def legend_html(self, ind, year, breaks, colors, fixed):
        if not colors:
            return ""
        out = [f"<div class='t'>{html.escape(ind.name)}（{html.escape(ind.unit)}）{year}年"
               + ("<br>色の区切りは全年共通" if fixed and len(self.years) > 1 else "") + "</div>"]
        edges = [None] + breaks + [None]
        # 大きい値を上にする
        for k in range(len(colors) - 1, -1, -1):
            lo, hi = edges[k], edges[k + 1]
            if lo is None:
                label = f"〜 {self.fmt(hi)}"
            elif hi is None:
                label = f"{self.fmt(lo)} 〜"
            else:
                label = f"{self.fmt(lo)} 〜 {self.fmt(hi)}"
            out.append(f"<div class='row'><span class='sw' style='background:{colors[k]}'></span>{label}</div>")
        out.append(f"<div class='row'><span class='sw' style='background:{rd.NO_DATA};opacity:.6'></span>データなし</div>")
        return "".join(out)

    def update_summary(self):
        text = self.summary_base
        c = self.selected
        if c and c in self.regions:
            values = self.series.get(self.year.currentText(), {})
            rank = next((r for r, code, _ in self.rows if code == c), None)
            if c in values and rank:
                text += f"\n▶ {self.name_of(c)}：{self.fmt(values[c])} {self.current().unit}　{rank:,}位"
            elif c in values:
                text += f"\n▶ {self.name_of(c)}：{self.fmt(values[c])}（範囲外）"
            else:
                text += f"\n▶ {self.name_of(c)}：データなし"
        self.summary.setText(text)
        self.update_trend()

    def update_trend(self):
        """推移のグラフ：表示範囲の中央値と、選んだ市区町村の値（年ごと）"""
        codes = self.target_codes()
        xs, med = [], []
        for y in self.years:
            m = rd.median(self.series[y][c] for c in codes if c in self.series[y])
            if m is not None:
                xs.append(int(y))
                med.append(m)
        self.median_curve.setData(xs, med)
        c = self.selected
        sx = [int(y) for y in self.years if c in self.series[y]] if c else []
        sy = [self.series[str(x)][c] for x in sx]
        self.sel_curve.setData(sx, sy)
        year = self.year.currentText()
        self.year_line.setVisible(bool(year))
        if year:
            self.year_line.setValue(int(year))
        step = -(-len(xs) // 8) or 1     # 目盛りは多くても8つ程度（年は整数で表示する）
        self.trend.getAxis("bottom").setTicks([[(x, str(x)) for x in xs[::-1][::step]]])
        self.trend.enableAutoRange()

    # ---------- 選択 ----------
    def select(self, code, from_map=False, pan=True):
        self.selected = code
        self.js(f"select({json.dumps(code)})")
        if pan and not from_map:
            self.js(f"focusCode({json.dumps(code)})")
        # 一覧の行を選ぶ
        t = self.table
        t.blockSignals(True)
        t.clearSelection()
        for row in range(t.rowCount()):
            if t.item(row, 0).data(QtCore.Qt.UserRole + 1) == code:
                t.selectRow(row)
                t.scrollToItem(t.item(row, 0), QtWidgets.QAbstractItemView.PositionAtCenter)
                break
        t.blockSignals(False)
        self.update_summary()

    def on_table_select(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.select(self.table.item(rows[0].row(), 0).data(QtCore.Qt.UserRole + 1))

    def on_pref(self):
        self.redraw()
        codes = self.target_codes()
        if self.pref.currentText() != ALL:
            self.js(f"fitCodes({json.dumps(codes)})")
        else:
            self.js("map.flyTo([37.5, 137.5], 5, {duration: 0.6})")

    def on_search(self, text=None):
        text = (text if isinstance(text, str) else self.search.text()).strip()
        if not text:
            return
        key = text.replace(" ", "").replace("\u3000", "")
        hits = [c for c in sorted(self.regions) if key in self.name_of(c).replace(" ", "")]
        # 「横浜」なら上北郡横浜町より横浜市を先にする（名前が入力で始まるもの → 短い名前の順）
        hits.sort(key=lambda c: (not self.regions[c][1].startswith(key), len(self.regions[c][1])))
        if not hits:
            self.set_status(f"「{text}」に当てはまる市区町村がありません", "idle")
            return
        code = hits[0]
        pref = self.regions[code][0]
        if self.pref.currentText() not in (ALL, pref):
            self.pref.setCurrentText(ALL)
        self.select(code)

    def shutdown(self):
        """アプリの終了時：グラフ分析ウィンドウも閉じる"""
        if self.chart_window:
            self.chart_window.close()
        self.close()

    def open_charts(self):
        """グラフ分析ウィンドウ（表示中の指標・範囲・選んだ市区町村を引き継ぐ）"""
        import region_charts
        if self.chart_window is None:
            self.chart_window = region_charts.RegionChartsWindow()
        self.chart_window.show_with(self.indicator.currentData(), self.pref.currentText(), self.selected)
        self.chart_window.show()
        self.chart_window.raise_()

    def go_home(self):
        """観測地点を含む市区町村を選ぶ（境界の多角形で内外判定する）"""
        self.home = adsb.AdsbWindow._load_home()
        code = find_region(self.topo, self.home[0], self.home[1]) if self.topo else None
        if code:
            self.select(code)
        else:
            self.js(f"map.flyTo([{self.home[0]}, {self.home[1]}], 9, {{duration: 0.6}})")
            self.set_status("観測地点を含む市区町村が見つかりませんでした", "idle")


# ---------------- 観測地点を含む市区町村 ----------------
def _decode_arcs(topo):
    """TopoJSON の arcs（差分・量子化）を経度・緯度の列に戻す"""
    tf = topo.get("transform")
    out = []
    for arc in topo["arcs"]:
        pts, x, y = [], 0, 0
        for p in arc:
            if tf:
                x, y = x + p[0], y + p[1]
                pts.append((x * tf["scale"][0] + tf["translate"][0], y * tf["scale"][1] + tf["translate"][1]))
            else:
                pts.append((p[0], p[1]))
        out.append(pts)
    return out


def _ring(arcs, idx):
    pts = []
    for i in idx:
        seg = arcs[i] if i >= 0 else arcs[~i][::-1]
        pts.extend(seg[1:] if pts else seg)
    return pts


def _inside(ring, lon, lat):
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def find_region(topo, lat, lon):
    arcs = _decode_arcs(topo)
    for obj in topo["objects"].values():
        for g in obj["geometries"]:
            code = (g.get("properties") or {}).get("N03_007")
            if not code:
                continue
            polys = g["arcs"] if g["type"] == "MultiPolygon" else [g["arcs"]] if g["type"] == "Polygon" else []
            for poly in polys:
                outer = _ring(arcs, poly[0])
                if _inside(outer, lon, lat) and not any(_inside(_ring(arcs, h), lon, lat) for h in poly[1:]):
                    return code
    return None


def main():
    # 地図（QtWebEngine）は QApplication より前にこの設定が必要
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    app = QtWidgets.QApplication(sys.argv)
    theme.apply(app)
    win = RegionStatsWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

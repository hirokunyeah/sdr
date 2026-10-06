"""
地域統計のグラフ分析ウィンドウ

  市区町村別の統計（region_data.py）を、地図ではなくグラフで見る。
  - 推移と将来の推計：選んだ全国・都道府県・市区町村（最大6つ）の推移を折れ線で比べ、
    過去の傾向をそのまま延ばした単純な推計と、伸び率を上乗せ・下乗せした試算、公式の将来推計（総人口）を表示する
  - 2つの指標の関係：横軸・縦軸に指標を選び、市区町村を点（大きさは人口）で並べた散布図。
    年を進めて点の動きを再生でき、相関係数と回帰直線も表示する
  地域統計ウィンドウの「📈 グラフで分析」から開く。単独でも起動できる：python desktop/region_charts.py
"""
import math
import sys

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import adsb
import region_data as rd
import region_stats
import theme
from region_stats import ALL, SPEEDS, Loader, NumAxis, indicator_combo

MAX_PICKS = 6
# 系列の色（固定の順で使う。色覚の違いがあっても隣り合う色を見分けやすい順）
SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"]
MEDIAN_KEY = "__median__"
JAPAN = "00"                 # 比べる地域：全国のコード（都道府県は2桁、市区町村は5桁）
PREF_SUFFIX = "（都道府県全体）"
SCOPE_COLOR = "#3987e5"      # 散布図：範囲に入る市区町村
OTHER_COLOR = "#555c6b"      # 散布図：範囲外の市区町村
END_YEARS = [2030, 2035, 2040, 2045, 2050]
BASES = [("直近5年", 5), ("直近10年", 10), ("直近15年", 15), ("全期間", 0)]   # 推計の基にする期間（年数）
REFS = [("表示しない", "none"), ("範囲の市区町村の中央値", "median"), ("範囲の合計", "sum")]
START_YEARS = [("自動", 0), ("1920年", 1920), ("1950年", 1950), ("1980年", 1980), ("2000年", 2000),
               ("2005年", 2005), ("2010年", 2010), ("2015年", 2015)]
AUTO_LONG_START = 1950   # 自動：全国・都道府県だけを比べるときの表示の開始
MERGER_END = 2010        # 平成の大合併が終わった年（2010年3月）。これより前は合併前の区域の値
Y_SCALES = [("実数", "raw"), ("指数（共通の基準年＝100）", "index"), ("対数", "log")]
METHODS = [("直線（毎年同じだけ増減）", "linear"), ("一定の伸び率（毎年同じ割合で増減）", "growth"), ("推計しない", "none")]


def styled_plot(**kw):
    plot = pg.PlotWidget(background=theme.C["plot"], axisItems={"left": NumAxis("left"), "bottom": NumAxis("bottom")}, **kw)
    plot.setFrameShape(QtWidgets.QFrame.NoFrame)
    plot.showGrid(x=True, y=True, alpha=0.15)
    plot.setMenuEnabled(False)
    plot.hideButtons()
    axis_font = theme.font(theme.MONO_FONTS, 8)
    for ax in ("left", "bottom"):
        a = plot.getAxis(ax)
        a.enableAutoSIPrefix(False)
        a.setTickFont(axis_font)
        a.setPen(theme.C["line"])
        a.setTextPen(theme.C["muted"])
    return plot


def color_with_alpha(hex_color, alpha):
    c = QtGui.QColor(hex_color)
    c.setAlpha(alpha)
    return c


def latest_le(series, year):
    """year 以前で最も新しい年の値 {コード: 値}（調査の周期が指標ごとに違うため）と、その年"""
    ys = [y for y in series if y <= year]
    if not ys:
        return {}, None
    y = max(ys)
    return series[y], y


# ---------------- 推計 ----------------
def project(points, method, base, end_year, adjust, additive, clamp_zero):
    """過去の値 [(年, 値)]（古い順）から end_year までの推計 [(年, 値)] を返す（最後の実績の年から始まる）

    method   "linear"：基にする期間の傾き（最小二乗）で毎年同じだけ増減
             "growth"：基にする期間の年平均の伸び率で増減（値が正のときだけ）
    base     基にする最近の年数（0 は全期間）
    adjust   試算の上乗せ。additive なら 1年あたりのポイント、そうでなければ 1年あたりの % で掛ける
    """
    if method == "none" or len(points) < 2:
        return []
    pts = [p for p in points if p[0] >= points[-1][0] - base] if base else points
    if len(pts) < 2:
        return []
    t_last, v_last = pts[-1]
    if method == "growth":
        t0, v0 = pts[0]
        if v0 <= 0 or v_last <= 0:
            return []
        rate = (v_last / v0) ** (1 / (t_last - t0))
        trend = lambda t: v_last * rate ** (t - t_last)
    else:
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        slope = sum((t - mt) * (v - mv) for t, v in pts) / den if den else 0.0
        trend = lambda t: v_last + slope * (t - t_last)
    out = []
    for t in range(t_last, end_year + 1):
        v = trend(t)
        if additive:
            v += adjust * (t - t_last)
        else:
            v *= (1 + adjust / 100) ** (t - t_last)
        if clamp_zero:
            v = max(0.0, v)
        out.append((t, v))
    return out


def linear_fit(xs, ys):
    """最小二乗の回帰直線 (傾き, 切片) と相関係数 r"""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if not sxx or not syy:
        return None
    slope = sxy / sxx
    return slope, my - slope * mx, sxy / math.sqrt(sxx * syy)


class RegionChartsWindow(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle("地域統計のグラフ分析")
        self.resize(1360, 860)
        self.regions = {}
        self.data = {}            # 指標のキー → {年: {コード: 値}}
        self.area = {}            # 指標のキー → {年: {"00"（全国）/ 都道府県コード2桁: 値}}。ない指標は {}
        self.official = {}        # 指標のキー → 公式の将来推計 {年: {コード: 値}}。ない指標は {}
        self.pref_code = {}       # 都道府県名 → コード2桁
        self.pref_name = {}       # コード2桁 → 都道府県名
        self.picks = []           # 比べる市区町村のコード（順番が色の順番）
        self.pending = None       # 境界の読み込み後に反映する (指標, 範囲, 市区町村)
        self.topo = None
        self._auto_log_pending = False
        self.loader = Loader(self)
        self.loader.done.connect(self.on_loaded)

        # --- 右側：範囲と比べる市区町村（両方のタブで共通） ---
        self.pref = QtWidgets.QComboBox()
        self.pref.addItem(ALL)
        self.pref.setToolTip("中央値・相関係数を計算する範囲。散布図ではこの範囲の点を青で表示します")
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("全国・都道府県・市区町村の名前で追加")
        self.search.setClearButtonEnabled(True)
        self.pick_list = QtWidgets.QListWidget()
        self.pick_list.setIconSize(QtCore.QSize(14, 14))
        self.add_japan_btn = QtWidgets.QPushButton("＋ 全国")
        self.add_japan_btn.setToolTip("日本全体の値を追加します")
        self.add_pref_btn = QtWidgets.QPushButton("＋ 都道府県")
        self.add_pref_btn.setToolTip("「範囲」で選んだ都道府県全体の値を追加します")
        self.remove_btn = QtWidgets.QPushButton("選択を外す")
        self.clear_btn = QtWidgets.QPushButton("すべて外す")
        self.state = QtWidgets.QLabel("")
        self.state.setObjectName("StatusCard")

        side = QtWidgets.QGroupBox("比べる地域")
        sl = QtWidgets.QVBoxLayout(side)
        sl.addWidget(theme.caption("範囲"))
        sl.addWidget(self.pref)
        sl.addSpacing(6)
        sl.addWidget(theme.caption(f"比べる地域（最大 {MAX_PICKS} つ）"))
        sl.addWidget(self.search)
        add_row = QtWidgets.QHBoxLayout()
        add_row.addWidget(self.add_japan_btn)
        add_row.addWidget(self.add_pref_btn)
        sl.addLayout(add_row)
        sl.addWidget(self.pick_list, 1)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.remove_btn)
        row.addWidget(self.clear_btn)
        sl.addLayout(row)
        hint = theme.caption("散布図の点をクリックしても追加できます。色は追加した順に決まります。"
                             "散布図には市区町村だけを表示します", "hint")
        hint.setWordWrap(True)
        sl.addWidget(hint)
        side.setMinimumWidth(260)
        side.setMaximumWidth(320)

        # --- タブ ---
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_trend_tab(), "推移と将来の推計")
        self.tabs.addTab(self._build_scatter_tab(), "2つの指標の関係（散布図）")

        body = QtWidgets.QHBoxLayout()
        body.addWidget(self.tabs, 1)
        body.addWidget(side)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.addWidget(self.state)
        lay.addLayout(body, 1)

        self.pref.currentIndexChanged.connect(self.redraw_all)
        self.search.returnPressed.connect(self.on_search)
        self.remove_btn.clicked.connect(self.remove_pick)
        self.clear_btn.clicked.connect(lambda: self.set_picks([]))
        self.add_japan_btn.clicked.connect(lambda: self.add_pick(JAPAN))
        self.add_pref_btn.clicked.connect(lambda: self.add_pick(self.pref_code.get(self.pref.currentText())))
        self.pref.currentIndexChanged.connect(
            lambda: self.add_pref_btn.setEnabled(self.pref.currentText() != ALL))
        self.tabs.currentChanged.connect(self.redraw_all)

        self.set_status("市区町村の一覧を読み込み中…", "busy")
        self.loader.request("topo", rd.boundaries)

    # ================= 推移と将来の推計 =================
    def _build_trend_tab(self):
        self.t_ind = indicator_combo("pop")
        self.t_method = QtWidgets.QComboBox()
        for label, key in METHODS:
            self.t_method.addItem(label, key)
        self.t_base = QtWidgets.QComboBox()
        for label, n in BASES:
            self.t_base.addItem(label, n)
        self.t_base.setCurrentIndex(1)
        self.t_base.setToolTip("推計の基にする最近の期間。この期間の実績の値から傾き・伸び率を求めます")
        self.t_end = QtWidgets.QComboBox()
        for y in END_YEARS:
            self.t_end.addItem(f"{y}年", y)
        self.t_end.setCurrentIndex(END_YEARS.index(2040))
        self.t_start = QtWidgets.QComboBox()
        for label, y in START_YEARS:
            self.t_start.addItem(label, y)
        self.t_start.setToolTip(f"自動：全国・都道府県だけなら{AUTO_LONG_START}年から、市区町村を含むときは{MERGER_END}年から。\n"
                                f"{MERGER_END}年より前の市区町村の値は市町村合併（平成の大合併）の前の区域のもので、"
                                "合併した市区町村は、その前後で値が飛びます")
        self.t_official = QtWidgets.QCheckBox("公式の将来推計も表示")
        self.t_official.setChecked(True)
        self.t_official.setToolTip("国立社会保障・人口問題研究所の将来推計人口（総人口だけ。2025〜2050年）")
        self.t_adjust = QtWidgets.QDoubleSpinBox()
        self.t_adjust.setRange(-10, 10)
        self.t_adjust.setSingleStep(0.1)
        self.t_adjust.setDecimals(1)
        self.t_adjust.setSuffix(" %/年")
        self.t_adjust.setToolTip("推計に上乗せ（マイナスなら下乗せ）して「この先こうなったら」を試算します")
        self.t_reset = QtWidgets.QPushButton("0 に戻す")
        self.t_ref = QtWidgets.QComboBox()
        for label, key in REFS:
            self.t_ref.addItem(label, key)
        self.t_ref.setToolTip("灰色の破線で比べる基準。中央値は範囲の市区町村を値の順に並べた真ん中の値、"
                              "合計は範囲の市区町村の値を足したもの（全国なら日本全体。人数・台数などの指標だけ）")
        self.t_scale = QtWidgets.QComboBox()
        for label, key in Y_SCALES:
            self.t_scale.addItem(label, key)
        self.t_scale.setToolTip("規模の違う市区町村の増え方・減り方を比べるときは「指数」か「対数」が見やすい")

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(theme.caption("指標"))
        bar.addWidget(self.t_ind)
        bar.addSpacing(8)
        bar.addWidget(theme.caption("推計の方法"))
        bar.addWidget(self.t_method)
        bar.addWidget(theme.caption("基にする期間"))
        bar.addWidget(self.t_base)
        bar.addStretch()
        bar2 = QtWidgets.QHBoxLayout()
        bar2.addWidget(theme.caption("表示の開始"))
        bar2.addWidget(self.t_start)
        bar2.addSpacing(8)
        bar2.addWidget(theme.caption("推計の終わり"))
        bar2.addWidget(self.t_end)
        bar2.addSpacing(8)
        bar2.addWidget(theme.caption("試算：伸びの上乗せ"))
        bar2.addWidget(self.t_adjust)
        bar2.addWidget(self.t_reset)
        bar2.addSpacing(8)
        bar2.addWidget(self.t_official)
        bar2.addSpacing(8)
        bar2.addWidget(theme.caption("比べる基準"))
        bar2.addWidget(self.t_ref)
        bar2.addSpacing(8)
        bar2.addWidget(theme.caption("縦軸"))
        bar2.addWidget(self.t_scale)
        bar2.addStretch()

        self.t_plot = styled_plot()
        self.t_plot.setMouseEnabled(x=False, y=False)
        self.t_legend = theme.caption("")
        self.t_legend.setTextFormat(QtCore.Qt.RichText)
        self.t_legend.setWordWrap(True)
        self.t_items = []         # 描き直すたびに消すグラフの部品
        self.t_ylabel = QtWidgets.QLabel("")
        self.t_ylabel.setStyleSheet(f"color: {theme.C['muted']};")
        shade = color_with_alpha(theme.C["accent"], 18)
        self.t_region = pg.LinearRegionItem(movable=False, brush=shade, pen=pg.mkPen(None))
        self.t_region.setZValue(-20)
        self.t_plot.addItem(self.t_region)

        self.t_table = QtWidgets.QTableWidget(0, 5)
        self.t_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.t_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.t_table.setShowGrid(False)
        self.t_table.setAlternatingRowColors(True)
        self.t_table.verticalHeader().hide()
        self.t_table.verticalHeader().setDefaultSectionSize(24)
        self.t_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.t_table.setMaximumHeight(200)
        self.t_note = theme.caption("", "hint")
        self.t_note.setWordWrap(True)

        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.addLayout(bar)
        lay.addLayout(bar2)
        lay.addWidget(self.t_legend)
        lay.addWidget(self.t_ylabel)
        lay.addWidget(self.t_plot, 1)
        lay.addWidget(self.t_table)
        lay.addWidget(self.t_note)

        self.t_ind.currentIndexChanged.connect(lambda: self.need_trend(self.t_ind.currentData()))
        for wdg in (self.t_method, self.t_base, self.t_end, self.t_scale, self.t_ref, self.t_start):
            wdg.currentIndexChanged.connect(self.redraw_trend)
        self.t_adjust.valueChanged.connect(self.redraw_trend)
        self.t_official.toggled.connect(self.redraw_trend)
        self.t_reset.clicked.connect(lambda: self.t_adjust.setValue(0))
        return w

    def redraw_trend(self):
        key = self.t_ind.currentData()
        series = self.data.get(key)
        if not series or not self.regions:
            return
        ind = rd.BY_KEY[key]
        area = self.area.get(key) or {}
        official = self.official.get(key) or {}
        additive = ind.unit == "%"
        self.t_adjust.setSuffix(" ポイント/年" if additive else " %/年")
        method, base = self.t_method.currentData(), self.t_base.currentData()
        end_year, adjust = self.t_end.currentData(), self.t_adjust.value()
        scope = self.pref.currentText()
        codes = self.scope_codes()
        has_muni = any(len(c) == 5 for c in self.picks)
        start = self.t_start.currentData()
        if not start:   # 自動：全国・都道府県だけなら長い期間、市区町村を含むなら合併後から
            start = MERGER_END if has_muni or not self.picks else AUTO_LONG_START
        years = [y for y in sorted(series) if int(y) >= start] or sorted(series)[-1:]
        show_official = self.t_official.isChecked() and bool(official)
        self.t_official.setEnabled(bool(ind.official))

        # 比べる基準：合計は足し合わせられる指標だけ（割合・平均を足しても意味がない）
        sum_item = self.t_ref.model().item(self.t_ref.findData("sum"))
        sum_item.setEnabled(ind.summable)
        ref = self.t_ref.currentData()
        if ref == "sum" and not ind.summable:
            self.t_ref.blockSignals(True)
            self.t_ref.setCurrentIndex(self.t_ref.findData("median"))
            self.t_ref.blockSignals(False)
            ref = "median"

        # 系列：比べる基準（灰色の破線） ＋ 選んだ全国・都道府県・市区町村
        lines = []
        notes = []
        if ref == "median":
            # 年によって対象の市区町村が入れ替わると中央値が見かけ上動くので、表示する全部の年で値がある市区町村だけで計算する
            fixed = [c for c in codes if all(c in series[y] for y in years)]
            ref_pts = [(int(y), rd.median(series[y][c] for c in fixed)) for y in years] if fixed else []
            lines.append((MEDIAN_KEY, f"{scope}の市区町村の中央値", theme.C["muted"], ref_pts))
            notes.append(f"中央値は、表示する全部の年で値がある {len(fixed):,} 市区町村の真ん中の値です。")
        elif ref == "sum":
            pc = "00" if scope == ALL else self.pref_code.get(scope)
            ref_pts = [(int(y), area[y][pc]) for y in sorted(area) if int(y) >= start and pc in area[y]]
            lines.append((MEDIAN_KEY, f"{scope}の合計", theme.C["muted"], ref_pts))
        for i, c in enumerate(self.picks):
            src = area if len(c) == 2 else series
            pts = [(int(y), src[y][c]) for y in sorted(src) if int(y) >= start and c in src[y]]
            lines.append((c, self.short_name(c), SERIES_COLORS[i], pts))
        if any(len(c) == 2 for c in self.picks) and not area:
            notes.append("この指標には全国・都道府県の値がありません。" if key in self.area else "全国・都道府県の値を取得中です。")

        for item in self.t_items:
            self.t_plot.removeItem(item)
        self.t_items = []
        # 縦軸の表し方。指数・対数は値が正のときだけ（増減率など負になる指標では実数に戻す）
        scale = self.t_scale.currentData()
        if scale != "raw" and ind.diverging:
            scale = "raw"
        axis = self.t_plot.getAxis("left")
        axis.logMode = scale == "log"
        axis.picture = None
        axis.update()
        # 指数は全部の線に共通の基準の年（どの線にも値がある最初の年）を 100 にする。
        # 線ごとに最初の年が違う（全国は1920年、市区町村は2000年など）と比べられないため
        base_year = None
        if scale == "index":
            firsts = [pts[0][0] for _, _, _, pts in lines if pts]
            base_year = max(firsts) if firsts else None
            lines = [(c, n, col, [p for p in pts if p[0] >= base_year]) for c, n, col, pts in lines]
        unit = {"raw": ind.unit, "index": f"指数：{base_year}年＝100", "log": f"{ind.unit}・対数"}[scale]
        self.t_ylabel.setText(f"↑ 縦軸：{ind.name}（{unit}）")

        def shown(points, base_value):
            """表示用の値に変換（指数は base_value を 100 にする）。変換できない点は除く"""
            if scale == "index":
                return [(t, v / base_value * 100) for t, v in points] if base_value and base_value > 0 else []
            if scale == "log":
                return [(t, math.log10(v)) for t, v in points if v > 0]
            return points

        legend, rows, ends = [], [], []
        first_year, last_year, drew_official = None, None, False
        for code, name, color, raw_pts in lines:
            if not raw_pts:
                continue
            pts = shown(raw_pts, raw_pts[0][1])
            if not pts:
                continue
            first_year = min(first_year or pts[0][0], pts[0][0])
            last_year = max(last_year or pts[-1][0], pts[-1][0])
            is_med = code == MEDIAN_KEY
            style = QtCore.Qt.DashLine if is_med else QtCore.Qt.SolidLine
            many = len(pts) > 30     # 毎年の値が長く続くときは点を打たない
            actual = self.t_plot.plot([t for t, _ in pts], [v for _, v in pts],
                                      pen=pg.mkPen(color, width=2, style=style),
                                      symbol=None if is_med or many else "o", symbolSize=7, symbolBrush=color,
                                      symbolPen=pg.mkPen(theme.C["plot"], width=2))
            self.t_items.append(actual)
            proj = project(raw_pts, method, base, end_year, adjust, additive, not ind.diverging)
            proj_shown = shown(proj, raw_pts[0][1])
            if proj_shown:
                item = self.t_plot.plot([t for t, _ in proj_shown], [v for _, v in proj_shown],
                                        pen=pg.mkPen(color, width=2, style=QtCore.Qt.DotLine))
                self.t_items.append(item)
            # 公式の将来推計（社人研）
            off_end = None
            if show_official and not is_med:
                off = [(int(y), official[y][code]) for y in sorted(official) if code in official[y]]
                off_shown = shown(off, raw_pts[0][1])
                if off_shown:
                    item = self.t_plot.plot([t for t, _ in off_shown], [v for _, v in off_shown],
                                            pen=pg.mkPen(color, width=1.5, style=QtCore.Qt.DashDotLine),
                                            symbol="d", symbolSize=6, symbolBrush=color, symbolPen=None)
                    self.t_items.append(item)
                    drew_official = True
                    off_end = next((v for t, v in off if t == end_year), None)
            # 線の端に名前（色だけに頼らない）。重ならないよう後でまとめて位置を決める
            ex, ey = (proj_shown or pts)[-1]
            label = pg.TextItem(name, color=theme.C["muted"] if is_med else color, anchor=(0, 0.5))
            label.setFont(theme.font(theme.UI_FONTS, 8))
            self.t_plot.addItem(label)
            self.t_items.append(label)
            ends.append([ey, ex, label])
            mark = "╍╍" if is_med else "━●"
            legend.append(f"<span style='color:{color}'>{mark}</span> {name}")
            t_last, v_last = raw_pts[-1]
            v_end = proj[-1][1] if proj else None
            rows.append((name, color, t_last, v_last, v_end, off_end))

        # 名前のラベルを上下にずらして重ならないようにする（縦軸の幅の 5% 以上あける）
        all_y = [it.yData for it in self.t_items if isinstance(it, pg.PlotDataItem) and it.yData is not None]
        all_y = [v for arr in all_y for v in arr]
        if ends and all_y:
            gap = (max(all_y) - min(all_y) or 1) * 0.05
            ends.sort(key=lambda e: e[0])
            for k in range(1, len(ends)):
                ends[k][0] = max(ends[k][0], ends[k - 1][0] + gap)
            for ey, ex, label in ends:
                label.setPos(ex, ey)

        # 推計の範囲を薄く塗る
        has_proj = method != "none" or drew_official
        first_year = first_year or int(years[0])
        last_year = last_year or int(years[-1])
        self.t_region.setVisible(has_proj)
        if has_proj:
            self.t_region.setRegion((last_year, end_year))
        right = end_year if has_proj else last_year
        span = right - first_year
        self.t_plot.setXRange(first_year, right + max(2, span * 0.12), padding=0.02)
        self.t_plot.enableAutoRange(axis="y")
        step = 10 if span > 60 else 5 if span > 20 else 2
        self.t_plot.getAxis("bottom").setTicks(
            [[(y, str(y)) for y in range(first_year // step * step, right + 1, step)]])
        if method != "none":
            legend.append(f"<span style='color:{theme.C['muted']}'>┈┈</span> 推計（傾向を延長）")
        if drew_official:
            legend.append(f"<span style='color:{theme.C['muted']}'>－・◆</span> 公式の将来推計")
        self.t_legend.setText("　".join(legend))

        # 表
        heads = ["地域", "最新の実績", "年", f"推計 {end_year}年" if method != "none" else "推計", "増減"]
        if drew_official:
            heads.append(f"公式推計 {end_year}年")
        t = self.t_table
        t.setColumnCount(len(heads))
        t.setHorizontalHeaderLabels(heads)
        t.setRowCount(len(rows))
        mono = theme.font(theme.MONO_FONTS, 9)
        for r, (name, color, t_last, v_last, v_end, off_end) in enumerate(rows):
            if v_end is None:
                change = "―"
            elif additive:
                change = f"{v_end - v_last:+,.{max(1, ind.digits)}f} ポイント"
            elif v_last:
                change = f"{(v_end / v_last - 1) * 100:+.1f}%"
            else:
                change = "―"
            cells = [name, self.fmt(v_last, ind), str(t_last), "―" if v_end is None else self.fmt(v_end, ind), change]
            if drew_official:
                cells.append("―" if off_end is None else self.fmt(off_end, ind))
            for k, text in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(text)
                if k == 0:
                    pix = QtGui.QPixmap(12, 12)
                    pix.fill(QtGui.QColor(color))
                    it.setIcon(QtGui.QIcon(pix))
                else:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                    it.setFont(mono)
                t.setItem(r, k, it)
        for k in range(1, len(heads)):
            t.resizeColumnToContents(k)

        how = {"linear": f"{self.t_base.currentText()}の値の傾きのまま毎年同じだけ増減",
               "growth": f"{self.t_base.currentText()}の年平均の伸び率のまま増減",
               "none": ""}[method]
        if how:
            notes.append(f"推計（点線）：{how}" + (f"、さらに毎年 {adjust:+.1f}{' ポイント' if additive else '%'} を上乗せ" if adjust else "")
                         + "。過去の傾向をそのまま延ばしただけの単純な計算で、出生・死亡・転入出などは考えていません。")
        if drew_official:
            notes.append("公式の将来推計（一点鎖線）：国立社会保障・人口問題研究所の将来推計人口"
                         "（全国は「日本の将来推計人口」、都道府県・市区町村は「日本の地域別将来推計人口」。統計ダッシュボードから取得）。")
        if any(len(c) == 2 for c in self.picks) and area:
            notes.append("全国・都道府県は、統計ダッシュボードの全国・都道府県の値です（国勢調査の間の年は推計人口など）。")
        if has_muni and start < MERGER_END and key not in ("income_per", "income_total", "taxpayers"):
            notes.append(f"{MERGER_END}年より前の市区町村の値は市町村合併の前の区域のもので、合併した市区町村は値が飛びます。")
        if ind.note:
            notes.append(f"指標：{ind.note}")
        self.t_note.setText("".join(notes))
        self.set_status(f"推移と将来の推計：{ind.name}　範囲 {scope}　比べる地域 {len(self.picks)}", "")

    # ================= 2つの指標の関係（散布図） =================
    def _build_scatter_tab(self):
        self.s_x = indicator_combo("density")
        self.s_y = indicator_combo("kei_hh")
        self.s_logx = QtWidgets.QCheckBox("横軸を対数に")
        self.s_logy = QtWidgets.QCheckBox("縦軸を対数に")
        self.s_size = QtWidgets.QCheckBox("点の大きさ＝人口")
        self.s_size.setChecked(True)
        self.s_trail = QtWidgets.QCheckBox("選んだ市区町村の軌跡")
        self.s_trail.setChecked(True)
        self.s_play = QtWidgets.QPushButton("▶ 年を進めて再生")
        self.s_play.setProperty("kind", "primary")
        self.s_play.setCheckable(True)
        self.s_play.setMinimumWidth(150)
        self.s_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.s_slider.setPageStep(1)
        self.s_slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.s_year_lbl = QtWidgets.QLabel("")
        self.s_year_lbl.setFont(theme.font(theme.MONO_FONTS, 12, bold=True))
        self.s_year_lbl.setMinimumWidth(80)
        self.s_speed = QtWidgets.QComboBox()
        for label, ms in SPEEDS:
            self.s_speed.addItem(label, ms)
        self.s_speed.setCurrentIndex(1)
        self.s_timer = QtCore.QTimer(self)
        self.s_years = []

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(theme.caption("横軸"))
        bar.addWidget(self.s_x)
        bar.addWidget(self.s_logx)
        bar.addSpacing(12)
        bar.addWidget(theme.caption("縦軸"))
        bar.addWidget(self.s_y)
        bar.addWidget(self.s_logy)
        bar.addStretch()
        bar2 = QtWidgets.QHBoxLayout()
        bar2.addWidget(self.s_play)
        bar2.addWidget(self.s_slider, 1)
        bar2.addWidget(self.s_year_lbl)
        bar2.addWidget(theme.caption("速さ"))
        bar2.addWidget(self.s_speed)
        bar2.addSpacing(8)
        bar2.addWidget(self.s_size)
        bar2.addWidget(self.s_trail)

        self.s_plot = styled_plot()
        self.s_scatter = pg.ScatterPlotItem(pxMode=True, hoverable=True, tip=self.scatter_tip,
                                            hoverPen=pg.mkPen(theme.C["text"], width=1.5))
        self.s_scatter.sigClicked.connect(lambda _item, pts, _ev: pts and self.add_pick(pts[0].data()))
        self.s_plot.addItem(self.s_scatter)
        self.s_fit = self.s_plot.plot([], [], pen=pg.mkPen(theme.C["text"], width=1.5, style=QtCore.Qt.DashLine))
        self.s_items = []         # 選んだ市区町村の点・ラベル・軌跡
        # 縦軸の名前はグラフの左上に横書きで出す（軸に沿って回転させると日本語が横倒しになり、長いと収まらない）
        self.s_ylabel = QtWidgets.QLabel("")
        self.s_ylabel.setStyleSheet(f"color: {theme.C['muted']};")
        self.s_info = theme.caption("")
        self.s_info.setTextFormat(QtCore.Qt.RichText)
        self.s_info.setWordWrap(True)
        self.s_note = theme.caption("", "hint")
        self.s_note.setWordWrap(True)

        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.addLayout(bar)
        lay.addLayout(bar2)
        lay.addWidget(self.s_info)
        lay.addWidget(self.s_ylabel)
        lay.addWidget(self.s_plot, 1)
        lay.addWidget(self.s_note)

        self.s_x.currentIndexChanged.connect(lambda: self.need(self.s_x.currentData(), auto_log=True))
        self.s_y.currentIndexChanged.connect(lambda: self.need(self.s_y.currentData(), auto_log=True))
        for chk in (self.s_logx, self.s_logy, self.s_size, self.s_trail):
            chk.toggled.connect(lambda: self.redraw_scatter(reset_range=True))
        self.s_slider.valueChanged.connect(lambda: self.redraw_scatter())
        self.s_play.toggled.connect(self.on_scatter_play)
        self.s_timer.timeout.connect(self.step_scatter)
        self.s_speed.currentIndexChanged.connect(lambda: self.s_timer.setInterval(self.s_speed.currentData()))
        return w

    def scatter_years(self):
        """両方の指標の値がそろう年（どちらかに値がある年で、もう一方にもそれ以前の値がある年）"""
        xs, ys = self.data.get(self.s_x.currentData()), self.data.get(self.s_y.currentData())
        if not xs or not ys:
            return []
        first = max(min(xs), min(ys))
        return sorted(y for y in set(xs) | set(ys) if y >= first)

    def scatter_points(self, year):
        """年 → {コード: (x, y, 人口)}（対数の軸は log10 に変換済み。変換できない値は除く）"""
        xv, xy = latest_le(self.data[self.s_x.currentData()], year)
        yv, yy = latest_le(self.data[self.s_y.currentData()], year)
        pop, _ = latest_le(self.data.get("pop", {}), year)
        logx, logy = self.s_logx.isChecked(), self.s_logy.isChecked()
        out = {}
        for c in self.regions:
            if c not in xv or c not in yv:
                continue
            x, y = xv[c], yv[c]
            if (logx and x <= 0) or (logy and y <= 0):
                continue
            out[c] = (math.log10(x) if logx else x, math.log10(y) if logy else y, pop.get(c))
        return out, xy, yy

    def redraw_scatter(self, reset_range=False):
        kx, ky = self.s_x.currentData(), self.s_y.currentData()
        if kx not in self.data or ky not in self.data or "pop" not in self.data or not self.regions:
            return
        years = self.scatter_years()
        if years != self.s_years:
            self.s_years = years
            self.s_slider.blockSignals(True)
            self.s_slider.setRange(0, max(0, len(years) - 1))
            self.s_slider.setValue(len(years) - 1)
            self.s_slider.blockSignals(False)
            reset_range = True
        enabled = len(years) > 1
        self.s_slider.setEnabled(enabled)
        self.s_play.setEnabled(enabled)
        if not years:
            self.s_scatter.clear()
            self.s_info.setText("2つの指標の年が重なりません")
            return
        year = years[self.s_slider.value()]
        self.s_year_lbl.setText(f"{year}年")
        ix, iy = rd.BY_KEY[kx], rd.BY_KEY[ky]
        for ax, flag in (("bottom", self.s_logx.isChecked()), ("left", self.s_logy.isChecked())):
            a = self.s_plot.getAxis(ax)
            if a.logMode != flag:
                a.logMode = flag
                a.picture = None
                a.update()
        log_note = lambda on: "・対数" if on else ""
        self.s_plot.setLabel("bottom", f"横軸：{ix.name}（{ix.unit}{log_note(self.s_logx.isChecked())}） →")
        self.s_ylabel.setText(f"↑ 縦軸：{iy.name}（{iy.unit}{log_note(self.s_logy.isChecked())}）")

        pts, xyear, yyear = self.scatter_points(year)
        scope = set(self.scope_codes())
        whole = self.pref.currentText() == ALL
        max_pop = max((p for _, _, p in pts.values() if p), default=1)
        size_on = self.s_size.isChecked()

        def size(p):
            return 4 + 26 * math.sqrt(p / max_pop) if size_on and p else 6

        spots = []
        # 範囲外 → 範囲内 の順に描く（範囲内が上に来る）
        for c, (x, y, p) in sorted(pts.items(), key=lambda kv: kv[0] in scope):
            inside = c in scope
            color = SCOPE_COLOR if inside else OTHER_COLOR
            alpha = (110 if whole else 190) if inside else 70
            spots.append({"pos": (x, y), "size": size(p), "data": c,
                          "brush": color_with_alpha(color, alpha), "pen": pg.mkPen(theme.C["plot"], width=0.6)})
        self.s_scatter.setData(spots)

        # 選んだ市区町村：色・名前ラベル・軌跡
        for item in self.s_items:
            self.s_plot.removeItem(item)
        self.s_items = []
        idx = self.s_slider.value()
        per_year = ({y: self.scatter_points(y)[0] for y in years[:idx + 1]}
                    if self.s_trail.isChecked() and self.picks else {})
        for i, c in enumerate(self.picks):
            color = SERIES_COLORS[i]
            if self.s_trail.isChecked():
                path = [per_year[y].get(c) for y in years[:idx + 1]]
                path = [(q[0], q[1]) for q in path if q]
                if len(path) > 1:
                    trail = self.s_plot.plot([q[0] for q in path], [q[1] for q in path],
                                             pen=pg.mkPen(color, width=1.5))
                    self.s_items.append(trail)
            if c not in pts:
                continue
            x, y, p = pts[c]
            dot = pg.ScatterPlotItem([x], [y], size=size(p) + 4, brush=color,
                                     pen=pg.mkPen(theme.C["plot"], width=2))
            label = pg.TextItem(self.short_name(c), color=color, anchor=(-0.15, 1.1))
            label.setFont(theme.font(theme.UI_FONTS, 9, bold=True))
            label.setPos(x, y)
            for item in (dot, label):
                self.s_plot.addItem(item)
                self.s_items.append(item)

        # 回帰直線と相関係数（範囲内の点で計算。対数の軸は変換後の値で計算する）
        # 極端な値（上下0.5%）は直線を大きく傾けるので除いて計算する
        sx = [v[0] for c, v in pts.items() if c in scope]
        sy = [v[1] for c, v in pts.items() if c in scope]
        if len(sx) >= 20:
            (xl, xh), (yl, yh) = self.robust_range(sx), self.robust_range(sy)
            kept = [(x, y) for x, y in zip(sx, sy) if xl <= x <= xh and yl <= y <= yh]
            sx, sy = [x for x, _ in kept], [y for _, y in kept]
        fit = linear_fit(sx, sy)
        if fit:
            slope, icpt, r = fit
            x0, x1 = min(sx), max(sx)
            self.s_fit.setData([x0, x1], [icpt + slope * x0, icpt + slope * x1])
            strength = "強い" if abs(r) >= 0.7 else "中程度の" if abs(r) >= 0.4 else "弱い" if abs(r) >= 0.2 else "ほとんどない"
            sign = "" if abs(r) < 0.2 else ("正の" if r > 0 else "負の")
            rel = f"相関係数 r = {r:+.2f}（{strength}{sign}相関）" if sign else f"相関係数 r = {r:+.2f}（相関はほとんどない）"
        else:
            self.s_fit.setData([], [])
            rel = "相関係数：計算できません"
        when = f"横軸 {xyear}年・縦軸 {yyear}年の値" if xyear != yyear else f"{xyear}年の値"
        self.s_info.setText(f"{self.pref.currentText()} {len(sx):,} 市区町村　{rel}　"
                            f"<span style='color:{theme.C['dim']}'>（{when}）</span>")

        # 年を進めても軸が動かないように、全部の年の値から範囲を決める
        if reset_range:
            allx, ally = [], []
            for y in years:
                for x_, y_, _ in self.scatter_points(y)[0].values():
                    allx.append(x_)
                    ally.append(y_)
            if allx:
                self.s_plot.setXRange(*self.robust_range(allx), padding=0.04)
                self.s_plot.setYRange(*self.robust_range(ally), padding=0.06)

        notes = ["相関は「一緒に増減する傾向」を表すだけで、原因と結果の関係を示すものではありません。",
                 "破線は回帰直線（最小二乗。範囲内の極端な値（上下0.5%）を除いて計算）。点にマウスを重ねると値、クリックで比べる市区町村に追加します。"]
        if int(year) < MERGER_END:
            notes.append(f"{MERGER_END}年より前は市町村合併の前の区域の値のため、今の市区町村と同じ区域の分だけ表示しています。")
        if not (self.s_logx.isChecked() and self.s_logy.isChecked()):
            notes.append("極端な値（上下0.5%）は表示範囲の外になることがあります（マウスのホイール・ドラッグで動かせます）。")
        self.s_note.setText("".join(notes))
        playing = self.s_play.isChecked()
        self.set_status(f"2つの指標の関係：{ix.name} × {iy.name}　{year}年" + ("　▶ 再生中" if playing else ""),
                        "busy" if playing else "")

    @staticmethod
    def robust_range(values):
        """表示範囲：極端な値に引きずられないよう、0.5% 点〜99.5% 点"""
        vs = sorted(values)
        lo = vs[int(len(vs) * 0.005)]
        hi = vs[min(len(vs) - 1, int(len(vs) * 0.995))]
        if hi <= lo:
            lo, hi = vs[0], vs[-1] + 1e-9
        return lo, hi

    def scatter_tip(self, x, y, data):
        c = data
        kx, ky = self.s_x.currentData(), self.s_y.currentData()
        year = self.s_years[self.s_slider.value()] if self.s_years else None
        if not c or not year:
            return ""
        xv, _ = latest_le(self.data[kx], year)
        yv, _ = latest_le(self.data[ky], year)
        pop, _ = latest_le(self.data.get("pop", {}), year)
        ix, iy = rd.BY_KEY[kx], rd.BY_KEY[ky]
        lines = [self.full_name(c),
                 f"{ix.name}：{self.fmt(xv.get(c, 0), ix)} {ix.unit}",
                 f"{iy.name}：{self.fmt(yv.get(c, 0), iy)} {iy.unit}"]
        if c in pop:
            lines.append(f"人口：{pop[c]:,.0f} 人")
        return "\n".join(lines)

    def on_scatter_play(self, on):
        if on:
            if self.s_slider.value() >= self.s_slider.maximum():
                self.s_slider.setValue(0)
            self.s_play.setText("⏸ 一時停止")
            self.s_timer.start(self.s_speed.currentData())
        else:
            self.s_timer.stop()
            self.s_play.setText("▶ 年を進めて再生")
            self.redraw_scatter()

    def step_scatter(self):
        i = self.s_slider.value() + 1
        if i > self.s_slider.maximum():
            self.s_play.setChecked(False)
            return
        self.s_slider.setValue(i)
        if i >= self.s_slider.maximum():
            self.s_play.setChecked(False)

    def auto_log(self):
        """値の幅が3桁以上ある指標（人口・密度など）は対数の軸を初期値にする"""
        for combo, chk in ((self.s_x, self.s_logx), (self.s_y, self.s_logy)):
            series = self.data.get(combo.currentData())
            if not series:
                continue
            vs = [v for v in series[max(series)].values() if v > 0]
            chk.blockSignals(True)
            chk.setChecked(bool(vs) and max(vs) / min(vs) >= 1000)
            chk.blockSignals(False)

    # ================= 共通 =================
    def set_status(self, text, tone=""):
        self.state.setText(text)
        if self.state.property("tone") != tone:
            theme.set_prop(self.state, "tone", tone)

    @staticmethod
    def fmt(v, ind):
        return f"{v:,.{ind.digits}f}"

    def full_name(self, c):
        if c == JAPAN:
            return "全国"
        if len(c) == 2:
            return self.pref_name.get(c, c)
        pref, name = self.regions.get(c, ("", c))
        return f"{pref} {name}"

    def short_name(self, c):
        """グラフのラベル用（郡名・支庁名は省く。同じ名前があるので都道府県は付ける）"""
        if len(c) == 2:
            return self.full_name(c)
        pref, name = self.regions.get(c, ("", c))
        for sep in ("郡", "支庁"):
            if sep in name[:-1]:
                name = name.split(sep, 1)[1]
        if pref != "北海道" and pref[-1:] in ("都", "府", "県"):
            pref = pref[:-1]
        return f"{name}（{pref}）"

    def scope_codes(self):
        pref = self.pref.currentText()
        return [c for c, (p, _) in self.regions.items() if pref == ALL or p == pref]

    def need(self, key, auto_log=False):
        """指標のデータがなければ取得し、そろったら描き直す"""
        if key is None:
            return
        self.s_play.setChecked(False)
        if key in self.data:
            if auto_log:
                self.auto_log()
            self.redraw_all()
            return
        self.set_status(f"「{rd.BY_KEY[key].name}」を取得中…", "busy")
        self.loader.request(f"data:{key}", lambda: rd.load(key))
        self._auto_log_pending = auto_log

    def need_trend(self, key):
        """推移のタブで使う全国・都道府県の値と公式の将来推計も取得する"""
        if key is None:
            return
        if key not in self.area:
            self.loader.request(f"area:{key}", lambda: rd.load_area(key))
        if key not in self.official:
            self.loader.request(f"official:{key}", lambda: rd.load_official(key))
        self.need(key)

    def on_loaded(self, name, result, err):
        if name == "topo":
            if err:
                self.set_status(f"市区町村の一覧を取得できませんでした：{err}", "rec")
                return
            self.topo = result
            self.regions = rd.regions(result)
            prefs = sorted({p for p, _ in self.regions.values()},
                           key=lambda p: min(c for c, (q, _) in self.regions.items() if q == p))
            self.pref.blockSignals(True)
            self.pref.addItems(prefs)
            self.pref.blockSignals(False)
            self.add_pref_btn.setEnabled(False)
            for c, (p, _) in sorted(self.regions.items()):
                self.pref_code.setdefault(p, c[:2])
                self.pref_name.setdefault(c[:2], p)
            names = ["全国"] + [p + PREF_SUFFIX for p in prefs] + [self.full_name(c) for c in sorted(self.regions)]
            completer = QtWidgets.QCompleter(names, self)
            completer.setFilterMode(QtCore.Qt.MatchContains)
            completer.activated.connect(lambda text: self.on_search(text))
            self.search.setCompleter(completer)
            if self.pending:
                self.show_with(*self.pending)
            else:
                self.set_picks([JAPAN])     # 単独で開いたときは日本全体から
            self.need_trend(self.t_ind.currentData())
            for key in {"pop", self.s_x.currentData(), self.s_y.currentData()}:
                self.need(key, auto_log=True)
            return
        kind, key = name.split(":", 1)
        if kind in ("area", "official"):
            # 全国・都道府県の値や公式推計がない指標もある（転入超過率など）。そのときは市区町村だけ表示する
            (self.area if kind == "area" else self.official)[key] = {} if err else result
            self.redraw_all()
            return
        if err:
            self.set_status(f"データを取得できませんでした：{err}", "rec")
            return
        self.data[key] = result
        if getattr(self, "_auto_log_pending", False):
            self.auto_log()
        self.redraw_all()

    def redraw_all(self, *_):
        if self.tabs.currentIndex() == 0:
            self.redraw_trend()
        else:
            self.redraw_scatter(reset_range=True)

    def show_with(self, key, pref, code):
        """地域統計ウィンドウから開いたとき：表示中の指標・範囲・選んだ市区町村を引き継ぐ"""
        if not self.regions:
            self.pending = (key, pref, code)
            return
        self.pending = None
        if key:
            self.t_ind.setCurrentIndex(self.t_ind.findData(key))
        if pref and self.pref.findText(pref) >= 0:
            self.pref.setCurrentText(pref)
        if code and code not in self.picks:
            self.add_pick(code)
        self.need_trend(self.t_ind.currentData())

    # ---------- 比べる市区町村 ----------
    def set_picks(self, codes):
        valid = lambda c: c == JAPAN or c in self.pref_name or c in self.regions
        self.picks = [c for c in codes if c and valid(c)][:MAX_PICKS]
        self.pick_list.clear()
        for i, c in enumerate(self.picks):
            pix = QtGui.QPixmap(14, 14)
            pix.fill(QtGui.QColor(SERIES_COLORS[i]))
            it = QtWidgets.QListWidgetItem(QtGui.QIcon(pix), self.full_name(c))
            it.setData(QtCore.Qt.UserRole, c)
            self.pick_list.addItem(it)
        self.redraw_all()

    def add_pick(self, code):
        if not code or code in self.picks:
            return
        if len(self.picks) >= MAX_PICKS:
            self.set_status(f"比べられるのは {MAX_PICKS} つまでです。「選択を外す」で減らしてください", "idle")
            return
        self.set_picks(self.picks + [code])

    def remove_pick(self):
        it = self.pick_list.currentItem()
        if it:
            self.set_picks([c for c in self.picks if c != it.data(QtCore.Qt.UserRole)])

    def on_search(self, text=None):
        text = (text if isinstance(text, str) else self.search.text()).strip()
        key = text.replace(" ", "").replace("　", "").replace(PREF_SUFFIX, "")
        if not key:
            return
        if key in ("全国", "日本", "日本全体"):
            self.add_pick(JAPAN)
            self.search.clear()
            return
        # 都道府県：正式名（東京都）か、「東京」のように市区町村名と紛れないとき
        pref_hit = next((c for c, p in self.pref_name.items()
                         if key == p or (len(key) >= 2 and p.startswith(key) and len(p) - len(key) <= 1)), None)
        if pref_hit and (key in self.pref_name.values()
                         or not any(n.startswith(key) for _, n in self.regions.values())):
            self.add_pick(pref_hit)
            self.search.clear()
            return
        hits = [c for c in sorted(self.regions) if key in self.full_name(c).replace(" ", "")]
        hits.sort(key=lambda c: (not self.regions[c][1].startswith(key), len(self.regions[c][1])))
        if not hits:
            self.set_status(f"「{text}」に当てはまる地域がありません", "idle")
            return
        self.add_pick(hits[0])
        self.search.clear()

    def closeEvent(self, ev):
        self.s_play.setChecked(False)
        super().closeEvent(ev)


def main():
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    app = QtWidgets.QApplication(sys.argv)
    theme.apply(app)
    win = RegionChartsWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

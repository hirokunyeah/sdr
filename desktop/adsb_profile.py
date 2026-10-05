"""
ADS-B 断面図パネル（機体の位置関係を横から見た図）

  横軸：基準からの水平距離（km）、縦軸：高度（ft）
  基準は「選択した機体」または「受信地点」。機体を基準にすると、その機体を左端（距離0）に置き、
  他の機体との水平距離と高度差を表示する。水平 5NM 未満かつ高度差 1000ft 未満の機体は赤で強調する
  （管制レーダー間隔の目安。あくまで受信データからの参考表示）
"""
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import theme
from adsb_decoder import distance_km

SEP_H_KM = 9.26     # 5NM
SEP_V_FT = 1000
NEAR = "#ff5252"
LABEL_GAP = 6       # 点とラベルの間隔（ピクセル）


def alt_color(alt):
    """地図と同じ高度の色分け（低い：橙 → 高い：紫）"""
    t = max(0.0, min(1.0, round(alt / 1000) / 40))
    return QtGui.QColor.fromHslF((30 + t * 250) / 360, 0.85, 0.60)


class ProfilePanel(QtWidgets.QGroupBox):
    selected = QtCore.Signal(str)   # 点をクリックした機体の ICAO

    def __init__(self):
        super().__init__("断面図（横から見た位置関係）")
        self.last = None   # 最後に描いた (planes, selected, home)。基準を切り替えたときに描き直す

        self.ref_mode = theme.Segmented(["選択した機体", "受信地点"])
        self.info = theme.caption("")
        head = QtWidgets.QHBoxLayout()
        head.addWidget(theme.caption("基準"))
        head.addWidget(self.ref_mode)
        head.addSpacing(12)
        head.addWidget(self.info, 1)

        self.plot = pg.PlotWidget(background=theme.C["plot"])
        self.plot.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        # マウスで拡大・移動したらその表示を保つ。ダブルクリックで全機が入る表示に戻す
        self.auto_range = True
        self.vb = self.plot.getViewBox()
        self.vb.sigRangeChangedManually.connect(lambda *_: setattr(self, "auto_range", False))
        self.plot.scene().sigMouseClicked.connect(self._on_click)
        self.plot.setLabel("left", "高度 ft")
        self.plot.setLabel("bottom", "基準からの水平距離 km")
        axis_font = theme.font(theme.MONO_FONTS, 8)
        for ax in ("left", "bottom"):
            a = self.plot.getAxis(ax)
            a.enableAutoSIPrefix(False)
            a.setTickFont(axis_font)
            a.setPen(theme.C["line"])
            a.setTextPen(theme.C["muted"])

        # 基準機の高度 ±1000ft の帯と、水平 5NM の線（機体基準のときだけ表示）
        band_brush = QtGui.QColor(theme.C["accent"])
        band_brush.setAlpha(28)
        self.band = pg.LinearRegionItem(orientation="horizontal", movable=False, brush=band_brush,
                                        pen=pg.mkPen(None))
        self.ref_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(theme.C["accent"], width=1, style=QtCore.Qt.DashLine))
        self.sep_line = pg.InfiniteLine(
            pos=SEP_H_KM, angle=90, pen=pg.mkPen(theme.C["dim"], width=1, style=QtCore.Qt.DashLine),
            label="5NM", labelOpts={"position": 0.95, "color": theme.C["muted"]})
        for item in (self.band, self.ref_line, self.sep_line):
            item.setZValue(-10)
            self.plot.addItem(item)

        self.scatter = pg.ScatterPlotItem(pxMode=True, hoverable=True)
        self.scatter.sigClicked.connect(lambda _item, pts, _ev: pts and self.selected.emit(pts[0].data()))
        self.plot.addItem(self.scatter)
        self.labels = []
        self.label_font = theme.font(theme.MONO_FONTS, 8)

        hint = theme.caption("点のクリックでその機体を選択（「選択した機体」基準なら、その機体が基準になります）。"
                             "ホイール・ドラッグで拡大・移動（重なるラベルは隠れ、拡大すると表示）、ダブルクリックで元の表示に戻ります。"
                             f"帯：基準機の高度 ±{SEP_V_FT:,}ft　赤：水平 5NM 未満かつ高度差 {SEP_V_FT:,}ft 未満　"
                             "中抜きの点：位置がインターネットのデータ", "hint")
        hint.setWordWrap(True)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(self.plot, 1)
        lay.addWidget(hint)

        self.ref_mode.currentIndexChanged.connect(self.redraw)
        self.vb.sigRangeChanged.connect(lambda *_: QtCore.QTimer.singleShot(0, self._place_labels))
        self.update_planes([], None, None)

    def redraw(self):
        if self.last:
            self.update_planes(*self.last)

    def _on_click(self, ev):
        if ev.double():
            self.auto_range = True
            self.redraw()

    def update_planes(self, planes, selected, home):
        self.last = (planes, selected, home)
        usable = [a for a in planes if a["lat"] is not None and a["alt"] is not None]
        hidden = len(planes) - len(usable)
        ref = None
        if self.ref_mode.currentIndex() == 0:
            ref = next((a for a in usable if a["icao"] == selected), None)

        for lab in self.labels:
            self.plot.removeItem(lab)
        self.labels = []
        for item in (self.band, self.ref_line, self.sep_line):
            item.setVisible(ref is not None)

        if ref:
            origin = (ref["lat"], ref["lon"])
            self.band.setRegion((ref["alt"] - SEP_V_FT, ref["alt"] + SEP_V_FT))
            self.ref_line.setValue(ref["alt"])
            info = f"基準：{ref['callsign'] or ref['icao']}（{ref['alt']:,}ft）"
        else:
            origin = home
            info = "基準：受信地点"
            if self.ref_mode.currentIndex() == 0 and usable:
                info += "（機体が選択されていないため。一覧・地図・この図の点をクリックすると機体基準になります）"
        info += f"　表示 {len(usable)}機"
        if hidden:
            info += f"（位置または高度が不明の {hidden}機は除外）"
        self.info.setText(info)

        spots = []
        for a in usable:
            name = a["callsign"] or a["icao"]
            x = distance_km(origin[0], origin[1], a["lat"], a["lon"]) if origin else 0.0
            is_ref = ref is not None and a["icao"] == ref["icao"]
            near = False
            if is_ref:
                text = f"{name}（基準）"
            elif ref:
                dalt = a["alt"] - ref["alt"]
                near = x < SEP_H_KM and abs(dalt) < SEP_V_FT
                text = f"{name}\n{x:.1f}km {dalt:+,}ft"
            else:
                text = f"{name}\n{x:.0f}km {a['alt']:,}ft"
            color = alt_color(a["alt"])
            net = a.get("src") == "net"      # インターネットのデータの機体は中抜きの点で描く
            fill = QtGui.QColor(color)
            if net:
                fill.setAlpha(40)
            edge = NEAR if near else "#ffffff" if a["icao"] == selected else color if net else theme.C["plot"]
            spots.append({
                "pos": (x, a["alt"]), "data": a["icao"],
                "symbol": "d" if is_ref else "o", "size": 15 if is_ref else 10,
                "brush": pg.mkBrush(fill),
                "pen": pg.mkPen(edge, width=2.5 if near or a["icao"] == selected else 1.5 if net else 1),
            })
            lab = pg.TextItem(text, color=NEAR if near else theme.C["text"], anchor=(0, 1))
            lab.setFont(self.label_font)
            # 基準機・接近機・選択機のラベルを優先して良い位置に置く
            lab.prio = 0 if is_ref else 1 if near else 2 if a["icao"] == selected else 3
            lab.point = (x, a["alt"])
            self.plot.addItem(lab)
            self.labels.append(lab)
        self.scatter.setData(spots=spots)
        if self.auto_range:
            self._fit(spots)
        self._place_labels()

    def _fit(self, spots):
        """全機とラベルが入る範囲にする（右端のラベルが切れないよう右側に余白を取る）"""
        xs = [s["pos"][0] for s in spots] or [0, 50]
        ys = [s["pos"][1] for s in spots] or [0, 40000]
        x1 = max(max(xs), SEP_H_KM * 1.2)
        y0, y1 = min(0, min(ys)), max(ys)
        self.vb.setRange(xRange=(min(0, min(xs)) - x1 * 0.04, x1 * 1.18),
                         yRange=(y0 - 1500, y1 + max(3000, (y1 - y0) * 0.15)), padding=0)

    def _place_labels(self):
        """ラベル同士が重ならないよう、点の右上・右下・左上・左下…の順に空いている位置を探して置く"""
        if not self.labels:
            return
        psx, psy = self.vb.viewPixelSize()   # 1ピクセルあたりの km・ft
        if not psx or not psy:
            return
        fm = QtGui.QFontMetrics(self.label_font)
        (vx0, _), (_, vy1) = self.vb.viewRange()
        placed = []
        view = QtCore.QRectF(0, 0, self.vb.width(), self.vb.height())
        for lab in sorted(self.labels, key=lambda l: (l.prio, l.point[0])):
            lines = lab.toPlainText().split("\n")
            w = max(fm.horizontalAdvance(t) for t in lines) + 8
            h = fm.height() * len(lines) + 4
            px, py = (lab.point[0] - vx0) / psx, (vy1 - lab.point[1]) / psy   # 点の画面位置（左上原点）
            g = LABEL_GAP
            cands = [(g, -g - h), (g, g), (-g - w, -g - h), (-g - w, g),
                     (g, -g - 2 * h), (g, g + h), (-g - w, -g - 2 * h), (-g - w, g + h)]
            rects = [QtCore.QRectF(px + dx, py + dy, w, h) for dx, dy in cands]
            free = [r for r in rects if view.contains(r) and not any(r.intersects(o) for o in placed)]
            # 図の中で他と重ならない置き場所がないラベルは隠す（基準機・接近機・選択機は必ず表示）。拡大すると表示される
            lab.setVisible(bool(free) or lab.prio <= 2)
            if not lab.isVisible():
                continue
            r = free[0] if free else next((r for r in rects if view.contains(r)), rects[0])
            placed.append(r)
            # TextItem は anchor=(0,1) なので、ラベルの左下の位置を渡す
            lab.setPos(vx0 + r.left() * psx, vy1 - r.bottom() * psy)

"""
気象衛星（Meteor-M LRPT）受信ウィンドウ

  1. 観測地点と軌道データ(TLE)から、衛星が上空を通過する時刻（パス）を予測
  2. パスの時刻になったら自動で 137MHz 帯を録音（IQ, 240kS/s, s16）
  3. 通過後に SatDump を呼び出して画像に変換し、ウィンドウに表示
"""
import glob
import json
import os
import shutil
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

from PySide6 import QtCore, QtGui, QtWidgets

import theme

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "satellite_settings.json")
TLE_FILE = os.path.join(HERE, "satellite_tle.txt")
DATA_DIR = os.path.join(HERE, "satellite_data")
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={}&FORMAT=tle"
REC_RATE = 240_000

# 受信対象（周波数は運用状況で 137.1MHz に切り替わることがある）
SATELLITES = [
    {"name": "METEOR-M2 3", "norad": 57166, "freq": 137.900},
    {"name": "METEOR-M2 4", "norad": 59051, "freq": 137.900},
]

DEFAULTS = {
    "lat": 35.68, "lon": 139.77, "min_elev": 25.0, "rec_elev": 10.0,
    "satdump": "", "auto": False, "delete_baseband": True,
    "freqs": {str(s["norad"]): s["freq"] for s in SATELLITES},
}


def find_satdump():
    exe = shutil.which("satdump")
    if exe:
        return exe
    for p in (os.path.normpath(os.path.join(HERE, "..", "tools", "windows", "SatDump", "satdump.exe")),
              r"C:\Program Files\SatDump\satdump.exe",
              r"C:\Program Files (x86)\SatDump\satdump.exe"):
        if os.path.exists(p):
            return p
    return ""


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        s.update({k: v for k, v in saved.items() if k != "freqs"})
        s["freqs"].update(saved.get("freqs", {}))
    except (OSError, ValueError):
        pass
    if not s["satdump"] or not os.path.exists(s["satdump"]):  # 未設定・移動済みなら探し直す
        s["satdump"] = find_satdump() or s["satdump"]
    return s


# ---------------- パス予測 ----------------
def predict_passes(tle_text, lat, lon, hours=36, rec_elev=10.0):
    """[(衛星名, NORAD, 開始UTC, 最大仰角時刻UTC, 終了UTC, 最大仰角)] を返す"""
    from skyfield.api import EarthSatellite, load, wgs84

    ts = load.timescale()
    loc = wgs84.latlon(lat, lon)
    lines = [l.strip() for l in tle_text.splitlines() if l.strip()]
    sats = []
    for i in range(len(lines) - 2):
        if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            sats.append(EarthSatellite(lines[i + 1], lines[i + 2], lines[i], ts))

    now = datetime.now(timezone.utc)
    t0, t1 = ts.from_datetime(now - timedelta(minutes=20)), ts.from_datetime(now + timedelta(hours=hours))
    passes = []
    for sat in sats:
        times, events = sat.find_events(loc, t0, t1, altitude_degrees=rec_elev)
        aos = tca = None
        max_el = 0.0
        for t, e in zip(times, events):
            if e == 0:
                aos, tca, max_el = t, None, 0.0
            elif e == 1 and aos is not None:
                el = (sat - loc).at(t).altaz()[0].degrees
                if el > max_el:
                    tca, max_el = t, el
            elif e == 2 and aos is not None and tca is not None:
                passes.append((sat.name.strip(), sat.model.satnum, aos.utc_datetime(),
                               tca.utc_datetime(), t.utc_datetime(), max_el))
                aos = None
    passes.sort(key=lambda p: p[2])
    return passes


# ---------------- ウィンドウ ----------------
class SatelliteWindow(QtWidgets.QWidget):
    tle_done = QtCore.Signal(str, str)   # (TLE本文, エラー)

    def __init__(self, main):
        super().__init__(None, QtCore.Qt.Window)
        self.main = main
        self.setWindowTitle("気象衛星（Meteor-M LRPT）")
        self.resize(1180, 820)
        self.cfg = load_settings()
        self.passes = []
        self.done_passes = set()
        self.recording = None     # {"pass":..., "dir":..., "file":..., "end":...}
        self.proc = None

        # --- 設定 ---
        self.lat = self._spin(-90, 90, self.cfg["lat"], 4, "°")
        self.lon = self._spin(-180, 180, self.cfg["lon"], 4, "°")
        self.min_elev = self._spin(0, 90, self.cfg["min_elev"], 0, "°")
        self.satdump = QtWidgets.QLineEdit(self.cfg["satdump"])
        self.satdump.setPlaceholderText("satdump の実行ファイル")
        browse = QtWidgets.QPushButton("参照…")
        browse.clicked.connect(self.browse_satdump)
        self.freq_edits = {}
        freq_box = QtWidgets.QHBoxLayout()
        for s in SATELLITES:
            sp = self._spin(130, 140, self.cfg["freqs"][str(s["norad"])], 4, " MHz")
            self.freq_edits[s["norad"]] = sp
            freq_box.addWidget(theme.caption(s["name"]))
            freq_box.addWidget(sp)
            freq_box.addSpacing(8)
        freq_box.addStretch()

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        form.setHorizontalSpacing(12)
        loc = QtWidgets.QHBoxLayout()
        for lab, w in (("緯度", self.lat), ("経度", self.lon), ("自動受信する最低仰角", self.min_elev)):
            loc.addWidget(theme.caption(lab))
            loc.addWidget(w)
            loc.addSpacing(8)
        loc.addStretch()
        form.addRow(theme.caption("観測地点", "heading"), loc)
        form.addRow(theme.caption("受信周波数", "heading"), freq_box)
        sd = QtWidgets.QHBoxLayout()
        sd.addWidget(self.satdump, 1)
        sd.addWidget(browse)
        form.addRow(theme.caption("SatDump", "heading"), sd)
        settings_box = QtWidgets.QGroupBox("設定")
        settings_box.setLayout(form)

        # --- 状態 ---
        self.state = QtWidgets.QLabel("")
        self.state.setObjectName("StatusCard")
        self.state.setWordWrap(True)

        # --- パス一覧 ---
        self.tle_btn = QtWidgets.QPushButton("🔄 軌道データ更新")
        self.pass_table = QtWidgets.QTableWidget(0, 5)
        self.pass_table.setHorizontalHeaderLabels(["衛星", "開始", "最大仰角の時刻", "終了", "最大仰角"])
        self.pass_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.pass_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.pass_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.pass_table.setAlternatingRowColors(True)
        self.pass_table.setShowGrid(False)
        self.pass_table.verticalHeader().hide()
        self.pass_table.verticalHeader().setDefaultSectionSize(30)

        self.auto = QtWidgets.QCheckBox("自動受信（パスの時刻に自動で録音・デコード）")
        self.auto.setChecked(self.cfg["auto"])
        pass_box = QtWidgets.QGroupBox("今後のパス（36時間）")
        pl = QtWidgets.QVBoxLayout(pass_box)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self.auto)
        head.addStretch()
        head.addWidget(self.tle_btn)
        pl.addLayout(head)
        pl.addWidget(self.pass_table, 1)
        legend = theme.caption("太字の青：次に自動受信するパス　灰色：最低仰角より低いパス（自動受信しない）", "hint")
        pl.addWidget(legend)

        # --- 手動録音 ---
        self.delete_bb = QtWidgets.QCheckBox("デコード後に録音ファイルを削除（1パス約0.8GB）")
        self.delete_bb.setChecked(self.cfg["delete_baseband"])
        self.manual_sat = QtWidgets.QComboBox()
        for s in SATELLITES:
            self.manual_sat.addItem(s["name"], s["norad"])
        self.rec_btn = QtWidgets.QPushButton("● 今すぐ録音")
        self.rec_btn.setProperty("kind", "danger")
        self.decode_btn = QtWidgets.QPushButton("📂 録音ファイルをデコード…")
        manual_box = QtWidgets.QGroupBox("手動録音・デコード")
        ml = QtWidgets.QVBoxLayout(manual_box)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.manual_sat)
        row.addWidget(self.rec_btn)
        row.addStretch()
        row.addWidget(self.decode_btn)
        ml.addLayout(row)
        ml.addWidget(self.delete_bb)

        # --- ログ ---
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFont(theme.font(theme.MONO_FONTS, 9))
        self.log.setPlaceholderText("ログ")
        log_box = QtWidgets.QGroupBox("ログ")
        QtWidgets.QVBoxLayout(log_box).addWidget(self.log)

        left = QtWidgets.QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(4)
        left.addWidget(self.state)
        left.addWidget(pass_box, 3)
        left.addWidget(manual_box)
        left.addWidget(settings_box)
        left.addWidget(log_box, 2)
        left_w = QtWidgets.QWidget()
        left_w.setLayout(left)

        # --- 画像 ---
        self.image_list = QtWidgets.QListWidget()
        self.image_view = QtWidgets.QLabel("受信した画像がここに表示されます")
        self.image_view.setObjectName("ImageView")
        self.image_view.setAlignment(QtCore.Qt.AlignCenter)
        self.image_view.setMinimumSize(200, 200)
        self.image_view.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        open_dir = QtWidgets.QPushButton("📁 フォルダを開く")
        open_dir.clicked.connect(lambda: self._open_dir(self.image_list.currentItem()))
        image_box = QtWidgets.QGroupBox("受信画像")
        right = QtWidgets.QVBoxLayout(image_box)
        right.addWidget(self.image_view, 3)
        right.addWidget(self.image_list, 1)
        right.addWidget(open_dir, 0, QtCore.Qt.AlignRight)

        split = QtWidgets.QSplitter()
        split.addWidget(left_w)
        split.addWidget(image_box)
        split.setSizes([620, 480])
        split.setChildrenCollapsible(False)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.addWidget(split)

        # --- イベント ---
        self.tle_btn.clicked.connect(self.update_tle)
        self.tle_done.connect(self.on_tle)
        self.rec_btn.clicked.connect(self.toggle_manual)
        self.decode_btn.clicked.connect(self.decode_file_dialog)
        self.image_list.currentItemChanged.connect(lambda cur, _: self.show_image(cur))
        for w in (self.lat, self.lon, self.min_elev):
            w.valueChanged.connect(self.on_settings_changed)
        for w in self.freq_edits.values():
            w.valueChanged.connect(self.save_settings)
        self.satdump.editingFinished.connect(self.save_settings)
        self.auto.toggled.connect(self.save_settings)
        self.delete_bb.toggled.connect(self.save_settings)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)

        os.makedirs(DATA_DIR, exist_ok=True)
        self.refresh_images()
        if os.path.exists(TLE_FILE):
            self.recalc()
            age = datetime.now().timestamp() - os.path.getmtime(TLE_FILE)
            if age > 3 * 86400:
                self.update_tle()
        else:
            self.update_tle()
        if not self.cfg["satdump"]:
            self.write_log("SatDump が見つかりません。Windows では setup_windows.bat を実行してください。"
                           "別の場所にある場合は「参照…」から satdump の実行ファイルを指定してください。")

    # ---------- ユーティリティ ----------
    @staticmethod
    def _spin(lo, hi, val, dec, suffix):
        sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(dec)
        sp.setValue(val)
        sp.setSuffix(suffix)
        sp.setKeyboardTracking(False)
        sp.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)   # 入力またはホイールで変更
        return sp

    def set_status(self, text, tone=""):
        """上部の状態カードを更新する。tone: rec / busy / idle / ""（通常）"""
        self.state.setText(text)
        if self.state.property("tone") != tone:
            theme.set_prop(self.state, "tone", tone)

    def write_log(self, text):
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def save_settings(self):
        self.cfg.update(lat=self.lat.value(), lon=self.lon.value(), min_elev=self.min_elev.value(),
                        satdump=self.satdump.text().strip(), auto=self.auto.isChecked(),
                        delete_baseband=self.delete_bb.isChecked(),
                        freqs={str(k): w.value() for k, w in self.freq_edits.items()})
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.write_log(f"設定を保存できません: {e}")

    def on_settings_changed(self):
        self.save_settings()
        self.recalc()

    def browse_satdump(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "SatDump の実行ファイル", "", "satdump (satdump.exe)")
        if path:
            self.satdump.setText(path)
            self.save_settings()

    # ---------- 軌道データ ----------
    def update_tle(self):
        self.tle_btn.setEnabled(False)
        self.write_log("軌道データ（TLE）を CelesTrak から取得しています…")

        def job():
            try:
                parts = []
                for s in SATELLITES:
                    with urllib.request.urlopen(TLE_URL.format(s["norad"]), timeout=20) as r:
                        parts.append(r.read().decode("ascii", "replace").strip())
                self.tle_done.emit("\n".join(parts), "")
            except Exception as e:
                self.tle_done.emit("", str(e))

        threading.Thread(target=job, daemon=True).start()

    def on_tle(self, text, err):
        self.tle_btn.setEnabled(True)
        if err or "1 " not in text:
            self.write_log(f"軌道データの取得に失敗しました: {err or text[:100]}")
            return
        with open(TLE_FILE, "w", encoding="ascii") as f:
            f.write(text)
        self.write_log("軌道データを更新しました")
        self.recalc()

    def recalc(self):
        try:
            with open(TLE_FILE, encoding="ascii") as f:
                tle = f.read()
            self.passes = predict_passes(tle, self.lat.value(), self.lon.value(),
                                         rec_elev=self.cfg["rec_elev"])
        except Exception as e:
            self.write_log(f"パス予測に失敗しました: {e}")
            return
        now = datetime.now(timezone.utc)
        self.pass_table.setRowCount(0)
        next_found = False
        for p in self.passes:
            if p[4] < now:
                continue
            r = self.pass_table.rowCount()
            self.pass_table.insertRow(r)
            low = p[5] < self.min_elev.value()
            is_next = not low and not next_found
            next_found = next_found or is_next
            vals = [p[0], self._local(p[2], True), self._local(p[3]), self._local(p[4]), f"{p[5]:.0f}°"]
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                it.setTextAlignment(QtCore.Qt.AlignCenter)
                if low:
                    it.setForeground(QtGui.QColor(theme.C["dim"]))
                elif is_next:
                    it.setForeground(QtGui.QColor(theme.C["accent"]))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                self.pass_table.setItem(r, c, it)

    @staticmethod
    def _local(dt, with_date=False):
        return dt.astimezone().strftime("%m/%d %H:%M:%S" if with_date else "%H:%M:%S")

    # ---------- 録音 ----------
    def _freq_for(self, norad):
        w = self.freq_edits.get(norad)
        return (w.value() if w else 137.9) * 1e6

    def start_record(self, name, norad, end=None, pass_key=None):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        d = os.path.join(DATA_DIR, f"{stamp}_{name.replace(' ', '-')}")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"baseband_{REC_RATE}.s16")
        freq = self._freq_for(norad)
        err = self.main.start_recording(freq, path)
        if err:
            self.write_log(f"録音を開始できません: {err}")
            return
        self.recording = {"dir": d, "file": path, "end": end, "key": pass_key, "name": name}
        self.rec_btn.setText("■ 録音停止")
        theme.set_prop(self.rec_btn, "active", "true")
        self.write_log(f"{name} の録音を開始しました（{freq / 1e6:.4f} MHz）")

    def stop_record(self, decode=True):
        if not self.recording:
            return
        nbytes = self.main.stop_recording()
        rec, self.recording = self.recording, None
        self.rec_btn.setText("● 今すぐ録音")
        theme.set_prop(self.rec_btn, "active", "false")
        self.write_log(f"録音を終了しました（{nbytes / 1e6:.0f} MB）")
        if decode:
            self.decode(rec["file"], rec["dir"])

    def toggle_manual(self):
        if self.recording:
            self.stop_record()
        else:
            self.start_record(self.manual_sat.currentText(), self.manual_sat.currentData())

    def tick(self):
        now = datetime.now(timezone.utc)
        if self.recording:
            end = self.recording["end"]
            size = os.path.getsize(self.recording["file"]) / 1e6 if os.path.exists(self.recording["file"]) else 0
            remain = f"／ 終了まで {int((end - now).total_seconds())} 秒" if end else ""
            self.set_status(f"🔴 録音中：{self.recording['name']}　{size:.0f} MB {remain}", "rec")
            if end and now >= end:
                self.stop_record()
            return
        if self.proc:
            self.set_status("⏳ SatDump でデコード中…", "busy")
            return

        upcoming = [p for p in self.passes if p[4] > now and p[5] >= self.min_elev.value()]
        if not upcoming:
            self.set_status("予定されているパスはありません（軌道データを更新してください）", "idle")
            return
        p = upcoming[0]
        key = (p[1], p[2].isoformat())
        if p[2] <= now:
            if self.auto.isChecked() and key not in self.done_passes:
                self.done_passes.add(key)
                self.start_record(p[0], p[1], end=p[4], pass_key=key)
            else:
                self.set_status(f"🛰 {p[0]} が通過中（最大仰角 {p[5]:.0f}°）")
            return
        wait = int((p[2] - now).total_seconds())
        h, m, s = wait // 3600, wait % 3600 // 60, wait % 60
        mode = "自動受信 待機中" if self.auto.isChecked() else "次のパス"
        self.set_status(f"{mode}：{p[0]}　{self._local(p[2])} 開始　あと {h}:{m:02d}:{s:02d}　最大仰角 {p[5]:.0f}°",
                        "" if self.auto.isChecked() else "idle")

    # ---------- デコード ----------
    def decode_file_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "録音ファイル（240kS/s s16）", DATA_DIR, "IQ (*.s16 *.raw);;すべて (*)")
        if path:
            self.decode(path, os.path.dirname(path))

    def decode(self, path, folder):
        exe = self.satdump.text().strip()
        if not exe or not os.path.exists(exe):
            self.write_log("SatDump が設定されていないため、デコードできません。録音ファイルは残してあります。")
            return
        out = os.path.join(folder, "decoded")
        args = ["meteor_m2-x_lrpt", "baseband", path, out,
                "--samplerate", str(REC_RATE), "--baseband_format", "s16", "--fill_missing"]
        self.write_log("SatDump 実行: " + " ".join([os.path.basename(exe)] + args))
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.proc.setWorkingDirectory(os.path.dirname(exe))
        self.proc.readyReadStandardOutput.connect(self._read_proc)
        self.proc.finished.connect(lambda code, _st, p=path, o=out: self._decoded(code, p, o))
        self.proc.errorOccurred.connect(lambda e: self.write_log(f"SatDump を起動できません: {e}"))
        self.proc.start(exe, args)

    def _read_proc(self):
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if line and ("%" not in line or "100" in line):   # 進捗の連続行は省く
                self.log.appendPlainText(line)

    def _decoded(self, code, path, out):
        self.proc = None
        pngs = glob.glob(os.path.join(out, "**", "*.png"), recursive=True)
        if pngs:
            self.write_log(f"デコード完了：画像 {len(pngs)} 枚")
            if self.delete_bb.isChecked():
                try:
                    os.remove(path)
                    self.write_log("録音ファイルを削除しました")
                except OSError:
                    pass
        else:
            self.write_log(f"画像を作れませんでした（終了コード {code}）。電波が弱かった可能性があります。"
                           "録音ファイルは残してあります。")
        self.refresh_images(select_dir=out)

    # ---------- 画像 ----------
    def refresh_images(self, select_dir=None):
        self.image_list.clear()
        files = sorted(glob.glob(os.path.join(DATA_DIR, "*", "decoded", "**", "*.png"), recursive=True),
                       reverse=True)
        select = None
        for f in files:
            rel = os.path.relpath(f, DATA_DIR)
            it = QtWidgets.QListWidgetItem(self._image_label(rel))
            it.setToolTip(rel)
            it.setData(QtCore.Qt.UserRole, f)
            self.image_list.addItem(it)
            if select is None and select_dir and f.startswith(select_dir):
                select = it
        if select:
            self.image_list.setCurrentItem(select)

    @staticmethod
    def _image_label(rel):
        """'20261005_094437_METEOR-M2-3/decoded/MSU-MR/x.png' → '10/05 09:44  METEOR-M2-3  ·  MSU-MR/x.png'"""
        parts = rel.replace("\\", "/").split("/")
        try:
            t = datetime.strptime(parts[0][:15], "%Y%m%d_%H%M%S")
            head = f"{t:%m/%d %H:%M}  {parts[0][16:]}"
        except ValueError:
            head = parts[0]
        return f"{head}  ·  {'/'.join(parts[2:])}"

    def show_image(self, item):
        if not item:
            return
        pix = QtGui.QPixmap(item.data(QtCore.Qt.UserRole))
        self._pix = pix
        self._fit()

    def _fit(self):
        pix = getattr(self, "_pix", None)
        if pix and not pix.isNull():
            self.image_view.setPixmap(pix.scaled(self.image_view.size(), QtCore.Qt.KeepAspectRatio,
                                                 QtCore.Qt.SmoothTransformation))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _open_dir(self, item):
        d = os.path.dirname(item.data(QtCore.Qt.UserRole)) if item else DATA_DIR
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(d))

    # ---------- 終了処理 ----------
    def closeEvent(self, ev):
        # 自動受信を続けられるよう、閉じても裏で動かし続ける
        ev.ignore()
        self.hide()
        if self.auto.isChecked():
            self.main.statusBar().showMessage("衛星ウィンドウは閉じましたが、自動受信は続いています", 5000)

    def shutdown(self):
        if self.recording:
            self.stop_record(decode=False)
        if self.proc:
            self.proc.kill()

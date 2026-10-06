"""
地域統計（市区町村別）のデータ取得

  市区町村の境界と統計値をインターネットから取得し、region_data フォルダにキャッシュする。
  - 境界：国土数値情報（行政区域）を軽量化した TopoJSON（スマートニュース メディア研究所が公開）。
    政令指定都市は区をまとめて1つの市として扱う
  - 統計：e-Stat の「統計ダッシュボード」API（appId 不要）。人口・世帯・住宅・自動車など
  - 所得：総務省「市町村税課税状況等の調」の市町村別内訳（Excel）。xlsx は標準ライブラリで読む
  いずれも取得は数秒〜十数秒かかるので、ウィンドウ側で別スレッドから呼ぶ。
"""
import gzip
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "region_data")
CACHE_DAYS = 30           # 統計値はこれより古いキャッシュを取り直す（境界は取り直さない）

BOUNDARY_URL = ("https://raw.githubusercontent.com/smartnews-smri/japan-topography/main/"
                "data/municipality/topojson/s0010/N03-21_210101_designated_city.json")
DASHBOARD_URL = "https://dashboard.e-stat.go.jp/api/1.0/Json/getData"
DASHBOARD_FROM = 2000     # 取得する最初の年（API は1回 10万件まで。市区町村 × 年で収まる範囲）
SOUMU_URL = "https://www.soumu.go.jp/main_sosiki/jichi_zeisei/czaisei/czaisei_seido/xls/J51-{yy}-b.xlsx"
SOUMU_YEARS = 6           # 所得は今年度から何年度さかのぼって探すか

CREDIT = ('統計：<a href="https://dashboard.e-stat.go.jp/">統計ダッシュボード</a>（総務省統計局）、'
          '<a href="https://www.soumu.go.jp/main_sosiki/jichi_zeisei/czaisei/czaisei_seido/ichiran09.html">'
          '市町村税課税状況等の調</a>（総務省）　境界：国土数値情報（行政区域データ）（国土交通省）を加工して作成')

USER_AGENT = "sdr-app-region-stats/1.0"


@dataclass
class Indicator:
    key: str
    group: str
    name: str
    unit: str
    spec: tuple           # ("dash", 指標コード) / ("ratio", 分子, 分母, 倍率) / ("income", 項目)
    digits: int = 0       # 表示する小数点以下の桁数
    diverging: bool = False   # 0 を中心に増減を色分けする（増減率など）
    note: str = ""


def dash(code):
    return ("dash", code)


POP = dash("0201010000000010000")        # 総人口
HOUSEHOLDS = dash("0202000000000010000")  # 世帯数

INDICATORS = [
    Indicator("pop", "人口", "総人口", "人", POP),
    Indicator("density", "人口", "人口密度", "人/km²", ("ratio", POP, dash("0101010000000010010"), 100),
              note="総人口 ÷ 総面積"),
    Indicator("old", "人口", "65歳以上の割合", "%", dash("0201010010000020030"), 1),
    Indicator("young", "人口", "15歳未満の割合", "%", dash("0201010010000020010"), 1),
    Indicator("growth", "人口", "人口増減率", "%", dash("0201010000000030000"), 2, True,
              "前回の国勢調査からの増減"),
    Indicator("migration", "人口", "転入超過率", "%", dash("0204060000000020020"), 2, True,
              "（転入者数 − 転出者数）÷ 人口"),
    Indicator("daytime", "人口", "昼夜間人口比率", "%", ("ratio", dash("0201060000000010000"), POP, 100), 1,
              note="昼間人口 ÷ 夜間人口（総人口）。100 を超えると通勤・通学で人が集まる地域"),
    Indicator("foreign", "人口", "外国人人口（人口10万人当たり）", "人", dash("0201100001000010008")),
    Indicator("households", "世帯・住宅", "世帯数", "世帯", HOUSEHOLDS),
    Indicator("hh_size", "世帯・住宅", "1世帯当たり人員", "人", dash("0202030000000020010"), 2),
    Indicator("old_alone", "世帯・住宅", "65歳以上の単独世帯の割合", "%", dash("0202010401000020010"), 1),
    Indicator("own_house", "世帯・住宅", "持ち家比率", "%", dash("0801010102010020000"), 1,
              note="住宅・土地統計調査（5年ごと）"),
    Indicator("vacant", "世帯・住宅", "空き家比率", "%", dash("0801010101000020040"), 1,
              note="住宅・土地統計調査（5年ごと）"),
    Indicator("floor", "世帯・住宅", "1住宅当たり延べ面積", "m²", dash("0801010401000010000"), 1,
              note="住宅・土地統計調査（5年ごと）"),
    Indicator("kei", "自動車", "軽自動車等の台数", "台", dash("1001040200000010010"),
              note="軽自動車税の課税台数（原付・二輪を含む）。普通乗用車の市区町村別の数は公開 API がありません"),
    Indicator("kei_hh", "自動車", "1世帯当たり軽自動車等の台数", "台",
              ("ratio", dash("1001040200000010010"), HOUSEHOLDS, 1), 2),
    Indicator("income_per", "所得・経済", "納税者1人当たり課税対象所得", "万円", ("income", "per"),
              note="課税対象所得 ÷ 所得割の納税義務者数（総務省「市町村税課税状況等の調」）。"
                   "年度は課税年度で、前年の所得が対象"),
    Indicator("income_total", "所得・経済", "課税対象所得の総額", "億円", ("income", "total"), 1),
    Indicator("taxpayers", "所得・経済", "所得割の納税義務者数", "人", ("income", "payers")),
    Indicator("unemp", "所得・経済", "完全失業率", "%", dash("0301100000020020010"), 1, note="国勢調査"),
    Indicator("offices", "所得・経済", "事業所数（民営）", "事業所", dash("0701010001000010012"),
              note="経済センサス"),
]
BY_KEY = {i.key: i for i in INDICATORS}


# ---------------- キャッシュ・取得 ----------------
def _path(name):
    return os.path.join(CACHE_DIR, name)


def _read_cache(name, max_age=None):
    try:
        if max_age is not None and time.time() - os.path.getmtime(_path(name)) > max_age:
            return None
        with gzip.open(_path(name), "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(name, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _path(name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _path(name))


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def boundaries():
    """市区町村の境界（TopoJSON の文字列）。一度取得したら取り直さない"""
    data = _read_cache("boundaries.json.gz")
    if data is None:
        data = json.loads(_get(BOUNDARY_URL))
        _write_cache("boundaries.json.gz", data)
    return data


def regions(topo):
    """境界データから {市区町村コード: (都道府県名, 市区町村名)}"""
    out = {}
    for obj in topo["objects"].values():
        for g in obj["geometries"]:
            p = g.get("properties") or {}
            code = p.get("N03_007")
            if code:
                name = (p.get("N03_003") or "") + (p.get("N03_004") or "")
                out[code] = (p.get("N03_001") or "", name)
    return out


def _dashboard(code):
    """統計ダッシュボードの1指標 → {年: {市区町村コード: 値}}"""
    # 期間の指定は、暦年の指標は "2000CY00"、年度の指標（比率など）は "2000FY00" の形で行う
    for cycle in ("CY", "FY"):
        url = (f"{DASHBOARD_URL}?Lang=JP&IndicatorCode={code}&RegionalRank=4"
               f"&TimeFrom={DASHBOARD_FROM}{cycle}00")
        res = json.loads(_get(url))["GET_STATS"]
        if "STATISTICAL_DATA" in res:
            break
    if res["RESULT"]["status"] != "0":
        raise RuntimeError(f"統計ダッシュボード：{res['RESULT']['errorMsg']}")
    rows = res["STATISTICAL_DATA"].get("DATA_INF", {}).get("DATA_OBJ", [])
    if isinstance(rows, dict):
        rows = [rows]
    out = {}
    for r in rows:
        v = r["VALUE"]
        try:
            value = float(v["$"])
        except (KeyError, ValueError):   # 秘匿・欠測は "-" や "***" になる
            continue
        out.setdefault(v["@time"][:4], {})[v["@regionCode"]] = value
    return out


def _xlsx_rows(data):
    """xlsx の最初のシートを行ごとの {列名: 値} で返す（openpyxl を使わない簡易版）"""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(data))
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
            strings.append("".join(t.text or "" for t in si.iter(ns + "t")))
    sheet = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))[0]
    for row in ET.fromstring(z.read(sheet)).iter(ns + "row"):
        cells = {}
        for c in row.iter(ns + "c"):
            v = c.find(ns + "v")
            if v is None:
                continue
            col = re.match(r"[A-Z]+", c.get("r")).group()
            cells[col] = strings[int(v.text)] if c.get("t") == "s" else v.text
        yield cells


def _income_year(data):
    """第11表 市町村別内訳 → {市区町村コード: (納税義務者数, 課税対象所得[千円])}"""
    cols = None
    out = {}
    for cells in _xlsx_rows(data):
        if cols is None:
            find = lambda word: next((k for k, v in cells.items() if v.startswith(word)), None)
            code, kind, payers, income = find("団体コード"), find("表側"), find("所得割の納税義務者数"), find("課税対象所得")
            if code and payers and income:
                cols = (code, kind, payers, income)
            continue
        code, kind, payers, income = (cells.get(k) for k in cols)
        # 市町村民税と道府県民税の2行ずつある。市町村民税の行を使う
        if not code or not re.fullmatch(r"\d{6}", code) or (kind and kind != "市町村民税"):
            continue
        try:
            out[code[:5]] = (float(payers), float(income))
        except (TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError("所得の表の形式が想定と違います")
    return out


def _income():
    """{年度: {市区町村コード: (納税義務者数, 課税対象所得[千円])}}。見つかった年度だけ"""
    out = {}
    this_year = time.localtime().tm_year
    for year in range(this_year, this_year - SOUMU_YEARS, -1):
        try:
            data = _get(SOUMU_URL.format(yy=year % 100))
        except urllib.error.HTTPError as e:
            if e.code == 404:    # まだ公表されていない・古くて形式が違う年度
                continue
            raise
        out[str(year)] = _income_year(data)
    if not out:
        raise RuntimeError("所得のデータが見つかりませんでした")
    return out


def _raw(spec, refresh):
    """取得元ごとの生データ（キャッシュ付き）"""
    kind = spec[0]
    name = f"dash_{spec[1]}.json.gz" if kind == "dash" else "income.json.gz"
    data = None if refresh else _read_cache(name, CACHE_DAYS * 86400)
    if data is None:
        try:
            data = _dashboard(spec[1]) if kind == "dash" else _income()
        except Exception:
            data = _read_cache(name)      # 取得できなければ古いキャッシュでも使う
            if data is None:
                raise
            return data
        _write_cache(name, data)
    return data


def _series(spec, refresh):
    if spec[0] == "dash":
        return _raw(spec, refresh)
    if spec[0] == "income":
        field = spec[1]
        out = {}
        for year, rows in _raw(spec, refresh).items():
            if field == "per":
                out[year] = {c: inc / payers / 10 for c, (payers, inc) in rows.items() if payers > 0}
            elif field == "total":
                out[year] = {c: inc / 1e5 for c, (payers, inc) in rows.items()}
            else:
                out[year] = {c: payers for c, (payers, inc) in rows.items()}
        return out
    # 比率：分子の各年に、その年以前で最も新しい分母の年を組み合わせる（調査の周期が違うため）
    _, num_spec, den_spec, scale = spec
    num, den = _series(num_spec, refresh), _series(den_spec, refresh)
    den_years = sorted(den)
    out = {}
    for year, values in num.items():
        dy = [y for y in den_years if y <= year]
        if not dy:
            continue
        d = den[dy[-1]]
        out[year] = {c: v / d[c] * scale for c, v in values.items() if d.get(c)}
    return out


def load(key, refresh=False):
    """指標の値 → {年: {市区町村コード: 値}}"""
    return _series(BY_KEY[key].spec, refresh)


# ---------------- 色分け ----------------
# 暗い背景の地図に重ねるため、値が小さいほど背景に近い暗い青、大きいほど明るい青にする
SEQ_COLORS = ["#184f95", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef", "#b7d3f6", "#e3effd"]
# 増減：減少は赤、増加は青、0 付近は灰色
DIV_COLORS = ["#e66767", "#a85352", "#6b4342", "#4a4a47", "#2f4a6b", "#4a7cc0", "#86b6ef"]
NO_DATA = "#3a3f4b"


def _quantile(sorted_values, q):
    i = q * (len(sorted_values) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (i - lo)


def classify(values, diverging):
    """値の一覧 → (区切り値の一覧, 色の一覧)。区切りは len(色)-1 個

    通常は分位数（各色にほぼ同じ数の市区町村が入る）で区切る。
    増減は 0 を中心に、絶対値の 90% 点を基準にした等間隔で区切る（真ん中の灰色が「ほぼ 0」）
    """
    vs = sorted(values)
    if not vs:
        return [], []
    if diverging:
        a = sorted(abs(v) for v in vs)
        m = _quantile(a, 0.9) or 1.0
        s = m / 2.5
        return [-2.5 * s, -1.5 * s, -0.5 * s, 0.5 * s, 1.5 * s, 2.5 * s], DIV_COLORS
    n = len(SEQ_COLORS)
    breaks = []
    for k in range(1, n):
        b = _quantile(vs, k / n)
        if not breaks or b > breaks[-1]:
            breaks.append(b)
    # 同じ値が多くて区切りが減ったときは、暗い色から明るい色まで間をあけて使う
    k = len(breaks) + 1
    if k == 1:
        return breaks, [SEQ_COLORS[n // 2]]
    return breaks, [SEQ_COLORS[round(i * (n - 1) / (k - 1))] for i in range(k)]


def color_of(value, breaks, colors):
    for b, c in zip(breaks, colors):
        if value < b:
            return c
    return colors[len(breaks)]

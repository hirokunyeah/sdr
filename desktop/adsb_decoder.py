"""
ADS-B（1090MHz Mode S 拡張スキッタ）の復調と解読。numpy だけで動く。

  Demodulator  … 2MS/s の IQ から、CRC が正しい DF17/18 メッセージ（112ビット）を取り出す
  decode()     … メッセージから便名・高度・速度・方位・位置(CPR)を読み取る
  Tracker      … 機体ごとに情報をまとめ、偶数/奇数の CPR から緯度経度を求める
"""
import math
import time

import numpy as np

FS = 2_000_000            # 1サンプル = 0.5µs（ADS-Bの1チップ）
CENTER = 1_090_000_000
BLOCK = 256_000           # 1回の読み込みサンプル数（0.128秒分。512バイト境界）
PREAMBLE = 16             # プリアンブル 8µs
MSG_BITS = 112
MSG_SAMPLES = PREAMBLE + 2 * MSG_BITS
QUIET = (1, 3, 4, 5, 6, 8, 10, 11, 12, 13, 14, 15)   # プリアンブル中のパルスがない位置
MIN_SNR = 2.5             # 雑音（中央値）の何倍以上のパルスを対象にするか

AIRCRAFT_TIMEOUT = 60     # これだけ受信がない機体は消す（秒）
TRAIL_MAX = 300           # 航跡として残す点の数
CPR_PAIR_SEC = 10         # 偶数/奇数の組で位置を出すときの最大時間差

CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


# ---------------- CRC（Mode S の 24ビットパリティ） ----------------
def _syndrome_matrix():
    """各ビットだけが1のメッセージのシンドローム。CRC は線形なので、任意のメッセージの
    シンドロームは「1のビットの行」の XOR（= 行列積 mod 2）で求められる"""
    gen = np.array([int(c) for c in "1111111111111010000001001"], dtype=np.uint8)
    m = np.zeros((MSG_BITS, 24), dtype=np.uint8)
    for i in range(MSG_BITS):
        msg = np.zeros(MSG_BITS, dtype=np.uint8)
        msg[i] = 1
        for k in range(MSG_BITS - 24):
            if msg[k]:
                msg[k:k + 25] ^= gen
        m[i] = msg[-24:]
    return m


SYN = _syndrome_matrix()
SYN_INT = SYN.astype(np.int32)
_W24 = (1 << np.arange(23, -1, -1)).astype(np.int64)
# 1ビットだけ誤ったときのシンドローム → 誤ったビット位置（DF5ビットとパリティは除く）
FIX1 = {int(SYN[i] @ _W24): i for i in range(5, MSG_BITS - 24)}


# ---------------- 復調 ----------------
class Demodulator:
    """RTL-SDR の生データ（uint8 IQ）を順に渡すと、正しいメッセージ（14バイトの bytes）を返す"""

    def __init__(self):
        self.tail = np.zeros(0, dtype=np.float32)
        self.known = set()     # CRC が一致して受信できた ICAO（誤り訂正はこれらの機体だけに使う）
        self.fixed = 0         # 1ビット訂正したメッセージ数

    def process(self, raw):
        iq = raw.astype(np.float32) - 127.5
        mag = np.hypot(iq[0::2], iq[1::2])
        m = np.concatenate((self.tail, mag))
        n = len(m) - MSG_SAMPLES
        if n <= 0:
            self.tail = m
            return []
        self.tail = m[n:]   # 末尾は次のブロックの先頭とつなげて探す

        p = [m[k:k + n] for k in range(10)]
        cand = ((p[0] > p[1]) & (p[1] < p[2]) & (p[2] > p[3]) & (p[3] < p[0]) & (p[4] < p[0]) &
                (p[5] < p[0]) & (p[6] < p[0]) & (p[7] > p[8]) & (p[8] < p[9]) & (p[9] > p[6]))
        idx = np.nonzero(cand)[0]
        if not len(idx):
            return []

        # パルスが十分強く、パルスのない位置が静かなものだけ残す
        high = (m[idx] + m[idx + 2] + m[idx + 7] + m[idx + 9]) / 4
        quiet = m[idx[:, None] + np.array(QUIET)].mean(axis=1)
        noise = float(np.median(mag)) + 1e-6
        idx = idx[(high > MIN_SNR * noise) & (quiet < high * 0.5)]
        if not len(idx):
            return []

        # PPM：各ビットの前半と後半の強さを比べる
        pos = idx[:, None] + PREAMBLE + 2 * np.arange(MSG_BITS)
        bits = (m[pos] > m[pos + 1]).astype(np.uint8)
        df = bits[:, :5] @ np.array([16, 8, 4, 2, 1])
        keep = (df == 17) | (df == 18)
        idx, bits = idx[keep], bits[keep]
        if not len(idx):
            return []

        syn = ((bits.astype(np.int32) @ SYN_INT) & 1) @ _W24
        out, last = [], -MSG_SAMPLES
        for i, s, b in zip(idx, syn, bits):
            if i < last + MSG_SAMPLES:      # 直前のメッセージと重なる位置は飛ばす
                continue
            fix = None
            if s:
                fix = FIX1.get(int(s))
                if fix is None:
                    continue
                b = b.copy()
                b[fix] ^= 1
            msg = np.packbits(b).tobytes()
            icao = msg[1:4]
            if fix is not None:
                if icao not in self.known:   # 未知の機体を誤り訂正で作り出さない
                    continue
                self.fixed += 1
            else:
                self.known.add(icao)
            out.append(msg)
            last = i
        return out


# ---------------- 解読 ----------------
def _bits(v, start, length):
    """112ビット整数 v の、先頭から start ビット目から length ビットを取り出す"""
    return (v >> (MSG_BITS - start - length)) & ((1 << length) - 1)


def decode(msg):
    """14バイトの DF17/18 メッセージを辞書にする。分からない種類は icao と tc だけ"""
    v = int.from_bytes(msg, "big")
    me = 32
    tc = _bits(v, me, 5)
    d = {"icao": msg[1:4].hex().upper(), "tc": tc}

    if 1 <= tc <= 4:                                    # 便名
        cs = "".join(CHARSET[_bits(v, me + 8 + 6 * k, 6)] for k in range(8))
        d["callsign"] = cs.replace("#", "").strip()

    elif 9 <= tc <= 18 or 20 <= tc <= 22:               # 飛行中の位置
        alt = _bits(v, me + 8, 12)
        if tc <= 18:
            if alt & 0x10:                              # Qビット=1 なら 25ft 単位
                n = ((alt >> 5) << 4) | (alt & 0xF)
                d["alt"] = n * 25 - 1000
        elif alt:
            d["alt"] = round(alt * 3.28084)             # GNSS高度（m）
        d["odd"] = _bits(v, me + 21, 1)
        d["lat_cpr"] = _bits(v, me + 22, 17) / 131072
        d["lon_cpr"] = _bits(v, me + 39, 17) / 131072

    elif tc == 19:                                      # 速度
        st = _bits(v, me + 5, 3)
        if st in (1, 2):                                # 対地速度（東西・南北成分）
            vew, vns = _bits(v, me + 14, 10), _bits(v, me + 25, 10)
            if vew and vns:
                k = 4 if st == 2 else 1
                vx = (vew - 1) * k * (-1 if _bits(v, me + 13, 1) else 1)
                vy = (vns - 1) * k * (-1 if _bits(v, me + 24, 1) else 1)
                d["speed"] = round(math.hypot(vx, vy))
                d["track"] = math.degrees(math.atan2(vx, vy)) % 360
        elif st in (3, 4):                              # 対気速度と機首方位
            if _bits(v, me + 13, 1):
                d["track"] = _bits(v, me + 14, 10) * 360 / 1024
            asp = _bits(v, me + 25, 10)
            if asp:
                d["speed"] = (asp - 1) * (4 if st == 4 else 1)
        vr = _bits(v, me + 37, 9)
        if vr:
            d["vrate"] = (vr - 1) * 64 * (-1 if _bits(v, me + 36, 1) else 1)
    return d


# ---------------- CPR（位置の圧縮形式）の復元 ----------------
def cpr_nl(lat):
    """緯度ごとの経度ゾーン数"""
    if lat == 0:
        return 59
    if abs(lat) == 87:
        return 2
    if abs(lat) > 87:
        return 1
    a = 1 - (1 - math.cos(math.pi / 30)) / math.cos(math.radians(lat)) ** 2
    return int(math.floor(2 * math.pi / math.acos(a)))


def cpr_global(even, odd, newest_odd):
    """偶数(even)と奇数(odd)の (lat_cpr, lon_cpr) の組から緯度経度を求める。失敗なら None"""
    (lat0, lon0), (lat1, lon1) = even, odd
    j = math.floor(59 * lat0 - 60 * lat1 + 0.5)
    rlat0 = 6.0 * (j % 60 + lat0)
    rlat1 = 360 / 59 * (j % 59 + lat1)
    rlat0 -= 360 if rlat0 >= 270 else 0
    rlat1 -= 360 if rlat1 >= 270 else 0
    if cpr_nl(rlat0) != cpr_nl(rlat1):     # 2つが別の経度ゾーンにまたがっている
        return None
    lat = rlat1 if newest_odd else rlat0
    nl = cpr_nl(lat)
    ni = max(nl - (1 if newest_odd else 0), 1)
    m = math.floor(lon0 * (nl - 1) - lon1 * nl + 0.5)
    lon = 360 / ni * (m % ni + (lon1 if newest_odd else lon0))
    lon -= 360 if lon >= 180 else 0
    return lat, lon


def cpr_local(ref_lat, ref_lon, lat_cpr, lon_cpr, odd):
    """近く（約300km以内）にあると分かっている基準位置から、1つのメッセージだけで位置を求める"""
    dlat = 360 / (60 - odd)
    j = math.floor(ref_lat / dlat) + math.floor(0.5 + (ref_lat % dlat) / dlat - lat_cpr)
    lat = dlat * (j + lat_cpr)
    dlon = 360 / max(cpr_nl(lat) - odd, 1)
    m = math.floor(ref_lon / dlon) + math.floor(0.5 + (ref_lon % dlon) / dlon - lon_cpr)
    return lat, dlon * (m + lon_cpr)


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, a)))


# ---------------- 機体の追跡 ----------------
class Tracker:
    def __init__(self):
        self.aircraft = {}   # ICAO → 情報の辞書
        self.messages = 0

    def update(self, msg, now=None):
        now = time.time() if now is None else now
        d = decode(msg)
        self.messages += 1
        a = self.aircraft.get(d["icao"])
        if a is None:
            a = self.aircraft[d["icao"]] = {
                "icao": d["icao"], "callsign": "", "alt": None, "speed": None, "track": None,
                "vrate": None, "lat": None, "lon": None, "msgs": 0, "seen": now, "seen_pos": None,
                "trail": [], "cpr": [None, None]}
        a["msgs"] += 1
        a["seen"] = now
        for k in ("callsign", "alt", "speed", "track", "vrate"):
            if k in d:
                a[k] = d[k]
        if "lat_cpr" in d:
            self._position(a, d, now)
        return a

    @staticmethod
    def _position(a, d, now):
        odd = d["odd"]
        a["cpr"][odd] = (d["lat_cpr"], d["lon_cpr"], now)
        pos = None
        if a["lat"] is not None and now - a["seen_pos"] < CPR_PAIR_SEC * 3:
            # 直前の位置が分かっていれば、それを基準に1メッセージで求める
            pos = cpr_local(a["lat"], a["lon"], d["lat_cpr"], d["lon_cpr"], odd)
            if distance_km(a["lat"], a["lon"], *pos) > 20:    # 不自然な飛びは捨てて組で求め直す
                a["lat"] = a["lon"] = None
                pos = None
        if pos is None:
            even, other = a["cpr"][0], a["cpr"][1]
            if even and other and abs(even[2] - other[2]) <= CPR_PAIR_SEC:
                pos = cpr_global(even[:2], other[:2], odd == 1)
        if pos is None:
            return
        a["lat"], a["lon"] = pos
        a["seen_pos"] = now
        trail = a["trail"]
        if not trail or trail[-1] != (round(pos[0], 5), round(pos[1], 5)):
            trail.append((round(pos[0], 5), round(pos[1], 5)))
            del trail[:-TRAIL_MAX]

    def prune(self, now=None):
        now = time.time() if now is None else now
        for icao in [k for k, a in self.aircraft.items() if now - a["seen"] > AIRCRAFT_TIMEOUT]:
            del self.aircraft[icao]

    def snapshot(self, now=None):
        """画面表示用のコピー"""
        now = time.time() if now is None else now
        out = []
        for a in self.aircraft.values():
            s = {k: v for k, v in a.items() if k != "cpr"}
            s["trail"] = list(a["trail"])
            s["age"] = now - a["seen"]
            s["pos_age"] = now - a["seen_pos"] if a["seen_pos"] else None
            out.append(s)
        return out

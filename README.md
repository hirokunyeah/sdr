# RTL-SDR V4 レシーバー

RTL-SDR Blog V4 で、FM放送・航空無線・アマチュア無線の受信と、
気象衛星 Meteor-M の画像受信、ADS-B による航空機の位置表示ができるアプリです。2つの版があります。

| 版 | 動かす場所 | 主な機能 | 入口 |
|---|---|---|---|
| **デスクトップ版** | Windows（推奨）/ Ubuntu / WSL / Mac | スペクトラム・ウォーターフォール、WFM/NFM/AM 復調、局サーチ、気象衛星受信、ADS-B（航空機の地図表示） | `desktop/sdr_app.py` |
| **Web版（サーバー）** | Raspberry Pi（Ubuntu）など | Pi で受信し、スマホやPCのブラウザで表示・音声再生 | `web/server.py` |

## フォルダ構成

```
sdr/
├── README.md                  … このファイル
├── setup_windows.bat          … Windows 用：archives のツールを展開する
├── docs/                      … マニュアル
│   ├── install-windows.md         Windows へのインストール
│   ├── install-linux.md           Ubuntu / WSL / Mac へのインストール
│   ├── install-raspberrypi.md     Raspberry Pi に Web版サーバーを入れる
│   ├── usage.md                   使い方（デスクトップ版・Web版・気象衛星・ADS-B）
│   └── troubleshooting.md         うまく動かないとき
├── desktop/                   … デスクトップ版アプリ
│   ├── sdr_app.py                 本体
│   ├── satellite.py               気象衛星ウィンドウ
│   ├── adsb.py                    ADS-B ウィンドウ（地図・機体一覧）
│   ├── adsb_decoder.py            ADS-B の復調・解読
│   ├── adsb_profile.py            ADS-B の断面図（機体同士の水平距離・高度差）
│   ├── adsb_net.py                ADS-B のインターネットのデータ取得（adsb.lol / adsb.fi）
│   ├── theme.py, icons/           画面のデザイン（ダークテーマ。Web版と同じ配色）
│   ├── requirements.txt
│   └── satellite_data/ など       実行時に作られるデータ・設定（※）
├── web/                       … Web版（Raspberry Pi 用サーバー）
│   ├── server.py
│   ├── static/index.html          ブラウザ画面
│   ├── requirements.txt
│   └── sdr-web.service            自動起動用 systemd 設定
└── tools/                     … ドライバ・ツール類
    ├── windows/
    │   ├── setup.ps1                  展開スクリプト本体（setup_windows.bat から呼ばれる）
    │   ├── archives/                  配布元のファイル（Git管理）
    │   │   ├── zadig-2.9.exe              USBドライバ(WinUSB)書き込みツール
    │   │   ├── rtl-sdr-blog-Release.zip   V4対応 rtlsdr.dll とコマンド類
    │   │   └── SatDump-Windows_x64_Portable.zip  気象衛星画像の変換ソフト
    │   ├── rtl-sdr-blog-x64/          ↓ setup_windows.bat で展開（Git管理外）
    │   └── SatDump/                   ↓ setup_windows.bat で展開（Git管理外）
    └── src/
        └── rtl-sdr-blog/              V4対応ドライバのソース（Linux / Mac でビルドする用）
```

※ `satellite_settings.json`（観測地点・SatDumpの場所）、`satellite_tle.txt`（軌道データ）、
`satellite_data/`（録音・受信画像）は、アプリが自動で作成・更新します。

## まずはここから

1. 自分の環境のインストール手順を読む
   - Windows → [docs/install-windows.md](docs/install-windows.md)
   - Ubuntu / WSL / Mac → [docs/install-linux.md](docs/install-linux.md)
   - Raspberry Pi（Web版）→ [docs/install-raspberrypi.md](docs/install-raspberrypi.md)
2. 使い方 → [docs/usage.md](docs/usage.md)
3. 困ったとき → [docs/troubleshooting.md](docs/troubleshooting.md)

### Windows の最短手順（概要）
```
1. setup_windows.bat を実行（rtlsdr.dll と SatDump を展開）
2. tools\windows\archives\zadig-2.9.exe で V4 に WinUSB ドライバを入れる
3. python -m venv .venv
4. .venv\Scripts\pip install -r desktop\requirements.txt
5. .venv\Scripts\python desktop\sdr_app.py
```
`rtlsdr.dll` は `tools\windows\rtl-sdr-blog-x64\` から自動で読み込まれるので、コピーは不要です。

## 注意
受信は自由ですが、電波法により放送以外の通信内容を他人に漏らしたり利用したりすることは禁止されています。

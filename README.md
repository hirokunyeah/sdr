# RTL-SDR V4 レシーバー

[RTL-SDR Blog V4](https://www.rtl-sdr.com/v4/) で、FM放送・航空無線・アマチュア無線の受信と、
気象衛星 Meteor-M の画像受信、ADS-B による航空機の位置表示ができる Windows 用デスクトップアプリです（Python / PySide6）。

## 主な機能

- スペクトラム・ウォーターフォール表示
- WFM / NFM / AM 復調、局サーチ
- 気象衛星 Meteor-M2-3 / M2-4（LRPT）の自動録音・画像化（SatDump を使用）
- ADS-B（1090MHz）で受信した航空機を地図・一覧・断面図で表示

## 動作環境

- Windows 10 / 11（64bit）
- Python 3.10 以降（64bit）
- RTL-SDR Blog V4（他の RTL-SDR でも動く可能性はありますが、V4 で確認しています）

## 取得方法

```
git clone https://github.com/hirokunyeah/sdr.git
```

GitHub の「Code → Download ZIP」でダウンロードして展開しても使えます。

## まずはここから

1. インストール → [docs/install-windows.md](docs/install-windows.md)
2. 使い方 → [docs/usage.md](docs/usage.md)
3. 困ったとき → [docs/troubleshooting.md](docs/troubleshooting.md)

### 最短手順（概要）
```
1. setup_windows.bat を実行（rtlsdr.dll と SatDump を展開）
2. tools\windows\archives\zadig-2.9.exe で V4 に WinUSB ドライバを入れる
3. python -m venv .venv
4. .venv\Scripts\pip install -r desktop\requirements.txt
5. .venv\Scripts\python desktop\sdr_app.py
```
`rtlsdr.dll` は `tools\windows\rtl-sdr-blog-x64\` から自動で読み込まれるので、コピーは不要です。

## フォルダ構成

```
sdr/
├── README.md                  … このファイル
├── LICENSE                    … ライセンス（MIT）
├── setup_windows.bat          … Windows 用：archives のツールを展開する
├── docs/                      … マニュアル
│   ├── install-windows.md         インストール
│   ├── usage.md                   使い方（基本操作・気象衛星・ADS-B）
│   └── troubleshooting.md         うまく動かないとき
├── desktop/                   … アプリ本体
│   ├── sdr_app.py                 メインウィンドウ（起動はこれ）
│   ├── satellite.py               気象衛星ウィンドウ
│   ├── adsb.py                    ADS-B ウィンドウ（地図・機体一覧）
│   ├── adsb_decoder.py            ADS-B の復調・解読
│   ├── adsb_profile.py            ADS-B の断面図（機体同士の水平距離・高度差）
│   ├── adsb_net.py                ADS-B のインターネットのデータ取得（adsb.lol / adsb.fi）
│   ├── theme.py, icons/           画面のデザイン（ダークテーマ）
│   ├── requirements.txt
│   └── satellite_data/ など       実行時に作られるデータ・設定（※）
└── tools/windows/             … ドライバ・ツール類
    ├── setup.ps1                  展開スクリプト本体（setup_windows.bat から呼ばれる）
    ├── archives/                  配布元のファイル
    │   ├── zadig-2.9.exe              USBドライバ(WinUSB)書き込みツール
    │   ├── rtl-sdr-blog-Release.zip   V4対応 rtlsdr.dll とコマンド類
    │   └── SatDump-Windows_x64_Portable.zip  気象衛星画像の変換ソフト
    ├── rtl-sdr-blog-x64/          ↓ setup_windows.bat で展開（Git管理外）
    └── SatDump/                   ↓ setup_windows.bat で展開（Git管理外）
```

※ `satellite_settings.json`（観測地点・SatDumpの場所）、`satellite_tle.txt`（軌道データ）、
`satellite_data/`（録音・受信画像）は、アプリが自動で作成・更新します（Git管理外）。

## ライセンス

このリポジトリのソースコード・ドキュメントは [MIT License](LICENSE) です。
`tools/windows/archives/` の同梱ソフトウェアは対象外で、それぞれのライセンスに従います（下表）。

## 同梱しているサードパーティ製ソフトウェア

`tools/windows/archives/` のファイルは、それぞれの配布元のものをそのまま同梱しています。
ライセンスは各ソフトウェアに従います。

| ソフトウェア | 配布元 | ライセンス |
|---|---|---|
| RTL-SDR Blog ドライバ（`rtl-sdr-blog-Release.zip`） | https://github.com/rtlsdrblog/rtl-sdr-blog | GPL-2.0 |
| SatDump（`SatDump-Windows_x64_Portable.zip`） | https://github.com/SatDump/SatDump | GPL-3.0 |
| Zadig（`zadig-2.9.exe`） | https://zadig.akeo.ie/ | GPL-3.0 |

ADS-B の地図には [Leaflet](https://leafletjs.com/) と国土地理院の[地理院タイル](https://maps.gsi.go.jp/development/ichiran.html)を、
インターネットの機体情報には [adsb.lol](https://adsb.lol/) / [adsb.fi](https://adsb.fi/) の API を使っています。

## 注意

- 受信は自由ですが、電波法により放送以外の通信内容を他人に漏らしたり利用したりすることは禁止されています。
- 本ソフトウェアは無保証です。使用によって生じたいかなる損害についても作者は責任を負いません。

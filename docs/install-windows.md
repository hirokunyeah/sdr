# Windows へのインストール（デスクトップ版）

必要なドライバ・ツールは `tools\windows\archives\` に入っています。
以下、コマンドはこのフォルダ（`sdr`）で PowerShell またはコマンドプロンプトを開いて実行します。

## 1. Python を用意
Python 3.10 以降（64bit）をインストールします（https://www.python.org/ ）。
インストーラーでは「Add python.exe to PATH」にチェックを入れてください。

## 2. ツールを展開する（setup_windows.bat）
`sdr` フォルダの **`setup_windows.bat`** をダブルクリックします（またはコマンドで `setup_windows.bat`）。
`tools\windows\archives\` の zip が次のように展開されます。

| 展開元（archives） | 展開先 | 内容 |
|---|---|---|
| `rtl-sdr-blog-Release.zip` の `x64` | `tools\windows\rtl-sdr-blog-x64\` | V4対応 `rtlsdr.dll`、`rtl_test.exe` などのコマンド |
| `SatDump-Windows_x64_Portable.zip` | `tools\windows\SatDump\` | 気象衛星画像の変換ソフト |

- 展開済みのものはスキップします。展開し直すときは `setup_windows.bat -Force`
- 展開したフォルダは Git には含めません（`.gitignore` で除外。clone した直後は毎回この手順が必要です）
- アプリ（`sdr_app.py`）の起動中は `rtlsdr.dll` が使用中になり展開し直せないので、アプリを閉じてから実行してください

## 3. USBドライバを書き込む（Zadig）
1. V4 を USB に挿す（USBハブではなくPC本体に直接挿すのがおすすめ）
2. `tools\windows\archives\zadig-2.9.exe` を起動（管理者権限の確認が出たら「はい」）
3. メニュー **Options → List All Devices** にチェック
4. 一覧から **「Bulk-In, Interface (Interface 0)」** を選ぶ
5. 右側のドライバを **WinUSB** にして **Install Driver**（または Replace Driver）

> 別のUSBポートに挿し替えた場合、そのポートで再度この手順が必要になることがあります。

## 4. 動作確認（任意）
```
tools\windows\rtl-sdr-blog-x64\rtl_test.exe -t
```
「Found Rafael Micro R828D tuner」「RTL-SDR Blog V4 Detected」と出ればOKです。

- 「VCRUNTIME140.dll が見つからない」と出る場合は、Microsoft の
  「Visual C++ 再頒布可能パッケージ（x64）」をインストールしてください
- 現在の rtl-sdr-blog 版は libusb を内蔵しているため、`libusb-1.0.dll` は不要です

## 5. Python ライブラリを入れる
```
python -m venv .venv
.venv\Scripts\pip install -r desktop\requirements.txt
```

## 6. 起動
```
.venv\Scripts\python desktop\sdr_app.py
```
`rtlsdr.dll` は `tools\windows\rtl-sdr-blog-x64\`（または `desktop\`）から自動で読み込まれます。

## 7. 気象衛星を受信する場合：SatDump
手順2で `tools\windows\SatDump\` に展開済みなので、追加のインストールは不要です。
気象衛星ウィンドウを開くと `tools\windows\SatDump\satdump.exe` が自動で設定されます。

- 別の SatDump を使いたい場合は「参照…」で `satdump.exe` を指定します。指定先が見つからなくなると、同梱版に自動で戻ります
- 最新版は https://www.satdump.org/ から入手できます

---

## 参考：archives の中身と入手元
| ファイル | 入手元 |
|---|---|
| `zadig-2.9.exe` | https://zadig.akeo.ie/ |
| `rtl-sdr-blog-Release.zip` | https://github.com/rtlsdrblog/rtl-sdr-blog/releases |
| `SatDump-Windows_x64_Portable.zip` | https://www.satdump.org/ |

新しい版に差し替える場合は、同じファイル名で `archives\` に置いて `setup_windows.bat -Force` を実行します。
32bit 版 Windows の場合は `rtl-sdr-blog-Release.zip` 内の `x86` フォルダを手動で展開して使います。

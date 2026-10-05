# Ubuntu / WSL / Mac へのインストール（デスクトップ版）

V4 は古い `librtlsdr`（apt や brew の標準パッケージ）では正しく動かないことがあるため、
**RTL-SDR Blog 版ドライバをソースからビルド**して入れます。
ソースは `tools/src/rtl-sdr-blog/` に同梱しています（最新版は `git clone https://github.com/rtlsdrblog/rtl-sdr-blog` でも取得できます）。

以下、コマンドはこのフォルダ（`sdr`）で実行します。

## Ubuntu

### 1. ドライバのビルドとインストール
```
sudo apt update
sudo apt purge -y '^librtlsdr' rtl-sdr        # 古いドライバが入っていれば削除
sudo apt install -y git cmake build-essential pkg-config libusb-1.0-0-dev \
                    python3-venv libportaudio2 libxcb-cursor0
cd tools/src/rtl-sdr-blog
mkdir -p build && cd build
cmake .. -DINSTALL_UDEV_RULES=ON
make -j4 && sudo make install
sudo cp ../rtl-sdr.rules /etc/udev/rules.d/
sudo ldconfig
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo reboot
```
再起動後、V4 を挿して `rtl_test -t` を実行し、「R828D tuner」と表示されればOKです（Ctrl+C で終了）。
`libportaudio2` は音声出力、`libxcb-cursor0` は画面表示（Qt）に必要です。

### 2. Python ライブラリを入れて起動
Ubuntu ではシステムの pip が使えないため、仮想環境を作ります。
```
python3 -m venv venv
venv/bin/pip install -r desktop/requirements.txt
venv/bin/python desktop/sdr_app.py
```

## WSL（Windows 11 の WSL2 + Ubuntu）
WSL からは USB 機器が直接見えないため、**usbipd-win** で V4 を WSL に接続します。
画面と音声は Windows 11 標準の WSLg で表示・再生されます
（Windows 10 では画面が出ないため、[Web版](install-raspberrypi.md#wsl-や-ubuntu-pc-で-web版を動かす) をおすすめします）。

### ① Windows 側：V4 を WSL につなぐ（PowerShell を管理者として開く）
```
winget install usbipd
usbipd list                                   # 「RTL2838UHIDIR」などの行の BUSID を確認（例：2-3）
usbipd bind --busid 2-3                       # 初回のみ
usbipd attach --wsl --busid 2-3 --auto-attach # WSL起動中に実行。挿し直しても自動で再接続
```
`--auto-attach` を付けた場合、PowerShell の画面は閉じずにそのままにしておきます。

> Zadig で WinUSB を入れた状態でも usbipd で WSL に渡せます。

### ② WSL 側：ドライバを入れる
上の「Ubuntu」の手順 1 を、`blacklist` と `sudo reboot` の2行を除いて実行します
（WSL のカーネルには DVB ドライバが含まれていないため不要です）。
そのうえで、WSL の最小構成に足りない画面・音声・日本語フォント用のパッケージを追加します。
```
sudo apt install -y usbutils libasound2-plugins fonts-noto-cjk \
  libgl1 libegl1 libdbus-1-3 libfontconfig1 libxkbcommon-x11-0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
  libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1
printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > ~/.asoundrc
sudo service udev restart
```
`.asoundrc` は、音声を WSLg の PulseAudio 経由で Windows のスピーカーへ出すための設定です。
`lsusb` に RTL2838 が表示され、`rtl_test -t` で「R828D tuner」と出ればOKです。

### ③ 起動
「Ubuntu」の手順 2 と同じです。

> 注意：Windows 側で作った `.venv`（`Scripts\` を持つもの）は WSL では使えません。
> WSL では上記のとおり別名の `venv` を作ってください。

## Mac
ターミナルで実行します（Homebrew が必要）。
```
brew uninstall librtlsdr   # 入っている場合のみ（古い版は V4 非対応のことがある）
brew install cmake libusb pkg-config portaudio
cd tools/src/rtl-sdr-blog
mkdir -p build && cd build
cmake .. && make && sudo make install
cd ../../../..
python3 -m venv venv
venv/bin/pip install -r desktop/requirements.txt
venv/bin/python desktop/sdr_app.py
```

## 気象衛星を受信する場合：SatDump
- Ubuntu：https://www.satdump.org/ の手順でインストール（`/usr/bin/satdump` か `/usr/local/bin/satdump` なら自動検出）
- Mac：`SatDump.app` を「アプリケーション」に入れれば自動検出
- 見つからない場合は、気象衛星ウィンドウの「参照…」で実行ファイルを指定します

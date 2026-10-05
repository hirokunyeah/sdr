# Raspberry Pi に Web版サーバーを入れる

Raspberry Pi（Ubuntu Server / Raspberry Pi OS）で受信・復調し、
スペクトラムと音声をブラウザへ配信します。同じネットワークのスマホ・PCから操作できます。

- 推奨：Raspberry Pi 4 以上（負荷を考えてサンプルレートは 1.2 MS/s にしています）
- 対応モード：WFM / NFM / AM（気象衛星受信はデスクトップ版のみ）

以下はユーザー名 `ubuntu`、配置先 `/home/ubuntu/sdr` の例です。自分の環境に合わせて読み替えてください。

## 1. ファイルを Pi にコピー
PC からこのフォルダの `web/` と `tools/src/` を Pi へコピーします（Windows の PowerShell でも使えます）。
```
ssh ubuntu@<PiのIP> "mkdir -p ~/sdr/tools"
scp -r web ubuntu@<PiのIP>:~/sdr/
scp -r tools/src ubuntu@<PiのIP>:~/sdr/tools/
```
以降は Pi に ssh でログインして作業します。

## 2. ドライバのビルドとインストール
```
sudo apt update
sudo apt purge -y '^librtlsdr' rtl-sdr        # 古いドライバが入っていれば削除
sudo apt install -y git cmake build-essential pkg-config libusb-1.0-0-dev python3-venv
cd ~/sdr/tools/src/rtl-sdr-blog
rm -rf build && mkdir build && cd build
cmake .. -DINSTALL_UDEV_RULES=ON
make -j4 && sudo make install
sudo cp ../rtl-sdr.rules /etc/udev/rules.d/
sudo ldconfig
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo reboot
```
再起動後、V4 を挿して `rtl_test -t` で「R828D tuner」と出ればOKです（Ctrl+C で終了）。

## 3. Python 環境を作って起動
```
cd ~/sdr/web
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python server.py
```
PC やスマホのブラウザで `http://<PiのIP>:8080/` を開きます。

| オプション | 意味 |
|---|---|
| `--port 8080` | 待ち受けポート（初期値 8080） |
| `--host 0.0.0.0` | 待ち受けアドレス（初期値はすべて） |
| `--demo` | SDR なしで疑似信号を流す（画面の動作確認用） |

## 4. 電源投入時に自動起動（systemd）
`sdr-web.service` の `User` と各パスが自分の環境と合っているか確認してから登録します。
```
sudo cp ~/sdr/web/sdr-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sdr-web
systemctl status sdr-web          # 状態確認
journalctl -u sdr-web -f          # ログを見る
```
設定ファイルを書き換えたら `sudo systemctl daemon-reload && sudo systemctl restart sdr-web` を実行します。

> ブラウザが1つも接続していないときは SDR を自動で止めるので、常時起動でも負荷はかかりません。

## WSL や Ubuntu PC で Web版を動かす
Raspberry Pi 以外でも同じ手順で動きます。WSL の場合は
[install-linux.md の WSL 手順](install-linux.md#wsl-windows-11-の-wsl2--ubuntu) ①② で V4 を接続・ドライバを入れたあと、
```
cd web
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python server.py
```
を実行し、Windows のブラウザで `http://localhost:8080/` を開きます。画面・音声の追加パッケージは不要です。

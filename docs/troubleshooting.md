# うまく動かないとき

## 共通
| 症状 | 対処 |
|---|---|
| 「ドライバが見つかりません」 | Windows：`setup_windows.bat` を実行し、`tools\windows\rtl-sdr-blog-x64\rtlsdr.dll` ができたか確認。Linux / Mac：ドライバのビルド・`sudo ldconfig` を確認 |
| `rtlsdr_set_dithering` が見つからない | `pip install pyrtlsdr==0.2.93` を入れ直す |
| 音が出ない（デスクトップ版） | 「音声出力」で実際に使っているスピーカー／ヘッドホンを選ぶ（OSの既定がモニターのHDMI音声などになっていることがある）。Windows の「音量ミキサー」で python がミュートになっていないかも確認 |
| 音が出ない（Web版） | 「🔊 音声オン」を押したか確認。iPhone はマナーモード（消音スイッチ）だと鳴らない |
| 音が途切れる | 他の重いアプリを閉じる。USBハブではなくPC本体に直接挿す |
| 雑音ばかり・信号が潰れる | ゲインを 25〜40dB 付近に固定して調整する |

## Windows
| 症状 | 対処 |
|---|---|
| デバイスを開けない | `tools\windows\archives\zadig-2.9.exe` で WinUSB を入れ直す（別のUSBポートに挿した場合も） |
| `setup_windows.bat` で「別のプロセスで使用されている」 | `sdr_app.py` や `rtl_test.exe` を閉じてから再実行 |
| `setup_windows.bat` が途中で閉じる・スクリプトの実行が禁止される | PowerShell で `powershell -ExecutionPolicy Bypass -File tools\windows\setup.ps1` を実行 |
| 「VCRUNTIME140.dll が見つからない」 | Visual C++ 再頒布可能パッケージ（x64）をインストール |

## Ubuntu / Raspberry Pi
| 症状 | 対処 |
|---|---|
| `usb_claim_interface error -6` | DVBドライバが掴んでいる。blacklist を設定して再起動 |
| `usb_open error -3` | 権限不足。`sudo rtl_test -t` で動くなら udev ルールを確認し、V4を挿し直す |
| Web版の画面が出ない | `systemctl status sdr-web` / `journalctl -u sdr-web -f` でエラーを確認。ファイアウォールで 8080 番を開ける |

## WSL
| 症状 | 対処 |
|---|---|
| `No supported devices found` | Windows 側で `usbipd attach` をやり直す（WSL を再起動すると接続が外れます） |
| 音が出ない・途切れる | `~/.asoundrc` を確認。USB 経由の転送が詰まる場合は、Web版を Windows のブラウザで使う方法も試す |
| 日本語が□になる | `sudo apt install fonts-noto-cjk` |

## ADS-B
- 機体が1機も出ない：アンテナを縦向き・約6.5cm にして窓際へ。ゲインは最大（49.6dB）から試し、
  近くの強い電波で飽和する場合は 40dB 前後に下げます。空港や航路から遠いと機体が少ないこともあります
- 一覧には出るが地図に出ない：位置メッセージを受信できていません。しばらく待つか、受信環境を改善してください
- 地図が真っ黒・「地図を読み込めませんでした」：インターネット接続を確認してください（一覧は地図なしでも使えます）
- 地図が真っ黒・「地図の表示処理が異常終了しました」（Windows）：`\\wsl.localhost\...` などのネットワークパスから起動すると、
  地図の表示処理（QtWebEngineProcess）が Chromium のサンドボックスで起動できません。ネットワークパスから起動したときは
  自動でサンドボックスを無効にしますが、ネットワークドライブ（Z: など）に割り当てて起動している場合は、
  環境変数 `QTWEBENGINE_DISABLE_SANDBOX=1` を設定して起動するか、アプリを C: などのローカルドライブに置いてください
- 地図の一部が黒い四角に抜ける・ブロック状のノイズが出る：GPU でタイルを描く処理の不具合です。
  アプリは「描くのは CPU、画面への合成は GPU」（`--disable-gpu-rasterization`）に設定してあります。
  それでも出る場合は、環境変数 `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu` を設定して起動すると GPU を全く使わなくなります（動きは遅くなります）
- 地図の動きが遅い：環境変数 `QTWEBENGINE_CHROMIUM_FLAGS` を自分で設定していると、アプリの設定が使われません。外して起動してください
- 「地図を表示できません（QtWebEngine が使えません）」：`pip install -r desktop/requirements.txt` で PySide6 を入れ直す。
  Linux では `sudo apt install libnss3 libxkbfile1` などのライブラリが必要なことがあります
- メイン画面の受信が開始できない：ADS-B ウィンドウで「■ 停止」を押してください
- 「ネット（…）：取得失敗」：インターネット接続を確認するか、取得先を切り替えてください。HTTP 429 はアクセスが多すぎるという意味で、自動で間隔を延ばして再試行します。サービス側で API キーが必要になった場合は取得できなくなります

## 気象衛星
- 最大仰角が高い（40°以上）パスほど成功しやすいです。まずは高いパスで試してください
- ゲインは「自動」ではなく 35〜45dB 程度に固定するのがおすすめです
- 衛星の送信周波数が 137.1MHz に切り替わることがあります。受信できない日は周波数欄を変更してください
- SatDump が見つからない：Windows は `setup_windows.bat` を実行して `tools\windows\SatDump\satdump.exe` ができたか確認。別の場所の SatDump は「参照…」で指定
- SatDump のバージョンによりコマンドの書き方が変わることがあります。ログのエラーを確認してください

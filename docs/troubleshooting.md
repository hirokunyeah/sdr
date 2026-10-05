# うまく動かないとき

## 共通
| 症状 | 対処 |
|---|---|
| 「ドライバが見つかりません」 | Windows：`setup_windows.bat` を実行し、`tools\windows\rtl-sdr-blog-x64\rtlsdr.dll` ができたか確認。Linux / Mac：ドライバのビルド・`sudo ldconfig` を確認 |
| `rtlsdr_set_dithering` が見つからない | `pip install pyrtlsdr==0.2.93` を入れ直す |
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

## 気象衛星
- 最大仰角が高い（40°以上）パスほど成功しやすいです。まずは高いパスで試してください
- ゲインは「自動」ではなく 35〜45dB 程度に固定するのがおすすめです
- 衛星の送信周波数が 137.1MHz に切り替わることがあります。受信できない日は周波数欄を変更してください
- SatDump が見つからない：Windows は `setup_windows.bat` を実行して `tools\windows\SatDump\satdump.exe` ができたか確認。別の場所の SatDump は「参照…」で指定
- SatDump のバージョンによりコマンドの書き方が変わることがあります。ログのエラーを確認してください

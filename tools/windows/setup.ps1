<#
  tools\windows\archives の配布ファイルを展開する（Windows 用）
    archives\rtl-sdr-blog-Release.zip          -> rtl-sdr-blog-x64\  （x64 フォルダのみ）
    archives\SatDump-Windows_x64_Portable.zip  -> SatDump\

  すでに展開済みのものはスキップする。-Force で消して展開し直す。
  ルートの setup_windows.bat から呼ばれる。
#>
param([switch]$Force)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$here = $PSScriptRoot
$archives = Join-Path $here "archives"

function Expand-To($zip, $dest, $sub) {
    $zipPath = Join-Path $archives $zip
    $destPath = Join-Path $here $dest
    if (-not (Test-Path $zipPath)) {
        Write-Host "[!] $zip が見つかりません" -ForegroundColor Red
        return $false
    }
    if (Test-Path $destPath) {
        if (-not $Force) {
            Write-Host "[skip] $dest は展開済みです（やり直す場合は -Force）"
            return $true
        }
        Remove-Item $destPath -Recurse -Force
    }
    Write-Host "[..] $zip を $dest に展開しています"
    try {
        # zip から展開先へ直接書き出す（$sub を指定した場合はそのフォルダの中身だけ）。
        # 一時フォルダ経由の移動は WSL 上のフォルダやウイルス対策ソフトのロックで失敗しやすいので使わない
        $prefix = if ($sub) { "$sub/" } else { "" }
        $zipFile = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
        try {
            foreach ($e in $zipFile.Entries) {
                $name = $e.FullName -replace '\\', '/'
                if (-not $name.StartsWith($prefix) -or $name.EndsWith("/")) { continue }
                $out = Join-Path $destPath $name.Substring($prefix.Length)
                New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $out, $true)
            }
        } finally {
            $zipFile.Dispose()
        }
        Get-ChildItem $destPath -Recurse -File | Unblock-File
    } catch {
        Write-Host "[!] $dest の展開に失敗しました: $_" -ForegroundColor Red
        Remove-Item $destPath -Recurse -Force -ErrorAction SilentlyContinue  # 中途半端に残さない
        return $false
    }
    Write-Host "[ok] $dest" -ForegroundColor Green
    return $true
}

$ok = $true
$ok = (Expand-To "rtl-sdr-blog-Release.zip" "rtl-sdr-blog-x64" "x64") -and $ok
$ok = (Expand-To "SatDump-Windows_x64_Portable.zip" "SatDump" $null) -and $ok

Write-Host ""
if ($ok) {
    Write-Host "完了しました。"
    Write-Host "  USBドライバ: tools\windows\archives\zadig-2.9.exe"
    Write-Host "  動作確認  : tools\windows\rtl-sdr-blog-x64\rtl_test.exe -t"
} else {
    exit 1
}

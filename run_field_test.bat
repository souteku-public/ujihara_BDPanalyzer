@echo off
rem ============================================================
rem  BDP Analyzer - 出先PC インターネット速度測定 (Python不要版)
rem  使い方: python.exe (ポータブル版) と bdp_analyzer フォルダを
rem          この .bat と同じ場所に置いてダブルクリック。
rem  受信側PC不要・DHCP可・追加インストール不要。
rem  結果は sat_speed.csv に保存され、別PCのCSVビューアで見られます。
rem ============================================================
cd /d "%~dp0"

rem 同フォルダの python.exe を優先。無ければ PATH の python を使う。
set PY=python
if exist "%~dp0python.exe" set PY="%~dp0python.exe"

echo 測定を開始します（上り/下り各30秒を60秒ごとに繰り返し）。
echo 停止するには、このウィンドウで Ctrl+C を押してください。
echo.

%PY% -m bdp_analyzer inettest --seconds 30 --streams 4 --loop --interval 60 --csv sat_speed.csv

echo.
echo 終了しました。結果は sat_speed.csv に保存されています。
pause

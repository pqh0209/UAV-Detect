# Đóng gói app thành 1 file exe: dist\uav_viewer.exe  (chạy:  powershell -ExecutionPolicy Bypass -File build_exe.ps1)
# Giữ đúng đường dẫn dist\uav_viewer.exe: luật Windows Firewall đã Allow gắn với đường dẫn này.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$work = Join-Path $env:TEMP "uav_viewer_build"
python -m pip install --quiet pyinstaller
# Build ra thư mục tạm trước: exe đang chạy thì file trong dist\ bị khoá
python -m PyInstaller --noconfirm --onefile --console --name uav_viewer --collect-all av `
    --workpath "$work\build" --specpath $work --distpath "$work\dist" "$root\viewer\uav_viewer.py"
New-Item -ItemType Directory -Force "$root\dist" | Out-Null
try {
    Copy-Item "$work\dist\uav_viewer.exe" "$root\dist\uav_viewer.exe" -Force
    Write-Host "Xong: $root\dist\uav_viewer.exe"
} catch {
    Write-Host "Không chép được (app đang mở?). Tắt app rồi chép tay: $work\dist\uav_viewer.exe"
}

# 启动大肥鱼竖屏直播窗口并调成 9:16 无黑边（去标题栏、去边框）
Add-Type -AssemblyName System.Windows.Forms
$chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { $chrome = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe" }
if (-not (Test-Path $chrome)) { Write-Host "Chrome not found"; exit 1 }

# 目标画面高度（物理像素）。0 = 自动：按屏幕高度取最大 9:16（本屏 1440 高 → 810x1440）。
# 实测：窗口高度会被系统钳制在屏幕内，无法超出屏幕，所以 1080x1920 开不出来；
# 屏幕只有 1440 高时，810x1440 就是能拿到的最大原生 9:16 画面。
# 想手动指定就填高度值（如 1200），宽度会按 9:16 自动算。
$TargetH = 0

Start-Process $chrome -ArgumentList @(
  '--app=http://127.0.0.1:18763/?view=live',
  '--window-size=640,1160',
  # 系统 DPI 是 125%：不加这条，Chrome 的 CSS 视口 = 物理像素/1.25，
  # 画面先被 DPI 砍一刀、再被直播画布放大，小字必糊。强制 1:1 让画面按物理像素渲染。
  '--force-device-scale-factor=1',
  "--user-data-dir=$PSScriptRoot\..\.runtime\chrome-live-916",
  '--autoplay-policy=no-user-gesture-required',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  # AudioServiceOutOfProcess 必须关掉：Chrome 默认把音频丢给 audio.mojom.AudioService 子进程去渲染，
  # 直播伴侣的「应用音频」是按浏览器主进程（窗口所属 PID）抓的，抓不到子进程的声音 → 语音一路没电平，
  # 音频闪避的触发源永远不响。关掉后音频回到主进程，采集和闪避才能生效。
  '--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,AudioServiceOutOfProcess',
  '--no-first-run'
)
Start-Sleep -Seconds 7

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W916 {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int w, int hh, uint f);
}
'@

$found = [IntPtr]::Zero
$cb = [W916+EnumWindowsProc]{ param($h, $l)
  $sb = New-Object System.Text.StringBuilder 256
  [W916]::GetWindowText($h, $sb, 256) | Out-Null
  # 标题由 live_view.js 写成「大肥鱼 · 直播间」，老版本是「肥波 · 直播间」；按关键字找最稳
  if ($sb.ToString() -like '*直播间*' -and [W916]::IsWindowVisible($h)) { $script:found = $h }
  return $true
}
[W916]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null

if ($found -eq [IntPtr]::Zero) { Write-Host '未找到直播窗口（页面没开成功？）'; exit 1 }

# 去标题栏和边框 → 纯画面（WS_POPUP|WS_VISIBLE|WS_CLIPSIBLINGS|WS_CLIPCHILDREN）
[W916]::SetWindowLong($found, -16, (0x80000000 -bor 0x10000000 -bor 0x04000000 -bor 0x02000000)) | Out-Null
[W916]::SetWindowPos($found, [IntPtr]::Zero, 0, 0, 0, 0, 0x0024) | Out-Null   # FRAMECHANGED
Start-Sleep -Milliseconds 600

# 定位用物理分辨率：CIM 返回物理像素，不受 DPI 缩放影响；WinForms 的 WorkingArea 会受缩放干扰。
$vc = Get-CimInstance Win32_VideoController | Where-Object { $_.CurrentHorizontalResolution } | Select-Object -First 1
$screenW = [int]$vc.CurrentHorizontalResolution
$screenH = [int]$vc.CurrentVerticalResolution
if ($screenW -lt 1) { $screenW = 2560 }
if ($screenH -lt 1) { $screenH = 1440 }

# 按屏幕高度取最大 9:16。实测窗口高度会被系统钳制在屏幕内，超出屏幕无效，所以只能以屏高为准。
if ($TargetH -le 0) { $ch = $screenH } else { $ch = [int][math]::Min($TargetH, $screenH) }
$cw = [int][math]::Round($ch * 9 / 16)
$x = [math]::Max(0, $screenW - $cw - 20)
$y = 0
[W916]::SetWindowPos($found, [IntPtr]::Zero, $x, $y, $cw, $ch, 0x0004) | Out-Null
Start-Sleep -Milliseconds 500
# Chrome 在去掉标题栏后可能自行重算边框，再设一次确保尺寸稳定
[W916]::SetWindowPos($found, [IntPtr]::Zero, $x, $y, $cw, $ch, 0x0004) | Out-Null
Write-Host "OK 直播窗口已就绪（$cw x $ch 物理像素，9:16 无黑边，devicePixelRatio=1）"
Write-Host '在直播伴侣/直播精灵里添加「窗口采集」→ 选「肥波 · 直播间」。'
Write-Host "画布设 1080x1920 时画面约放大 $([math]::Round(1080 / $cw, 2)) 倍；要像素级 1:1 就把画布设成 ${cw}x${ch}。"

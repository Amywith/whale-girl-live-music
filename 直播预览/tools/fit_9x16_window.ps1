# 启动大肥鱼竖屏直播窗口并调成 9:16 无黑边（去标题栏、去边框）
Add-Type -AssemblyName System.Windows.Forms
$chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { $chrome = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe" }
if (-not (Test-Path $chrome)) { Write-Host "Chrome not found"; exit 1 }

Start-Process $chrome -ArgumentList @(
  '--app=http://127.0.0.1:18763/?view=live',
  '--window-size=640,1160',
  "--user-data-dir=$PSScriptRoot\..\.runtime\chrome-live-916",
  '--autoplay-policy=no-user-gesture-required',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  '--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling',
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
  if ($sb.ToString() -eq '肥波 · 直播间' -and [W916]::IsWindowVisible($h)) { $script:found = $h }
  return $true
}
[W916]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null

if ($found -eq [IntPtr]::Zero) { Write-Host '未找到直播窗口（页面没开成功？）'; exit 1 }

# 去标题栏和边框 → 纯画面（WS_POPUP|WS_VISIBLE|WS_CLIPSIBLINGS|WS_CLIPCHILDREN）
[W916]::SetWindowLong($found, -16, (0x80000000 -bor 0x10000000 -bor 0x04000000 -bor 0x02000000)) | Out-Null
[W916]::SetWindowPos($found, [IntPtr]::Zero, 0, 0, 0, 0, 0x0024) | Out-Null   # FRAMECHANGED
Start-Sleep -Milliseconds 600

# 适配屏幕高：目标 9:16
$scr = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$ch = [math]::Min(1344, $scr.Height - 60)
$cw = [math]::Round($ch * 9 / 16)
[W916]::SetWindowPos($found, [IntPtr]::Zero, ($scr.Right - $cw - 30), 20, $cw, $ch, 0x0004) | Out-Null
Write-Host "OK 直播窗口已就绪（$cw x $ch，9:16 无黑边）"
Write-Host '在直播伴侣里添加「窗口采集」→ 选「肥波 · 直播间」'

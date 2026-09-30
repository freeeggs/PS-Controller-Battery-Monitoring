# ============================================================
#  构建 PSBatteryTray 单文件 exe
#  用法：在项目根目录执行
#      powershell -ExecutionPolicy Bypass -File build\build.ps1
#  或直接双击 build\build.bat
# ============================================================

param(
    [string]$Python = "",
    [switch]$SkipTests,
    [switch]$Onedir
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

# ---- 1. 定位 Python ----
if (-not $Python) {
    $candidates = @(
        (Join-Path $Root ".venv\Scripts\python.exe"),
        "$env:USERPROFILE\.workbuddy\binaries\python\envs\psbt\Scripts\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $Python = $c; break }
    }
}
if (-not $Python -or -not (Test-Path $Python)) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $Python) {
    throw "未找到 Python。请先安装 Python 3.9+，或用 -Python 指定解释器路径。"
}
Write-Step "使用解释器：$Python"
& $Python -c "import sys; print(sys.version)"

# ---- 2. 依赖 ----
Write-Step "安装 / 校验构建依赖"
& $Python -m pip install --disable-pip-version-check --quiet -r requirements.txt

# ---- 3. 生成图标 ----
Write-Step "生成程序图标 assets\app.ico"
& $Python tools\make_icons.py

# ---- 4. 单元测试 ----
if (-not $SkipTests) {
    Write-Step "运行单元测试"
    & $Python tests\run_tests.py
    if ($LASTEXITCODE -ne 0) { throw "单元测试未通过，已中止打包。" }
}

# ---- 5. 清理 ----
Write-Step "清理上次构建产物"
foreach ($d in @("build\work", "dist")) {
    if (Test-Path $d) { Remove-Item $d -Recurse -Force }
}

# ---- 6. 打包 ----
Write-Step "PyInstaller 打包（单文件、无控制台）"
$extra = @()
if ($Onedir) { $extra += "--onedir" }
& $Python -m PyInstaller --clean --noconfirm `
    --distpath dist --workpath build\work `
    @extra build\psbattery.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }

# ---- 7. 发布到 Release ----
Write-Step "整理 Release 目录"
New-Item -ItemType Directory -Force -Path "Release" | Out-Null
Copy-Item "dist\PSBatteryTray.exe" "Release\PSBatteryTray.exe" -Force
Copy-Item "README.md" "Release\README.md" -Force
if (Test-Path "docs\TECHNICAL_FEASIBILITY.md") {
    Copy-Item "docs\TECHNICAL_FEASIBILITY.md" "Release\技术可行性分析.md" -Force
}

$exe = Get-Item "Release\PSBatteryTray.exe"
Write-Host ""
Write-Host ("构建完成：" + $exe.FullName) -ForegroundColor Green
Write-Host ("文件大小：{0:N2} MB" -f ($exe.Length / 1MB)) -ForegroundColor Green
Write-Host "可直接双击运行（首次运行会出现在托盘的「隐藏的图标」面板里）。"

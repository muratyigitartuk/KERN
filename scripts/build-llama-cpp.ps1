param(
    [string]$SourceRoot = "",
    [switch]$Vulkan
)

$ErrorActionPreference = "Stop"

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"

if (-not $SourceRoot) {
    $SourceRoot = Join-Path $env:USERPROFILE "Desktop\tools\llama.cpp-fresh"
}

if (-not (Test-Path $vswhere)) {
    throw "vswhere.exe was not found. Install Visual Studio Build Tools."
}

$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) {
    throw "Visual Studio C++ Build Tools were not found."
}

$cmakeCandidates = @(
    (Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"),
    (Get-Command "cmake" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
) | Where-Object { $_ }
$ninjaCandidates = @(
    (Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"),
    (Get-Command "ninja" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
) | Where-Object { $_ }

$cmake = $cmakeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$ninja = $ninjaCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $cmake) {
    throw "CMake was not found. Install Visual Studio Build Tools with the CMake component."
}
if (-not $ninja) {
    throw "Ninja was not found. Install Visual Studio Build Tools with the CMake component."
}

if ($Vulkan -and -not $env:VULKAN_SDK) {
    $sdkRoot = Get-ChildItem "C:\VulkanSDK" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if ($sdkRoot) {
        $env:VULKAN_SDK = $sdkRoot.FullName
        $env:PATH = "$($sdkRoot.FullName)\Bin;$env:PATH"
    }
}

if ($Vulkan -and -not $env:VULKAN_SDK) {
    throw "Vulkan SDK was not found. Install it first, for example: winget install --id KhronosGroup.VulkanSDK --source winget"
}

if (-not (Test-Path $SourceRoot)) {
    git clone https://github.com/ggml-org/llama.cpp.git $SourceRoot
}

Push-Location $SourceRoot
try {
    git pull --ff-only
    $buildDir = if ($Vulkan) { "build-kern-vulkan" } else { "build-kern-cpu" }
    $vulkanFlag = if ($Vulkan) { "ON" } else { "OFF" }
    $configure = "`"$vs\VC\Auxiliary\Build\vcvars64.bat`" >nul && `"$cmake`" -S . -B $buildDir -G Ninja -DCMAKE_MAKE_PROGRAM=`"$ninja`" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DLLAMA_CURL=OFF -DGGML_VULKAN=$vulkanFlag"
    $build = "`"$vs\VC\Auxiliary\Build\vcvars64.bat`" >nul && `"$cmake`" --build $buildDir --config Release --target llama-server -j 8"

    cmd /c $configure
    if ($LASTEXITCODE -ne 0) {
        throw "llama.cpp configure failed with exit code $LASTEXITCODE."
    }

    cmd /c $build
    if ($LASTEXITCODE -ne 0) {
        throw "llama.cpp build failed with exit code $LASTEXITCODE."
    }

    $server = Join-Path $SourceRoot "$buildDir\bin\llama-server.exe"
    if (-not (Test-Path $server)) {
        throw "Build finished but llama-server.exe was not found at $server."
    }
    Write-Host "Built llama-server: $server" -ForegroundColor Green
}
finally {
    Pop-Location
}

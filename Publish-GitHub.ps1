$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Windows PowerShell 5.1 reads UTF-8 without BOM incorrectly. This file is
# distributed as UTF-8 BOM and we also align native command output to UTF-8.
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
} catch {
    # Encoding setup is cosmetic; publishing can continue if the host rejects it.
}

$Owner = "mebularts"
$Repo = "spotidown"
$FullRepo = "$Owner/$Repo"
$Version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot "VERSION.txt") -Raw).Trim()
$Tag = "v$Version"
$Description = "Smart Spotify metadata sync, deduplicated local music library and Apple Music new-arrivals workflow."
$Homepage = "https://mebularts.com.tr"

Set-Location $PSScriptRoot

function Write-Step([string]$Text) {
    Write-Host "`n==> $Text" -ForegroundColor Cyan
}

function Ensure-Command([string]$Command, [string]$WingetId) {
    if (Get-Command $Command -ErrorAction SilentlyContinue) { return }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "'$Command' bulunamadı ve winget mevcut değil. Önce $Command kurmalısın."
    }

    Write-Step "$Command bulunamadı. winget ile kuruluyor..."
    & winget install --id $WingetId -e --source winget --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "$Command kurulumu başarısız oldu." }

    # winget PATH değişikliğini mevcut PowerShell oturumu hemen görmeyebilir.
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"

    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command kuruldu ancak bu terminal PATH'i henüz görmüyor. Terminali kapatıp scripti yeniden çalıştır."
    }
}

# Some native commands intentionally return non-zero while we probe state
# (for example: no HEAD yet, repo does not exist yet, no remote yet).
# With $ErrorActionPreference='Stop', PowerShell 5.1 can turn redirected stderr
# into a terminating error. Run those probes with errors silenced and inspect
# only the native exit code.
function Test-NativeSuccess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$ArgumentList = @()
    )

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $FilePath @ArgumentList *> $null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

Write-Host @"

SpotiDown GitHub Publisher
by mebularts
--------------------------
Target : https://github.com/$FullRepo
Version: $Tag
"@ -ForegroundColor Green

Ensure-Command "git" "Git.Git"
Ensure-Command "gh" "GitHub.cli"

Write-Step "GitHub oturumu kontrol ediliyor..."
$AuthOk = Test-NativeSuccess -FilePath "gh" -ArgumentList @("auth", "status")
if (-not $AuthOk) {
    Write-Host "GitHub oturumu yok. Tarayıcı üzerinden giriş açılıyor..." -ForegroundColor Yellow
    & gh auth login --hostname github.com --git-protocol https --web
    if ($LASTEXITCODE -ne 0) { throw "GitHub giriş işlemi tamamlanamadı." }
}

$Login = (& gh api user --jq .login).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Login) { throw "Aktif GitHub hesabı okunamadı." }
if ($Login -ne $Owner) {
    throw "Aktif GitHub hesabı '$Login'. Bu yayın scripti '$Owner' hesabı için hazırlandı. 'gh auth switch -u $Owner' çalıştırıp tekrar dene."
}

Write-Step "Yerel Git reposu hazırlanıyor..."
if (-not (Test-Path -LiteralPath ".git")) {
    & git init
    if ($LASTEXITCODE -ne 0) { throw "git init başarısız oldu." }
}

& git branch -M main
if ($LASTEXITCODE -ne 0) { throw "main branch hazırlanamadı." }

& git config user.name "$Owner"
& git config user.email "$Owner@users.noreply.github.com"

& git add --all
if ($LASTEXITCODE -ne 0) { throw "Dosyalar Git staging alanına eklenemedi." }

# .gitignore'ın kritik yerel dosyaları gerçekten dışarıda tuttuğunu ayrıca doğrula.
$TrackedSensitive = & git ls-files | Select-String -Pattern '(^|/)(\.env|youtube-cookies\.txt|library\.sqlite)$|\.(mp3|m4a|flac|wav|ogg|opus)$'
if ($TrackedSensitive) {
    Write-Host $TrackedSensitive -ForegroundColor Red
    throw "Gizli/runtime dosyası Git'e eklenmek üzere görünüyor. Yayın güvenlik nedeniyle durduruldu."
}

$HasHead = Test-NativeSuccess -FilePath "git" -ArgumentList @("rev-parse", "--verify", "HEAD")
$IndexIsClean = Test-NativeSuccess -FilePath "git" -ArgumentList @("diff", "--cached", "--quiet")
$HasStagedChanges = -not $IndexIsClean

if (-not $HasHead -or $HasStagedChanges) {
    & git commit -m "release: SpotiDown $Tag by mebularts"
    if ($LASTEXITCODE -ne 0) { throw "Git commit oluşturulamadı." }
} else {
    Write-Host "Commit edilecek yeni değişiklik yok; mevcut HEAD kullanılacak." -ForegroundColor DarkGray
}

Write-Step "GitHub reposu hazırlanıyor: $FullRepo"
$RepoExists = Test-NativeSuccess -FilePath "gh" -ArgumentList @("repo", "view", $FullRepo)

if (-not $RepoExists) {
    & gh repo create $FullRepo --public --source=. --remote=origin --push --description $Description --homepage $Homepage
    if ($LASTEXITCODE -ne 0) { throw "GitHub reposu oluşturulamadı." }
} else {
    Write-Host "Repo zaten var; mevcut repoya güvenli normal push yapılacak." -ForegroundColor Yellow
    $OriginExists = Test-NativeSuccess -FilePath "git" -ArgumentList @("remote", "get-url", "origin")
    if ($OriginExists) {
        & git remote set-url origin "https://github.com/$FullRepo.git"
    } else {
        & git remote add origin "https://github.com/$FullRepo.git"
    }
    if ($LASTEXITCODE -ne 0) { throw "Git remote ayarlanamadı." }

    & git push -u origin main
    if ($LASTEXITCODE -ne 0) {
        throw "Push reddedildi. Uzak repoda farklı geçmiş olabilir; script güvenlik için force-push yapmadı."
    }
}

Write-Step "Repo bilgileri ve topic'ler güncelleniyor..."
& gh repo edit $FullRepo `
    --description $Description `
    --homepage $Homepage `
    --enable-issues `
    --add-topic spotify `
    --add-topic music-library `
    --add-topic python `
    --add-topic spotdl `
    --add-topic soundcloud `
    --add-topic apple-music `
    --add-topic windows `
    --add-topic deduplication | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Repo bilgileri güncellenemedi." }

Write-Step "Temiz release ZIP'i oluşturuluyor..."
$Dist = Join-Path $PSScriptRoot "dist"
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
$Zip = Join-Path $Dist "SpotiDown-$Tag.zip"
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }

# Yalnızca commit edilmiş/tracked dosyaları paketler; .env, audio, SQLite,
# cookie, çalışma çıktıları ve dist klasörü release ZIP'ine girmez.
& git archive --format=zip --output="$Zip" HEAD
if ($LASTEXITCODE -ne 0) { throw "Release ZIP'i oluşturulamadı." }

Write-Step "GitHub Release hazırlanıyor: $Tag"
$ReleaseExists = Test-NativeSuccess -FilePath "gh" -ArgumentList @("release", "view", $Tag, "--repo", $FullRepo)
if ($ReleaseExists) {
    & gh release upload $Tag $Zip --repo $FullRepo --clobber
} else {
    & gh release create $Tag $Zip --repo $FullRepo --target main --title "SpotiDown $Tag" --generate-notes --latest
}
if ($LASTEXITCODE -ne 0) { throw "GitHub Release oluşturma/yükleme işlemi başarısız oldu." }

Write-Host @"

============================================================
 YAYIN TAMAMLANDI / PUBLISH COMPLETE
============================================================
 Repo    : https://github.com/$FullRepo
 Release : https://github.com/$FullRepo/releases/tag/$Tag
 ZIP     : $Zip
============================================================
"@ -ForegroundColor Green

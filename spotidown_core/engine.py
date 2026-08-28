#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SpotiDown
=========

Spotify playlistlerini spotDL'nin olgun metadata, eşleştirme ve indirme motoru
üzerinden indirir. Dosya adı eski komutlarla uyumluluk için korunmuştur;
Selenium kullanılmaz.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

from .library import Library, spotify_id, track_identity
from .i18n import bi


APP_NAME = "SpotiDown"

# ============================================================================
# ARGÜMANSIZ ÇALIŞTIRMA AYARLARI
# Bu bölümdeki değerler, dosya doğrudan çalıştırıldığında varsayılan olarak
# kullanılır. Komut satırı argümanları verilirse bu değerlerin üzerine yazılır.
# ============================================================================
DEFAULT_PLAYLIST_URL = "https://open.spotify.com/playlist/6AnD6DPdHJXgcjaORGeU1J"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "downloads"
DEFAULT_LIBRARY_DB = PROJECT_ROOT / "data" / "library.sqlite"
DEFAULT_MANIFEST_DIR = PROJECT_ROOT / "data" / "manifests"
DEFAULT_PLAYLIST_DIR = PROJECT_ROOT / "playlists"
DEFAULT_THREADS = 8
DEFAULT_BITRATE = "192k"
DEFAULT_PASSES = 4

# YOUTUBE / YT-DLP MOTOR AYARLARI
# Güncel YouTube çözümleyicisi Deno + yt-dlp-ejs kullanır. İlk çalıştırmada
# ve aşağıdaki süre dolduğunda yt-dlp[default] güncellenir.
DEFAULT_AUTO_UPDATE_YTDLP = True
DEFAULT_YTDLP_UPDATE_INTERVAL_DAYS = 7
DEFAULT_YTDLP_SLEEP_REQUESTS = 0.0
DEFAULT_YTDLP_SLEEP_INTERVAL = 0
DEFAULT_YTDLP_MAX_SLEEP_INTERVAL = 0
DEFAULT_YTDLP_SOCKET_TIMEOUT = 15
DEFAULT_YTDLP_RETRIES = 2
DEFAULT_YTDLP_FRAGMENT_RETRIES = 2
DEFAULT_YTDLP_EXTRACTOR_RETRIES = 1
DEFAULT_YTDLP_CONCURRENT_FRAGMENTS = 4

# Ağ profili: varsayılan olarak iki turda da sabit proxy korunur.
# False yapılırsa yalnızca ikinci (YouTube) turu doğrudan bağlantıya geçmez.
DEFAULT_RETRY_WITHOUT_PROXY = False
DEFAULT_NON_YOUTUBE_FALLBACK = False

# COOKIE AYARLARI
# "never"  : Cookie hiçbir zaman kullanılmaz.
# "always" : Her indirme turunda cookie kullanılır.
# "auto"   : İlk tur cookiesiz, sonraki turlar cookie ile çalışır.
DEFAULT_COOKIE_MODE = "never"
DEFAULT_COOKIE_FILE = PROJECT_ROOT / "youtube-cookies.txt"

# PROXY AYARLARI
# True olduğunda aşağıdaki sabit proxy argümansız çalıştırmada kullanılır.
# Ortamda SPOTIDOWN_PROXY tanımlıysa sabit adresin yerine o değer alınır.
DEFAULT_PROXY_ENABLED = bool(os.environ.get("SPOTIDOWN_PROXY"))
DEFAULT_PROXY_URL = os.environ.get("SPOTIDOWN_PROXY")


MIN_SPOTDL_VERSION = (4, 5, 2)
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".flac", ".ogg", ".wav"}

# SES KAYNAĞI ÖNCELİĞİ
# Komut satırındaki genel sıra. Hızlı varsayılan akışta ilk tur yalnızca
# SoundCloud/Bandcamp, ikinci tur yalnızca kalanlar için YouTube kullanır.
DEFAULT_AUDIO_PROVIDERS = ("soundcloud", "bandcamp", "youtube", "youtube-music")

# AKILLI SOUNDCLOUD ÇOKLU SONUÇ AYARLARI
# İlk hızlı SoundCloud turunda seçilen yükleme indirilemezse yalnızca başarısız
# kalan parçalar için SoundCloud aramasındaki diğer yükleyiciler denenir.
DEFAULT_SOUNDCLOUD_MULTI_RESULT_ENABLED = True
DEFAULT_SOUNDCLOUD_SEARCH_RESULTS = 15
DEFAULT_SOUNDCLOUD_ALTERNATIVE_ATTEMPTS = 6
DEFAULT_SOUNDCLOUD_SEARCH_THREADS = 8
DEFAULT_SOUNDCLOUD_DOWNLOAD_THREADS = 6
DEFAULT_SOUNDCLOUD_DURATION_TOLERANCE_SECONDS = 18
DEFAULT_SOUNDCLOUD_DURATION_TOLERANCE_RATIO = 0.12
DEFAULT_SOUNDCLOUD_MIN_MATCH_SCORE = 0.42

# Kurtarma aşamaları ayrı yürütülür. Böylece bir kaynakta indirilen parça sonraki
# kaynaklarda tekrar aranmaz.
PRIMARY_SOUNDCLOUD_PROVIDERS = ("soundcloud",)
BANDCAMP_FALLBACK_PROVIDERS = ("bandcamp",)
YOUTUBE_FALLBACK_PROVIDERS = ("youtube", "youtube-music")
ENGINE_UPDATE_MARKER = Path.home() / ".spotdl" / "spotidown-engine-update.json"


class Console:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

    @classmethod
    def info(cls, message: str) -> None:
        print(f"{cls.CYAN}{bi('[BİLGİ]', '[INFO]')}{cls.RESET} {message}", flush=True)

    @classmethod
    def success(cls, message: str) -> None:
        print(f"{cls.GREEN}{bi('[TAMAM]', '[OK]')}{cls.RESET} {message}", flush=True)

    @classmethod
    def warning(cls, message: str) -> None:
        print(f"{cls.YELLOW}{bi('[UYARI]', '[WARN]')}{cls.RESET} {message}", flush=True)

    @classmethod
    def error(cls, message: str) -> None:
        print(f"{cls.RED}{bi('[HATA]', '[ERROR]')}{cls.RESET} {message}", flush=True)


def enable_windows_ansi() -> None:
    if os.name == "nt":
        os.system("chcp 65001 > nul")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def numeric_version(value: str) -> tuple[int, ...]:
    values = re.findall(r"\d+", value)
    return tuple(int(item) for item in values[:3])


def validate_spotify_url(value: str) -> str:
    parsed = urlparse(value)
    valid_hosts = {"open.spotify.com", "www.spotify.com"}
    valid_types = r"/(playlist|track|album)/[A-Za-z0-9]+"
    if parsed.scheme not in {"http", "https"}:
        raise argparse.ArgumentTypeError("Spotify URL'si http/https ile başlamalıdır.")
    if parsed.netloc.lower() not in valid_hosts or not re.search(valid_types, parsed.path):
        raise argparse.ArgumentTypeError(
            "Geçerli bir Spotify playlist, albüm veya parça URL'si girin."
        )
    return value


def validate_proxy_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5", "socks5h"}:
        raise argparse.ArgumentTypeError(
            "Proxy protokolü http, https, socks4, socks5 veya socks5h olmalıdır."
        )
    if not parsed.hostname or parsed.port is None:
        raise argparse.ArgumentTypeError(
            "Proxy `protocol://login:password@hostname:port` biçiminde olmalıdır."
        )
    return value


def validate_netscape_cookie_file(path: Path) -> tuple[bool, str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        return False, f"Cookie dosyası okunamadı: {exc}"

    if not lines or lines[0].strip() not in {
        "# Netscape HTTP Cookie File",
        "# HTTP Cookie File",
    }:
        return False, "İlk satır Netscape cookie başlığı değil."

    cookie_count = 0
    youtube_count = 0
    for line_number, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = raw_line.split("\t")
        if len(fields) != 7:
            return False, f"{line_number}. satır 7 sekmeli alandan oluşmuyor."
        domain = fields[0].removeprefix("#HttpOnly_").lower()
        if fields[1] not in {"TRUE", "FALSE"} or fields[3] not in {"TRUE", "FALSE"}:
            return False, f"{line_number}. satırdaki TRUE/FALSE alanı geçersiz."
        try:
            int(fields[4])
        except ValueError:
            return False, f"{line_number}. satırdaki son kullanma zamanı geçersiz."
        cookie_count += 1
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            youtube_count += 1

    if cookie_count == 0:
        return False, "Dosyada cookie kaydı bulunamadı."
    if youtube_count == 0:
        return False, "Dosyada youtube.com cookie kaydı bulunamadı."
    return True, f"{youtube_count} YouTube cookie kaydı doğrulandı."


def spotdl_version() -> str | None:
    if importlib.util.find_spec("spotdl") is None:
        return None
    try:
        return importlib.metadata.version("spotdl")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_child_environment(proxy: str | None = None) -> dict[str, str]:
    """Alt süreçler için proxy ortamını tutarlı şekilde hazırlar."""
    environment = os.environ.copy()
    if not proxy:
        return environment

    # Büyük/küçük harfli değişkenlerin tamamı ayarlanır. requests, httpx,
    # urllib ve yt-dlp sürümleri platforma göre farklı isimleri okuyabilir.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment[name] = proxy

    # Kullanıcının sistemindeki genel NO_PROXY ayarı proxy'yi yanlışlıkla
    # atlamasın. Bu süreçte ağ isteklerinin tamamı sabit proxy'den geçer.
    environment.pop("NO_PROXY", None)
    environment.pop("no_proxy", None)
    return environment


def install_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "spotdl>=4.5.2",
    ]


def ensure_spotdl(auto_install: bool, proxy: str | None = None) -> bool:
    version = spotdl_version()
    if version and numeric_version(version) >= MIN_SPOTDL_VERSION:
        Console.success(bi(f"spotDL {version} hazır.", f"spotDL {version} is ready."))
        return True

    if version:
        Console.warning(bi(f"spotDL {version} eski; en az 4.5.2 gerekli.", f"spotDL {version} is outdated; 4.5.2 or newer is required."))
    else:
        Console.warning(bi("spotDL kurulu değil.", "spotDL is not installed."))

    command = install_command()
    if not auto_install:
        Console.error(bi("Kurulum gerekli:", "Installation required:"))
        print(subprocess.list2cmdline(command))
        return False

    Console.info(bi("spotDL kuruluyor/güncelleniyor (bir defalık işlem)...", "Installing/updating spotDL (one-time operation)..."))
    result = subprocess.run(
        command,
        check=False,
        env=build_child_environment(proxy),
    )
    if result.returncode != 0:
        Console.error(bi("spotDL kurulamadı.", "spotDL could not be installed."))
        return False

    importlib.invalidate_caches()
    version = spotdl_version()
    if not version or numeric_version(version) < MIN_SPOTDL_VERSION:
        Console.error(bi("Kurulum tamamlandı ancak uygun spotDL sürümü bulunamadı.", "Installation finished, but a compatible spotDL version was not found."))
        return False

    Console.success(bi(f"spotDL {version} kuruldu.", f"spotDL {version} installed."))
    return True


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def resolve_deno_executable() -> Path | None:
    candidates = [
        shutil.which("deno"),
        str(Path.home() / ".spotdl" / "deno.exe"),
        str(Path.home() / ".spotdl" / "deno"),
    ]
    for executable in candidates:
        if not executable:
            continue
        path = Path(executable).resolve()
        if not path.is_file():
            continue
        try:
            result = subprocess.run(
                [str(path), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            match = re.search(r"deno\s+(\d+(?:\.\d+){1,2})", result.stdout)
            if match and numeric_version(match.group(1)) >= (2, 3, 0):
                return path
        except Exception:
            continue
    return None


def deno_available() -> bool:
    return resolve_deno_executable() is not None


def package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def engine_update_due() -> bool:
    if not DEFAULT_AUTO_UPDATE_YTDLP:
        return False
    try:
        payload = json.loads(ENGINE_UPDATE_MARKER.read_text(encoding="utf-8"))
        checked_at = float(payload.get("checked_at", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True
    return time.time() - checked_at >= DEFAULT_YTDLP_UPDATE_INTERVAL_DAYS * 86400


def mark_engine_updated() -> None:
    try:
        ENGINE_UPDATE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        ENGINE_UPDATE_MARKER.write_text(
            json.dumps({"checked_at": time.time()}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def ensure_ytdlp_engine(auto_install: bool, proxy: str | None = None) -> bool:
    yt_dlp_version = package_version("yt-dlp")
    ejs_version = package_version("yt-dlp-ejs")
    must_install = not yt_dlp_version or not ejs_version
    must_update = auto_install and engine_update_due()

    if not must_install and not must_update:
        Console.success(bi(
            f"yt-dlp {yt_dlp_version} ve yt-dlp-ejs {ejs_version} hazır.",
            f"yt-dlp {yt_dlp_version} and yt-dlp-ejs {ejs_version} are ready.",
        ))
        return True

    if not auto_install:
        if must_install:
            Console.error(bi(
                "YouTube motoru eksik. `python -m pip install -U \"yt-dlp[default]\"` komutunu çalıştırın.",
                "YouTube engine is missing. Run `python -m pip install -U \"yt-dlp[default]\"`.",
            ))
            return False
        return True

    Console.info(bi("yt-dlp ve YouTube EJS çözümleyicisi kontrol ediliyor/güncelleniyor...", "Checking/updating yt-dlp and the YouTube EJS solver..."))
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "yt-dlp[default]",
    ]
    result = subprocess.run(
        command,
        check=False,
        env=build_child_environment(proxy),
    )
    if result.returncode != 0 and proxy:
        Console.warning(bi(
            "YouTube motoru proxy üzerinden güncellenemedi; doğrudan bağlantıyla bir kez daha deneniyor.",
            "The YouTube engine could not be updated through the proxy; retrying once with a direct connection.",
        ))
        result = subprocess.run(
            command,
            check=False,
            env=build_child_environment(None),
        )
    importlib.invalidate_caches()
    yt_dlp_version = package_version("yt-dlp")
    ejs_version = package_version("yt-dlp-ejs")
    if result.returncode != 0 or not yt_dlp_version or not ejs_version:
        Console.error(bi(
            "yt-dlp veya yt-dlp-ejs kurulamadı. Yukarıdaki pip hatasını kontrol edin.",
            "yt-dlp or yt-dlp-ejs could not be installed. Check the pip error above.",
        ))
        return False
    mark_engine_updated()
    Console.success(bi(
        f"yt-dlp {yt_dlp_version} ve yt-dlp-ejs {ejs_version} hazır.",
        f"yt-dlp {yt_dlp_version} and yt-dlp-ejs {ejs_version} are ready.",
    ))
    return True


def run_spotdl_setup(option: str, proxy: str | None = None) -> bool:
    command = [sys.executable, "-m", "spotdl", option]
    result = subprocess.run(
        command,
        check=False,
        env=build_child_environment(proxy),
    )
    return result.returncode == 0


def ensure_runtime(auto_install: bool, proxy: str | None = None) -> bool:
    if not ffmpeg_available():
        if not auto_install:
            Console.error(bi("FFmpeg bulunamadı. `spotdl --download-ffmpeg` çalıştırın.", "FFmpeg was not found. Run `spotdl --download-ffmpeg`."))
            return False
        Console.info(bi("FFmpeg spotDL klasörüne indiriliyor...", "Downloading FFmpeg into the spotDL directory..."))
        if not run_spotdl_setup("--download-ffmpeg", proxy):
            Console.error(bi("FFmpeg otomatik olarak kurulamadı.", "FFmpeg could not be installed automatically."))
            return False

    if not deno_available():
        if not auto_install:
            Console.warning(bi(
                "Deno >= 2.3 bulunamadı. Bazı YouTube parçaları indirilemeyebilir.",
                "Deno >= 2.3 was not found. Some YouTube tracks may fail.",
            ))
            return True
        Console.info(bi("Deno spotDL klasörüne indiriliyor (bir defalık işlem)...", "Downloading Deno into the spotDL directory (one-time operation)..."))
        if not run_spotdl_setup("--download-deno", proxy):
            Console.warning(bi(
                "Deno otomatik kurulamadı. spotDL yine çalıştırılacak ancak bazı YouTube parçaları başarısız olabilir.",
                "Deno could not be installed automatically. spotDL will still run, but some YouTube tracks may fail.",
            ))
    return True


def output_template(output_dir: Path) -> str:
    # Track ID yalnızca geçici .incoming isminde kullanılır. İndirme tamamlanınca
    # dosya downloads/library/Artist - Title.ext biçimine taşınır. Böylece
    # eşleme güvenilir kalırken kullanıcıya görünen dosya adında Spotify ID olmaz.
    template = (
        ".incoming"
        + os.sep
        + "{track-id} - {artists} - {title}.{output-ext}"
    )
    return str(output_dir.resolve() / template)

def build_download_command(
    args: argparse.Namespace,
    query: Path,
    threads: int,
    use_cookies: bool,
    providers: tuple[str, ...] | list[str] | None = None,
    proxy: str | None = None,
    pass_number: int = 1,
) -> list[str]:
    selected_providers = list(providers or args.audio)
    command = [
        sys.executable,
        "-m",
        "spotdl",
        "download",
        str(query.resolve()),
        "--audio",
        *selected_providers,
        "--threads",
        str(threads),
        "--format",
        args.format,
        "--bitrate",
        args.bitrate,
        "--output",
        output_template(args.output),
        "--overwrite",
        "skip",
        "--max-retries",
        str(args.retries),
        "--simple-tui",
        "--print-errors",
        "--save-errors",
        str((args.output.resolve() / f"spotdl-errors-pass-{pass_number}.txt")),
    ]
    if args.preload:
        command.append("--preload")
    if args.only_verified:
        command.append("--only-verified-results")

    yt_dlp_arguments: list[str] = []
    if use_cookies and args.cookie_file:
        command.extend(["--cookie-file", str(args.cookie_file.resolve())])
    else:
        yt_dlp_arguments.append("--no-cookies")

    # yt-dlp 2026 sürümlerinde YouTube JavaScript challenge çözümü için Deno
    # yolunu açıkça veriyoruz. ~/.spotdl içindeki Deno PATH'te olmayabilir.
    deno_path = resolve_deno_executable()
    if deno_path:
        deno_value = deno_path.as_posix()
        yt_dlp_arguments.append(f"--js-runtimes=deno:{deno_value}")

    # İstemci listesini elle zorlamıyoruz. Güncel yt-dlp uygun YouTube
    # istemcilerini ve EJS çözümünü kendi sürümüne göre seçer.
    # Hızlı mod: başarısız URL'lerde yt-dlp'nin varsayılan uzun tekrar ve
    # bekleme sürelerini kısaltır. Başarılı indirmelerde kaliteyi etkilemez.
    yt_dlp_arguments.extend(
        [
            f"--socket-timeout={DEFAULT_YTDLP_SOCKET_TIMEOUT}",
            f"--retries={DEFAULT_YTDLP_RETRIES}",
            f"--fragment-retries={DEFAULT_YTDLP_FRAGMENT_RETRIES}",
            f"--extractor-retries={DEFAULT_YTDLP_EXTRACTOR_RETRIES}",
            f"--concurrent-fragments={DEFAULT_YTDLP_CONCURRENT_FRAGMENTS}",
        ]
    )
    if DEFAULT_YTDLP_SLEEP_REQUESTS > 0:
        yt_dlp_arguments.append(
            f"--sleep-requests={DEFAULT_YTDLP_SLEEP_REQUESTS}"
        )
    if DEFAULT_YTDLP_SLEEP_INTERVAL > 0:
        yt_dlp_arguments.append(
            f"--sleep-interval={DEFAULT_YTDLP_SLEEP_INTERVAL}"
        )
    if (
        DEFAULT_YTDLP_SLEEP_INTERVAL > 0
        and DEFAULT_YTDLP_MAX_SLEEP_INTERVAL >= DEFAULT_YTDLP_SLEEP_INTERVAL
    ):
        yt_dlp_arguments.append(
            f"--max-sleep-interval={DEFAULT_YTDLP_MAX_SLEEP_INTERVAL}"
        )
    if proxy:
        yt_dlp_arguments.append(f"--proxy={proxy}")
    if yt_dlp_arguments:
        command.extend(["--yt-dlp-args", " ".join(yt_dlp_arguments)])
    return command


def build_scan_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "spotdl",
        "save",
        args.spotify_url,
        "--save-file",
        str(args.manifest.resolve()),
        "--threads",
        str(args.threads),
        "--simple-tui",
    ]


def load_manifest(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Manifest okunamadı: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Manifest biçimi geçersiz; JSON listesi bekleniyordu.")
    return [item for item in payload if isinstance(item, dict)]


def audio_files_by_identity(
    output_dir: Path,
    tracks: list[dict[str, object]],
    library: Library,
    source: str | None = None,
) -> dict[str, Path]:
    # ID güvenli eşleme için sadece geçici isimde kullanılır. Sonra dosyaları
    # okunabilir Artist - Title isimlerine taşı ve DB yolunu güncelle.
    library.scan_id_filenames(output_dir, tracks, source)
    library.beautify_id_files(output_dir, tracks, output_dir / "library", source)
    return library.existing_paths(tracks)

def pending_tracks(
    tracks: list[dict[str, object]], existing: dict[str, Path]
) -> list[dict[str, object]]:
    seen: set[str] = set()
    pending: list[dict[str, object]] = []
    for track in tracks:
        identity = track_identity(track)
        if identity in existing or identity in seen:
            continue
        seen.add(identity)
        pending.append(track)
    return pending

def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def normalize_match_text(value: object) -> str:
    """Karşılaştırma için metni Türkçe karakterlere dayanıklı sadeleştirir."""
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def track_artist_text(track: dict[str, object]) -> str:
    artists = track.get("artists") or [track.get("artist") or ""]
    if isinstance(artists, list):
        return ", ".join(str(artist) for artist in artists if artist)
    return str(artists)


def track_search_text(track: dict[str, object]) -> str:
    return f"{track_artist_text(track)} - {str(track.get('name') or '').strip()}".strip()


def collect_failed_soundcloud_urls(output_dir: Path) -> set[str]:
    """Önceki spotDL turlarında hata veren SoundCloud URL'lerini toplar."""
    failed: set[str] = set()
    pattern = re.compile(r"https?://(?:www\.)?soundcloud\.com/[^\s\]\[\)\(\"']+")
    for error_file in output_dir.glob("spotdl-errors-pass-*.txt"):
        try:
            content = error_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in pattern.findall(content):
            failed.add(match.rstrip(".,;:!?"))
    return failed


def soundcloud_candidate_score(
    track: dict[str, object], candidate: dict[str, object]
) -> float:
    """İsim, sanatçı ve süreye göre SoundCloud sonucunu puanlar."""
    target_title = normalize_match_text(track.get("name"))
    target_artist = normalize_match_text(track_artist_text(track))
    target_full = normalize_match_text(track_search_text(track))

    candidate_title = normalize_match_text(candidate.get("title"))
    candidate_uploader = normalize_match_text(
        candidate.get("uploader") or candidate.get("channel")
    )
    candidate_full = f"{candidate_uploader} {candidate_title}".strip()

    title_score = difflib.SequenceMatcher(
        None, target_title, candidate_title
    ).ratio()
    full_score = difflib.SequenceMatcher(None, target_full, candidate_full).ratio()

    target_tokens = set(f"{target_artist} {target_title}".split())
    candidate_tokens = set(candidate_full.split())
    token_score = (
        len(target_tokens & candidate_tokens) / max(1, len(target_tokens))
    )

    target_duration = int(track.get("duration") or 0)
    try:
        candidate_duration = int(float(candidate.get("duration") or 0))
    except (TypeError, ValueError):
        candidate_duration = 0

    duration_score = 0.50
    if target_duration > 0 and candidate_duration > 0:
        tolerance = max(
            DEFAULT_SOUNDCLOUD_DURATION_TOLERANCE_SECONDS,
            int(target_duration * DEFAULT_SOUNDCLOUD_DURATION_TOLERANCE_RATIO),
        )
        difference = abs(target_duration - candidate_duration)
        if difference > tolerance:
            return -1.0
        duration_score = max(0.0, 1.0 - difference / max(1, tolerance))

    score = (
        title_score * 0.42
        + full_score * 0.20
        + token_score * 0.18
        + duration_score * 0.20
    )

    # Spotify parçasında bulunmayan sürüm etiketlerini cezalandır. Böylece
    # slowed, remix, cover, instrumental gibi yanlış yüklemeler alta düşer.
    variant_terms = (
        "remix",
        "slowed",
        "sped up",
        "nightcore",
        "instrumental",
        "karaoke",
        "cover",
        "live",
        "reverb",
        "8d",
    )
    for term in variant_terms:
        normalized_term = normalize_match_text(term)
        if normalized_term in candidate_full and normalized_term not in target_full:
            score -= 0.12

    return score


def search_soundcloud_candidates(
    track: dict[str, object],
    proxy: str | None,
    search_results: int,
    attempted_urls: set[str],
) -> tuple[str, list[str]]:
    """yt-dlp scsearch ile bir şarkının farklı SoundCloud yüklemelerini bulur."""
    identity = track_identity(track)
    query = track_search_text(track)
    if not query:
        return identity, []

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return identity, []

    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "playlistend": search_results,
        "socket_timeout": DEFAULT_YTDLP_SOCKET_TIMEOUT,
        "retries": 1,
        "extractor_retries": 1,
    }
    if proxy:
        options["proxy"] = proxy

    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(
                f"scsearch{search_results}:{query}",
                download=False,
            )
    except Exception:
        return identity, []

    if not isinstance(info, dict):
        return identity, []
    entries = info.get("entries") or []
    if not isinstance(entries, list):
        return identity, []

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
            or ""
        ).strip()
        if not url.startswith("http") or "soundcloud.com/" not in url:
            continue
        url = url.rstrip("/")
        if url in seen or url in attempted_urls:
            continue
        seen.add(url)
        score = soundcloud_candidate_score(track, entry)
        if score >= DEFAULT_SOUNDCLOUD_MIN_MATCH_SCORE:
            scored.append((score, url))

    scored.sort(key=lambda item: item[0], reverse=True)
    urls = [url for _score, url in scored]
    return identity, urls[:DEFAULT_SOUNDCLOUD_ALTERNATIVE_ATTEMPTS]


def discover_soundcloud_candidates(
    tracks: list[dict[str, object]],
    proxy: str | None,
    search_results: int,
    attempted_urls: set[str],
) -> dict[str, list[str]]:
    """Başarısız parçalar için SoundCloud alternatiflerini paralel arar."""
    candidates: dict[str, list[str]] = {}
    if not tracks:
        return candidates

    workers = min(DEFAULT_SOUNDCLOUD_SEARCH_THREADS, len(tracks))
    Console.info(bi(
        f"SoundCloud çoklu sonuç taraması: {len(tracks)} parça için ilk {search_results} sonuç inceleniyor.",
        f"SoundCloud multi-result scan: checking the first {search_results} results for {len(tracks)} tracks.",
    ))
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                search_soundcloud_candidates,
                track,
                proxy,
                search_results,
                attempted_urls,
            )
            for track in tracks
        ]
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                identity, urls = future.result()
            except Exception:
                continue
            if urls:
                candidates[identity] = urls
            if completed % 25 == 0 or completed == len(tracks):
                Console.info(bi(
                    f"SoundCloud alternatif taraması: {completed}/{len(tracks)} tamamlandı.",
                    f"SoundCloud alternative scan: {completed}/{len(tracks)} completed.",
                ))

    total_urls = sum(len(urls) for urls in candidates.values())
    Console.info(bi(
        f"{len(candidates)} parça için {total_urls} uygun farklı SoundCloud yüklemesi bulundu.",
        f"Found {total_urls} suitable alternative SoundCloud uploads for {len(candidates)} tracks.",
    ))
    return candidates


def filter_target_tracks(
    tracks: list[dict[str, object]],
    existing: dict[str, Path],
    target_identities: set[str] | None,
) -> list[dict[str, object]]:
    pending = pending_tracks(tracks, existing)
    if target_identities is None:
        return pending
    return [track for track in pending if track_identity(track) in target_identities]

def run_spotdl_stage(
    args: argparse.Namespace,
    tracks: list[dict[str, object]],
    manifest_path: Path,
    providers: tuple[str, ...],
    threads: int,
    proxy: str | None,
    pass_number: int,
    label: str,
    use_cookies: bool = False,
) -> int:
    if not tracks:
        return 0
    write_json(manifest_path, tracks)
    proxy_label = bi("proxy ile", "with proxy") if proxy else bi("proxysiz", "without proxy")
    cookie_label = bi("cookie ile", "with cookies") if use_cookies else bi("cookiesiz", "without cookies")
    Console.info(bi(
        f"{label}: {len(tracks)} parça, {threads} işçi, {cookie_label}, {proxy_label}.",
        f"{label}: {len(tracks)} tracks, {threads} workers, {cookie_label}, {proxy_label}.",
    ))
    return run_child(
        build_download_command(
            args,
            manifest_path,
            threads,
            use_cookies,
            providers=providers,
            proxy=proxy,
            pass_number=pass_number,
        ),
        proxy,
    )


def run_soundcloud_alternative_rounds(
    args: argparse.Namespace,
    tracks: list[dict[str, object]],
    target_identities: set[str] | None,
    library: Library,
) -> int:
    """Her turda aynı parçanın sıradaki farklı SoundCloud yüklemesini dener."""
    existing = audio_files_by_identity(args.output, tracks, library)
    pending = filter_target_tracks(tracks, existing, target_identities)
    if not pending or not DEFAULT_SOUNDCLOUD_MULTI_RESULT_ENABLED:
        return 0

    attempted_urls = collect_failed_soundcloud_urls(args.output)
    candidate_map = discover_soundcloud_candidates(
        pending,
        args.proxy,
        DEFAULT_SOUNDCLOUD_SEARCH_RESULTS,
        attempted_urls,
    )
    if not candidate_map:
        Console.warning(bi("Uygun farklı SoundCloud yüklemesi bulunamadı.", "No suitable alternative SoundCloud upload found."))
        return 0

    final_return_code = 0
    for candidate_index in range(DEFAULT_SOUNDCLOUD_ALTERNATIVE_ATTEMPTS):
        existing = audio_files_by_identity(args.output, tracks, library)
        remaining = filter_target_tracks(tracks, existing, target_identities)
        forced_batch: list[dict[str, object]] = []
        before = set(existing)
        for track in remaining:
            identity = track_identity(track)
            urls = candidate_map.get(identity, [])
            if candidate_index >= len(urls):
                continue
            forced_track = dict(track)
            forced_track["download_url"] = urls[candidate_index]
            forced_batch.append(forced_track)

        if not forced_batch:
            continue

        final_return_code = run_spotdl_stage(
            args,
            forced_batch,
            args.output / f"pending-soundcloud-alternative-{candidate_index + 1}.spotdl",
            PRIMARY_SOUNDCLOUD_PROVIDERS,
            min(DEFAULT_SOUNDCLOUD_DOWNLOAD_THREADS, args.threads),
            args.proxy,
            10 + candidate_index,
            bi(f"SoundCloud alternatif yükleme {candidate_index + 1}", f"SoundCloud alternative upload {candidate_index + 1}"),
        )
        audio_files_by_identity(args.output, tracks, library, "soundcloud-alt")
        if final_return_code in {130, -signal.SIGINT}:
            return final_return_code

    return final_return_code

def playlist_name(tracks: list[dict[str, object]]) -> str:
    if tracks:
        value = str(tracks[0].get("list_name") or "Spotify Playlist").strip()
        if value:
            return value
    return "Spotify Playlist"


def safe_playlist_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value or "Spotify Playlist"


def write_m3u8(
    playlist_dir: Path,
    tracks: list[dict[str, object]],
    existing: dict[str, Path],
) -> Path:
    playlist_dir.mkdir(parents=True, exist_ok=True)
    name = safe_playlist_filename(playlist_name(tracks))
    target = playlist_dir / f"{name}.m3u8"
    lines = ["#EXTM3U"]
    ordered = sorted(tracks, key=lambda item: int(item.get("list_position") or 0))
    for track in ordered:
        audio_path = existing.get(track_identity(track))
        if not audio_path:
            continue
        duration = int(track.get("duration") or -1)
        artists = track.get("artists") or [track.get("artist") or ""]
        if isinstance(artists, list):
            artist_text = ", ".join(str(artist) for artist in artists if artist)
        else:
            artist_text = str(artists)
        title = str(track.get("name") or audio_path.stem)
        relative = os.path.relpath(audio_path, target.parent).replace("\\", "/")
        lines.append(f"#EXTINF:{duration},{artist_text} - {title}")
        lines.append(relative)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return target

def create_apple_music_export(
    export_root: Path,
    tracks: list[dict[str, object]],
    new_identities: set[str],
    existing: dict[str, Path],
) -> tuple[Path | None, int, int]:
    """Create a zero-extra-space hardlink batch for tracks added in this run."""
    selected = [t for t in tracks if track_identity(t) in new_identities and track_identity(t) in existing]
    if not selected:
        return None, 0, 0

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = export_root / f"{stamp} - {safe_playlist_filename(playlist_name(tracks))}"
    folder.mkdir(parents=True, exist_ok=True)
    linked = 0
    failed = 0
    fallback_paths: list[str] = []

    for track in selected:
        source = existing.get(track_identity(track))
        if not source or not source.is_file():
            continue
        target = folder / source.name
        counter = 2
        while target.exists():
            target = folder / f"{source.stem} ({counter}){source.suffix}"
            counter += 1
        try:
            os.link(source, target)
            linked += 1
        except OSError:
            failed += 1
            fallback_paths.append(str(source.resolve()))

    if fallback_paths:
        (folder / "NEW_TRACKS.txt").write_text("\n".join(fallback_paths) + "\n", encoding="utf-8-sig")
    return folder, linked, failed


def run_child(command: list[str], proxy: str | None = None) -> int:
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    Console.info(bi("spotDL motoru başlatılıyor...", "Starting spotDL engine..."))
    # Proxy hem spotDL'nin kendi HTTP istemcisine hem de yt-dlp'ye taşınır.
    child_environment = build_child_environment(proxy)
    process = subprocess.Popen(
        command,
        creationflags=creation_flags,
        env=child_environment,
    )

    def stop_child(_signum: int, _frame: object) -> None:
        Console.warning(bi("Durdurma isteği alındı; çalışan spotDL kapatılıyor...", "Stop requested; shutting down the running spotDL process..."))
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.kill()
        except Exception:
            process.kill()
        os._exit(130)

    previous_handler = signal.signal(signal.SIGINT, stop_child)
    try:
        return process.wait()
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Spotify playlistlerini spotDL'nin metadata ve eşleştirme motoruyla indirir."
        ),
    )
    parser.add_argument(
        "spotify_url",
        nargs="?",
        default=DEFAULT_PLAYLIST_URL,
        type=validate_spotify_url,
        help="Spotify URL'si; verilmezse kod içindeki varsayılan kullanılır",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Çıktı dizini (varsayılan: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        choices=range(1, 17),
        default=DEFAULT_THREADS,
        metavar="1-16",
        help=f"Paralel indirme sayısı (varsayılan: {DEFAULT_THREADS})",
    )
    parser.add_argument(
        "--format",
        choices=("mp3", "m4a", "opus", "flac", "ogg", "wav"),
        default="mp3",
    )
    parser.add_argument("--bitrate", default=DEFAULT_BITRATE)
    parser.add_argument(
        "--audio",
        nargs="+",
        choices=(
            "youtube-music",
            "youtube",
            "soundcloud",
            "bandcamp",
            "piped",
            "slider-kz",
        ),
        default=list(DEFAULT_AUDIO_PROVIDERS),
        help="Sırayla denenecek ses sağlayıcıları",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=DEFAULT_COOKIE_FILE,
        help=(
            "Netscape biçimindeki YouTube cookie dosyası. Dosyanın varlığı "
            "tek başına cookie kullanımını açmaz; --cookie-mode belirler."
        ),
    )
    parser.add_argument(
        "--cookie-mode",
        choices=("auto", "always", "never"),
        default=DEFAULT_COOKIE_MODE,
        help=(
            "auto: ilk tur cookiesiz, sonraki tur cookie; "
            "always: her zaman cookie; never: cookie kullanma"
        ),
    )
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--proxy",
        type=validate_proxy_url,
        default=DEFAULT_PROXY_URL if DEFAULT_PROXY_ENABLED else None,
        help=(
            "Sabit ağ proxy'si. Verilmezse kodun üst kısmındaki proxy ayarı "
            "kullanılır."
        ),
    )
    proxy_group.add_argument(
        "--no-proxy",
        dest="proxy",
        action="store_const",
        const=None,
        help="Bu çalıştırmada proxy kullanımını kapat",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--preload",
        action="store_true",
        help="İndirme URL'lerini önceden çözerek indirme aşamasını hızlandır",
    )
    parser.add_argument(
        "--only-verified",
        action="store_true",
        help="Yalnızca spotDL tarafından güçlü eşleşme olarak doğrulanan sonuçları indir",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="İndirmeden Spotify metadata manifesti oluştur",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Ana spotDL manifesti (varsayılan: çıktı/playlist.spotdl)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Spotify playlist metadata manifestini yeniden oluştur",
    )
    parser.add_argument(
        "--passes",
        type=int,
        choices=range(1, 5),
        default=DEFAULT_PASSES,
        metavar="1-4",
        help=(
            "Kurtarma aşaması sayısı: 1=SoundCloud, 2=farklı SoundCloud "
            "yüklemeleri, 3=Bandcamp, 4=YouTube"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Bu çalıştırmada en fazla N eksik parçayı dene (tanılama için)",
    )
    parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Eksik spotDL/FFmpeg/Deno bileşenlerini otomatik kurma",
    )
    parser.add_argument(
        "--source-key",
        default=None,
        help="SQLite playlist kaydı için özel kaynak anahtarı (discover modu kullanır)",
    )
    parser.add_argument(
        "--library-db",
        type=Path,
        default=DEFAULT_LIBRARY_DB,
        help="Global SQLite müzik arşivi",
    )
    parser.add_argument(
        "--playlist-dir",
        type=Path,
        default=DEFAULT_PLAYLIST_DIR,
        help="M3U8 playlistlerinin yazılacağı klasör",
    )
    parser.add_argument(
        "--offline-manifest",
        action="store_true",
        help="Manifest varsa Spotify'a yeniden bakmadan onu kullan",
    )
    return parser.parse_args()


def manifest_path_for_url(url: str) -> Path:
    match = re.search(r"/(playlist|album|track)/([A-Za-z0-9]+)", url)
    if match:
        return DEFAULT_MANIFEST_DIR / f"{match.group(1)}-{match.group(2)}.spotdl"
    return DEFAULT_MANIFEST_DIR / "spotify-source.spotdl"


def main() -> int:
    enable_windows_ansi()
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        Console.error(bi("--limit en az 1 olmalıdır.", "--limit must be at least 1."))
        return 2
    if args.manifest is None:
        args.manifest = manifest_path_for_url(args.spotify_url)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "library").mkdir(parents=True, exist_ok=True)
    (args.output / ".incoming").mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.playlist_dir.mkdir(parents=True, exist_ok=True)

    if args.proxy:
        try:
            args.proxy = validate_proxy_url(args.proxy)
        except argparse.ArgumentTypeError as exc:
            Console.error(bi(f"Proxy adresi geçersiz: {exc}", f"Invalid proxy URL: {exc}"))
            return 2

    if args.cookie_mode != "never":
        if not args.cookie_file or not args.cookie_file.is_file():
            Console.error(bi(f"Cookie dosyası bulunamadı: {args.cookie_file}", f"Cookie file not found: {args.cookie_file}"))
            return 2
        valid, detail = validate_netscape_cookie_file(args.cookie_file)
        if not valid:
            Console.error(bi(f"Cookie dosyası geçersiz: {detail}", f"Invalid cookie file: {detail}"))
            return 2
        Console.success(detail)

    print(f"\n{APP_NAME} - {bi('akıllı arşiv/senkron motoru', 'smart library/sync engine')} — mebularts\n", flush=True)
    Console.info(bi(f"Kaynak: {args.source_key or args.spotify_url}", f"Source: {args.source_key or args.spotify_url}"))
    Console.info(bi(f"Global arşiv: {args.library_db.resolve()}", f"Global library: {args.library_db.resolve()}"))
    Console.info(bi(f"Ses kütüphanesi: {(args.output / 'library').resolve()}", f"Audio library: {(args.output / 'library').resolve()}"))

    if args.proxy:
        parsed_proxy = urlparse(args.proxy)
        Console.success(bi(
            "Sabit ağ proxy'si kullanılacak: " + f"{parsed_proxy.scheme}://{parsed_proxy.hostname}:{parsed_proxy.port}",
            "Using configured network proxy: " + f"{parsed_proxy.scheme}://{parsed_proxy.hostname}:{parsed_proxy.port}",
        ))
    else:
        Console.info(bi("Proxy kullanımı kapalı.", "Proxy disabled."))

    auto_install = not args.no_auto_install
    started = time.perf_counter()
    # Yeni şarkıları görebilmek için varsayılan davranış her sync'te Spotify
    # manifestini yenilemektir. --offline-manifest yalnızca hızlı/offline tekrar içindir.
    should_scan = not args.offline_manifest or not args.manifest.exists() or args.refresh
    spotdl_ready = False
    if should_scan:
        if not ensure_spotdl(auto_install, args.proxy):
            return 2
        spotdl_ready = True
        action = bi("yenileniyor", "is being refreshed") if args.manifest.exists() else bi("oluşturuluyor", "is being created")
        Console.info(bi(f"Spotify metadata manifesti {action}...", f"Spotify metadata manifest {action}..."))
        return_code = run_child(build_scan_command(args), args.proxy)
        if return_code != 0:
            Console.error(bi(f"Manifest işlemi {return_code} çıkış koduyla sonlandı.", f"Manifest operation ended with exit code {return_code}."))
            return return_code
        if args.scan_only:
            Console.success(bi(f"Manifest hazır: {args.manifest.resolve()}", f"Manifest ready: {args.manifest.resolve()}"))
            return 0
    else:
        Console.info(bi("Offline manifest modu: mevcut Spotify manifesti kullanılıyor.", "Offline manifest mode: using existing Spotify manifest."))

    try:
        tracks = load_manifest(args.manifest)
    except RuntimeError as exc:
        Console.error(str(exc))
        return 2
    if not tracks:
        Console.error(bi("Manifest içinde parça bulunamadı.", "No tracks found in manifest."))
        return 2

    with Library(args.library_db) as library:
        library.register_manifest(tracks, args.source_key or args.spotify_url)
        existing = audio_files_by_identity(args.output, tracks, library)
        initial_pending = pending_tracks(tracks, existing)
        initial_pending_identities = {track_identity(track) for track in initial_pending}
        target_identities: set[str] | None = None
        if args.limit:
            selected = initial_pending[: args.limit]
            target_identities = {track_identity(track) for track in selected}
            Console.info(bi(
                f"Tanılama limiti: seçilen {len(target_identities)} parça bütün kurtarma aşamalarında izlenecek.",
                f"Diagnostic limit: {len(target_identities)} selected tracks will be followed through all recovery stages.",
            ))

        Console.info(bi(
            f"Durum: {len(existing)}/{len(tracks)} arşivde mevcut, {len(initial_pending)} benzersiz parça eksik.",
            f"Status: {len(existing)}/{len(tracks)} already in library, {len(initial_pending)} unique tracks missing.",
        ))
        if not initial_pending:
            m3u8 = write_m3u8(args.playlist_dir, tracks, existing)
            Console.success(bi(f"Yeni parça yok. Playlist güncellendi: {m3u8}", f"No new tracks. Playlist updated: {m3u8}"))
            return 0

        # Ağır indirme motorlarını yalnızca gerçekten yeni/eksik parça varsa hazırla.
        if not spotdl_ready and not ensure_spotdl(auto_install, args.proxy):
            return 2
        if not ensure_ytdlp_engine(auto_install, args.proxy):
            return 2
        if not ensure_runtime(auto_install, args.proxy):
            return 2

        final_return_code = 0

        existing = audio_files_by_identity(args.output, tracks, library)
        pending = filter_target_tracks(tracks, existing, target_identities)
        if pending and args.passes >= 1:
            final_return_code = run_spotdl_stage(
                args, pending, args.output / "pending-soundcloud-primary.spotdl",
                PRIMARY_SOUNDCLOUD_PROVIDERS, args.threads, args.proxy, 1,
                bi("SoundCloud hızlı tur", "SoundCloud fast pass"),
            )
            audio_files_by_identity(args.output, tracks, library, "soundcloud")
            if final_return_code in {130, -signal.SIGINT}:
                return final_return_code

        if args.passes >= 2:
            final_return_code = run_soundcloud_alternative_rounds(
                args, tracks, target_identities, library
            )
            if final_return_code in {130, -signal.SIGINT}:
                return final_return_code

        if args.passes >= 3:
            existing = audio_files_by_identity(args.output, tracks, library)
            pending = filter_target_tracks(tracks, existing, target_identities)
            if pending:
                final_return_code = run_spotdl_stage(
                    args, pending, args.output / "pending-bandcamp.spotdl",
                    BANDCAMP_FALLBACK_PROVIDERS, min(6, args.threads), args.proxy,
                    30, bi("Bandcamp kurtarma turu", "Bandcamp recovery pass"),
                )
                audio_files_by_identity(args.output, tracks, library, "bandcamp")
                if final_return_code in {130, -signal.SIGINT}:
                    return final_return_code

        if args.passes >= 4:
            existing = audio_files_by_identity(args.output, tracks, library)
            pending = filter_target_tracks(tracks, existing, target_identities)
            if pending:
                youtube_proxy = None if DEFAULT_RETRY_WITHOUT_PROXY else args.proxy
                use_cookies = bool(
                    args.cookie_file and args.cookie_mode in {"always", "auto"}
                )
                final_return_code = run_spotdl_stage(
                    args, pending, args.output / "pending-youtube.spotdl",
                    YOUTUBE_FALLBACK_PROVIDERS, min(4, args.threads), youtube_proxy,
                    40, bi("YouTube son kurtarma turu", "YouTube final recovery pass"), use_cookies=use_cookies,
                )
                audio_files_by_identity(args.output, tracks, library, "youtube")
                if final_return_code in {130, -signal.SIGINT}:
                    return final_return_code

        existing = audio_files_by_identity(args.output, tracks, library)
        remaining = pending_tracks(tracks, existing)
        pending_path = args.output / "pending.spotdl"
        write_json(pending_path, remaining)
        m3u8_path = write_m3u8(args.playlist_dir, tracks, existing)
        completed_new = initial_pending_identities & set(existing.keys())
        export_path, export_linked, export_failed = create_apple_music_export(
            PROJECT_ROOT / "exports" / "apple-music",
            tracks,
            completed_new,
            existing,
        )
        elapsed = time.perf_counter() - started

        Console.success(bi(f"M3U8 oluşturuldu: {m3u8_path}", f"M3U8 created: {m3u8_path}"))
        if export_path:
            Console.success(bi(
                f"Apple Music yeni gelenler: {export_path} ({export_linked} hardlink)",
                f"Apple Music new arrivals: {export_path} ({export_linked} hardlinks)",
            ))
            if export_failed:
                Console.warning(bi(
                    f"{export_failed} dosyada hardlink oluşturulamadı; NEW_TRACKS.txt içine yollar yazıldı.",
                    f"Could not hardlink {export_failed} files; original paths were written to NEW_TRACKS.txt.",
                ))
        Console.info(bi(
            f"Son durum: {len(existing)}/{len(tracks)} mevcut, {len(remaining)} eksik.",
            f"Final status: {len(existing)}/{len(tracks)} available, {len(remaining)} missing.",
        ))
        if remaining:
            Console.warning(bi(f"Eksik kayıt listesi: {pending_path.resolve()}", f"Missing-track list: {pending_path.resolve()}"))
        Console.success(bi(f"İşlem {elapsed:.1f} saniyede tamamlandı.", f"Completed in {elapsed:.1f} seconds."))
        return 0 if not remaining else (final_return_code or 1)

if __name__ == "__main__":
    raise SystemExit(main())

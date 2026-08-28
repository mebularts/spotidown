#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(PROJECT_ROOT / ".env")

from spotidown_core import Library, bi, import_legacy, language, set_language
from spotidown_core import engine
from spotidown_core.discover import (
    SpotifyApiError,
    access_token,
    discover_new_track_urls,
    filter_not_downloaded,
    resolve_credentials,
    save_urls_with_spotdl,
)

DEFAULT_PLAYLIST_URL = engine.DEFAULT_PLAYLIST_URL
DEFAULT_DB = PROJECT_ROOT / "data" / "library.sqlite"
DEFAULT_OUTPUT = PROJECT_ROOT / "downloads"
DEFAULT_MANIFESTS = PROJECT_ROOT / "data" / "manifests"
SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"

BANNER = r"""
M   M EEEEE BBBB  U   U L      A    RRRR  TTTTT SSSS
MM MM E     B   B U   U L     A A   R   R   T   S
M M M EEEE  BBBB  U   U L    AAAAA  RRRR    T   SSSS
M   M E     B   B U   U L    A   A  R R     T      S
M   M EEEEE BBBB   UUU  LLLLL A   A R  RR    T   SSSS

 ())) SPOTIFY   [>] YOUTUBE   ~~~ SOUNDCLOUD   ♪ APPLE MUSIC
                         S P O T I D O W N
""".strip("\n")


def load_settings() -> dict[str, object]:
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(**updates: object) -> None:
    payload = load_settings()
    payload.update(updates)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_saved_language() -> None:
    if os.environ.get("SPOTIDOWN_LANG"):
        return
    saved = str(load_settings().get("language") or "").strip()
    if saved:
        set_language(saved)


apply_saved_language()


def run_engine(argv: list[str]) -> int:
    old = sys.argv[:]
    try:
        sys.argv = ["spotidown.py", *argv]
        return engine.main()
    finally:
        sys.argv = old


def format_size(value: int) -> str:
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def show_stats(db: Path = DEFAULT_DB) -> int:
    with Library(db) as library:
        stats = library.stats()
    print("\n" + bi("SpotiDown Arşiv İstatistikleri", "SpotiDown Library Statistics"))
    print("-" * 38)
    labels = [
        (bi("Toplam kayıt", "Total records"), stats.total),
        (bi("İndirilen", "Downloaded"), stats.downloaded),
        (bi("Eksik/yalnız metadata", "Missing / metadata only"), stats.missing),
        (bi("Playlist/kaynak", "Playlists / sources"), stats.playlists),
        (bi("Disk kullanımı", "Disk usage"), format_size(stats.bytes_on_disk)),
        ("SoundCloud", stats.soundcloud),
        ("Bandcamp", stats.bandcamp),
        ("YouTube", stats.youtube),
        (bi("Diğer/legacy", "Other / legacy"), stats.other),
    ]
    for label, value in labels:
        print(f"{label:<23}: {value}")
    return 0


def import_archive(path: Path, db: Path = DEFAULT_DB) -> int:
    if not path.exists():
        print(bi(f"[HATA] Klasör bulunamadı: {path}", f"[ERROR] Folder not found: {path}"))
        return 2
    with Library(db) as library:
        result = import_legacy(library, path)
    print("\n" + bi("Eski arşiv taraması tamamlandı", "Legacy archive scan completed"))
    print("-" * 38)
    print(f"{bi('Bulunan ses dosyası', 'Audio files found'):<23}: {result['audio_files']}")
    print(f"{bi('Manifest/playlist', 'Manifests/playlists'):<23}: {result['manifests']}")
    print(f"{bi('Eşleşen toplam', 'Total matched'):<23}: {result['matched']}")
    print(f"{bi('Track-ID eşleşmesi', 'Track-ID matches'):<23}: {result['id_matched']}")
    print(f"{bi('Eski sıra eşleşmesi', 'Legacy position matches'):<23}: {result['position_matched']}")
    print("\n" + bi(
        "Eşleşen dosyalar taşınmadı; yalnızca SQLite arşivine kaydedildi.",
        "Matched files were not moved; only their paths were registered in SQLite.",
    ))
    return 0


def discover_and_sync(args: argparse.Namespace) -> int:
    client_id, client_secret, source = resolve_credentials(PROJECT_ROOT)
    if not client_id or not client_secret:
        print(bi(
            "[HATA] Yeni çıkanlar modu için Spotify Web API bilgileri gerekli.",
            "[ERROR] Spotify Web API credentials are required for New Releases mode.",
        ))
        print(bi(
            "SpotiDown/.env dosyasına SPOTIFY_CLIENT_ID ve SPOTIFY_CLIENT_SECRET ekleyin.",
            "Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to SpotiDown/.env.",
        ))
        return 2

    print(bi(
        f"[BİLGİ] Spotify API kimliği bulundu: {source} (değerler gizlendi).",
        f"[INFO] Spotify API credentials found: {source} (values hidden).",
    ))
    try:
        token = access_token(client_id, client_secret)
        discovered = discover_new_track_urls(
            token,
            market=args.market,
            days=args.days,
            pages=args.pages,
            max_tracks=args.max_tracks,
        )
    except SpotifyApiError as exc:
        print(bi(f"[HATA] {exc}", f"[ERROR] {exc}"))
        return 2

    with Library(args.library_db) as library:
        new_items = filter_not_downloaded(library, discovered)

    print(bi(
        f"[BİLGİ] {args.market.upper()} için bulunan: {len(discovered)} parça",
        f"[INFO] Found for {args.market.upper()}: {len(discovered)} tracks",
    ))
    print(bi(
        f"[BİLGİ] Arşivde olmayan: {len(new_items)} parça",
        f"[INFO] Not in library: {len(new_items)} tracks",
    ))
    if not new_items:
        print(bi("[TAMAM] Yeni indirilecek parça yok.", "[OK] No new tracks to process."))
        return 0

    if args.limit and args.limit > 0:
        new_items = new_items[: args.limit]

    if not engine.ensure_spotdl(not args.no_auto_install, args.proxy):
        return 2

    label = bi(f"Yeni Çıkanlar {args.market.upper()}", f"New Releases {args.market.upper()}")
    manifest = DEFAULT_MANIFESTS / f"new-releases-{args.market.lower()}.spotdl"
    urls = [str(item["url"]) for item in new_items]
    try:
        saved = save_urls_with_spotdl(
            urls,
            manifest,
            threads=args.threads,
            proxy=args.proxy,
            playlist_name=label,
        )
    except RuntimeError as exc:
        print(bi(f"[HATA] {exc}", f"[ERROR] {exc}"))
        return 2
    print(bi(
        f"[BİLGİ] İndirme manifesti hazır: {len(saved)} parça",
        f"[INFO] Download manifest ready: {len(saved)} tracks",
    ))

    engine_args = [
        "--manifest", str(manifest),
        "--offline-manifest",
        "--source-key", f"spotify:new-releases:{args.market.upper()}",
        "--library-db", str(args.library_db),
        "--output", str(args.output),
        "--passes", str(args.passes),
        "--threads", str(args.threads),
        "--cookie-mode", args.cookie_mode,
    ]
    if args.proxy:
        engine_args += ["--proxy", args.proxy]
    else:
        engine_args += ["--no-proxy"]
    if args.no_auto_install:
        engine_args.append("--no-auto-install")
    return run_engine(engine_args)


def read_last_playlist() -> str:
    value = str(load_settings().get("last_playlist") or "")
    if re.match(r"https://open\.spotify\.com/(playlist|album|track)/", value):
        return value
    return DEFAULT_PLAYLIST_URL


def save_last_playlist(url: str) -> None:
    save_settings(last_playlist=url)


def choose_language(force: bool = False) -> str:
    if not force and str(load_settings().get("language") or "").strip():
        lang = set_language(str(load_settings().get("language")))
        return lang
    print("\nDil / Language")
    print("1) Türkçe")
    print("2) English")
    raw = input("\nSeçim / Choice [1-2]: ").strip()
    lang = set_language("en" if raw == "2" else "tr")
    save_settings(language=lang)
    return lang


def press_enter() -> None:
    input("\n" + bi("Ana menüye dönmek için Enter...", "Press Enter to return to the main menu..."))


def show_banner() -> None:
    print("\n" + BANNER)
    print("=" * 66)
    print(bi(" mebularts tarafından geliştirildi", " built by mebularts"))
    print("=" * 66)


def interactive_menu() -> int:
    if not str(load_settings().get("language") or "").strip():
        choose_language(force=True)

    while True:
        show_banner()
        print("1) " + bi("Playlist / albüm / parça senkronize et", "Sync playlist / album / track"))
        print("2) " + bi("Yeni çıkanları tara ve senkronize et", "Discover and sync new releases"))
        print("3) " + bi("Eski müzik arşivini içe aktar", "Import legacy music archive"))
        print("4) " + bi("Arşiv istatistikleri", "Library statistics"))
        print("5) " + bi("Dil değiştir", "Change language"))
        print("6) " + bi("Çıkış", "Exit"))
        choice = input("\n" + bi("Seçim [1-6]: ", "Choice [1-6]: ")).strip()

        if choice == "1":
            default = read_last_playlist()
            url = input(bi(f"Spotify URL [{default}]: ", f"Spotify URL [{default}]: ")).strip() or default
            save_last_playlist(url)
            run_engine([url])
            press_enter()
            continue

        if choice == "2":
            market = input(bi("Spotify market [TR]: ", "Spotify market [TR]: ")).strip().upper() or "TR"
            ns = argparse.Namespace(
                market=market, days=14, pages=5, max_tracks=150, limit=None,
                library_db=DEFAULT_DB, output=DEFAULT_OUTPUT, passes=4,
                threads=8, cookie_mode="never", proxy=os.environ.get("SPOTIDOWN_PROXY"),
                no_auto_install=False,
            )
            discover_and_sync(ns)
            press_enter()
            continue

        if choice == "3":
            raw = input(bi(
                f"Eski arşiv klasörü [{PROJECT_ROOT}]: ",
                f"Legacy archive folder [{PROJECT_ROOT}]: ",
            )).strip()
            import_archive(Path(raw) if raw else PROJECT_ROOT)
            press_enter()
            continue

        if choice == "4":
            show_stats()
            press_enter()
            continue

        if choice == "5":
            choose_language(force=True)
            continue

        if choice == "6":
            print("\n" + bi("Görüşürüz. — mebularts", "See you. — mebularts"))
            return 0

        print("\n" + bi("Geçersiz seçim.", "Invalid choice."))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotidown.py",
        description=bi(
            "Spotify metadata senkronizasyonu + global yerel müzik arşivi",
            "Spotify metadata sync + global local music library",
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help=bi("Spotify URL'sini senkronize et", "Sync a Spotify URL"))
    sync.add_argument("spotify_url")
    sync.add_argument("engine_args", nargs=argparse.REMAINDER)

    disc = sub.add_parser("discover", help=bi("Konuma göre yeni çıkanları tara", "Discover new releases by market"))
    disc.add_argument("--market", default="TR")
    disc.add_argument("--days", type=int, default=14)
    disc.add_argument("--pages", type=int, default=5)
    disc.add_argument("--max-tracks", type=int, default=150)
    disc.add_argument("--limit", type=int, default=None)
    disc.add_argument("--library-db", type=Path, default=DEFAULT_DB)
    disc.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    disc.add_argument("--passes", type=int, choices=range(1, 5), default=4)
    disc.add_argument("--threads", type=int, choices=range(1, 17), default=8)
    disc.add_argument("--cookie-mode", choices=("never", "auto", "always"), default="never")
    disc.add_argument("--proxy", default=os.environ.get("SPOTIDOWN_PROXY"))
    disc.add_argument("--no-auto-install", action="store_true")

    imp = sub.add_parser("import-legacy", help=bi("Eski arşivi SQLite'a tanıt", "Register a legacy archive in SQLite"))
    imp.add_argument("path", nargs="?", type=Path, default=PROJECT_ROOT)
    imp.add_argument("--library-db", type=Path, default=DEFAULT_DB)

    stats = sub.add_parser("stats", help=bi("Arşiv istatistiklerini göster", "Show library statistics"))
    stats.add_argument("--library-db", type=Path, default=DEFAULT_DB)

    sub.add_parser("menu", help=bi("Etkileşimli menü", "Interactive menu"))
    return parser


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return interactive_menu()

    if argv[0] in {"-h", "--help"}:
        build_parser().print_help()
        return 0
    if argv[0].startswith("http://") or argv[0].startswith("https://") or argv[0].startswith("--"):
        if argv[0].startswith("http"):
            save_last_playlist(argv[0])
        return run_engine(argv)

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sync":
        save_last_playlist(args.spotify_url)
        extra = [x for i, x in enumerate(args.engine_args) if not (i == 0 and x == "--")]
        return run_engine([args.spotify_url, *extra])
    if args.command == "discover":
        return discover_and_sync(args)
    if args.command == "import-legacy":
        return import_archive(args.path, args.library_db)
    if args.command == "stats":
        return show_stats(args.library_db)
    if args.command == "menu":
        return interactive_menu()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

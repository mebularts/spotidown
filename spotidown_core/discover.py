from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .library import Library
from .i18n import bi

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyApiError(RuntimeError):
    pass


def _request_json(url: str, headers: dict[str, str], data: bytes | None = None) -> dict:
    request = Request(url, headers=headers, data=data)
    try:
        with urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SpotifyApiError(f"Spotify API HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SpotifyApiError(bi(f"Spotify API erişim hatası: {exc}", f"Spotify API access error: {exc}")) from exc
    if not isinstance(payload, dict):
        raise SpotifyApiError(bi("Spotify API beklenmeyen yanıt döndürdü.", "Spotify API returned an unexpected response."))
    return payload


def resolve_credentials(project_root: Path) -> tuple[str | None, str | None, str]:
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if cid and secret:
        return cid, secret, "environment/.env"

    candidates = [
        Path.home() / ".spotdl" / "config.json",
        Path.home() / ".config" / "spotdl" / "config.json",
    ]
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(payload, dict):
            continue
        cid = str(payload.get("client_id") or "").strip()
        secret = str(payload.get("client_secret") or "").strip()
        if cid and secret:
            return cid, secret, str(path)
    return None, None, "none"


def access_token(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = _request_json(
        TOKEN_URL,
        {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=b"grant_type=client_credentials",
    )
    token = str(payload.get("access_token") or "")
    if not token:
        raise SpotifyApiError(bi("Spotify access token alınamadı.", "Could not obtain a Spotify access token."))
    return token


def _get(token: str, endpoint: str, params: dict[str, object]) -> dict:
    url = f"{API}{endpoint}?{urlencode(params)}"
    return _request_json(url, {"Authorization": f"Bearer {token}"})


def _parse_release_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def discover_new_track_urls(
    token: str,
    market: str = "TR",
    days: int = 14,
    pages: int = 5,
    max_tracks: int = 150,
) -> list[dict[str, object]]:
    market = market.upper()
    cutoff = date.today() - timedelta(days=max(1, days))
    albums: dict[str, dict] = {}
    # 2026 Spotify Search limiti istek başına 10 sonuç; sayfalama burada yapılır.
    for page in range(max(1, pages)):
        payload = _get(
            token,
            "/search",
            {"q": "tag:new", "type": "album", "market": market, "limit": 10, "offset": page * 10},
        )
        container = payload.get("albums") or {}
        items = container.get("items") if isinstance(container, dict) else []
        if not isinstance(items, list) or not items:
            break
        for album in items:
            if not isinstance(album, dict):
                continue
            album_id = str(album.get("id") or "")
            released = _parse_release_date(str(album.get("release_date") or ""))
            if not album_id or (released and released < cutoff):
                continue
            albums[album_id] = album

    tracks: list[dict[str, object]] = []
    seen: set[str] = set()
    for album_id, album in albums.items():
        offset = 0
        while len(tracks) < max_tracks:
            payload = _get(
                token,
                f"/albums/{album_id}/tracks",
                {"market": market, "limit": 50, "offset": offset},
            )
            items = payload.get("items") or []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("id") or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                artists = item.get("artists") or []
                artist_names = [str(a.get("name") or "") for a in artists if isinstance(a, dict)]
                tracks.append(
                    {
                        "spotify_id": sid,
                        "url": f"https://open.spotify.com/track/{sid}",
                        "name": str(item.get("name") or ""),
                        "artists": artist_names,
                        "duration": int(item.get("duration_ms") or 0) // 1000,
                        "album": str(album.get("name") or ""),
                        "release_date": str(album.get("release_date") or ""),
                    }
                )
                if len(tracks) >= max_tracks:
                    break
            if len(items) < 50 or len(tracks) >= max_tracks:
                break
            offset += len(items)
        if len(tracks) >= max_tracks:
            break
    return tracks


def save_urls_with_spotdl(
    urls: list[str],
    manifest_path: Path,
    threads: int = 8,
    proxy: str | None = None,
    playlist_name: str = "New Releases",
) -> list[dict[str, object]]:
    if not urls:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("[]\n", encoding="utf-8")
        return []

    merged: list[dict[str, object]] = []
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if proxy:
        env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy})
    with tempfile.TemporaryDirectory(prefix="spotidown-discover-") as temp:
        temp_root = Path(temp)
        for chunk_index in range(0, len(urls), 20):
            chunk = urls[chunk_index:chunk_index + 20]
            chunk_file = temp_root / f"chunk-{chunk_index // 20}.spotdl"
            command = [
                sys.executable, "-m", "spotdl", "save", *chunk,
                "--save-file", str(chunk_file), "--threads", str(threads), "--simple-tui",
            ]
            completed = subprocess.run(command, env=env, check=False)
            if completed.returncode != 0:
                raise RuntimeError(bi(f"spotDL discover manifesti oluşturamadı (kod {completed.returncode}).", f"spotDL could not create the discovery manifest (code {completed.returncode})."))
            try:
                payload = json.loads(chunk_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(bi(f"spotDL discover manifesti okunamadı: {exc}", f"Could not read the spotDL discovery manifest: {exc}")) from exc
            if isinstance(payload, list):
                merged.extend(item for item in payload if isinstance(item, dict))

    # Aynı track-id'yi bir kez tut ve yapay liste bilgisini ekle.
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in merged:
        sid = str(item.get("song_id") or "")
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        unique.append(item)
    for pos, item in enumerate(unique, start=1):
        item["list_name"] = playlist_name
        item["list_position"] = pos
        item["list_length"] = len(unique)
        item["list_url"] = None
    manifest_path.write_text(
        json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return unique


def filter_not_downloaded(library: Library, discovered: list[dict[str, object]]) -> list[dict[str, object]]:
    return [item for item in discovered if not library.has_spotify_id(str(item.get("spotify_id") or ""))]

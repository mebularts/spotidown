from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".flac", ".ogg", ".wav"}
SPOTIFY_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{22})(?![A-Za-z0-9])")
POSITION_RE = re.compile(r"^0*(\d+)\s+-\s+")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: object) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def spotify_id(track: dict[str, object]) -> str | None:
    for key in ("song_id", "spotify_id", "track_id", "id"):
        value = str(track.get(key) or "").strip()
        if re.fullmatch(r"[A-Za-z0-9]{22}", value):
            return value
    url = str(track.get("url") or track.get("spotify_url") or "")
    match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]{22})", url)
    return match.group(1) if match else None


def track_identity(track: dict[str, object]) -> str:
    sid = spotify_id(track)
    if sid:
        return f"spotify:{sid}"
    isrc = str(track.get("isrc") or "").strip().upper()
    if isrc:
        return f"isrc:{isrc}"
    artists = track.get("artists") or track.get("artist") or ""
    if isinstance(artists, list):
        artists = ", ".join(str(x) for x in artists)
    title = track.get("name") or track.get("title") or ""
    duration = int(float(track.get("duration") or 0))
    return f"fallback:{normalize_text(artists)}|{normalize_text(title)}|{duration}"


def track_artist(track: dict[str, object]) -> str:
    artists = track.get("artists") or track.get("artist") or ""
    if isinstance(artists, list):
        return ", ".join(str(x) for x in artists if x)
    return str(artists)


def safe_filename_component(value: object, fallback: str = "Unknown") -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:140] or fallback


def pretty_audio_filename(track: dict[str, object], suffix: str) -> str:
    artist = safe_filename_component(track_artist(track), "Unknown Artist")
    title = safe_filename_component(track.get("name") or track.get("title"), "Unknown Track")
    return f"{artist} - {title}{suffix.lower()}"


@dataclass
class Stats:
    total: int
    downloaded: int
    missing: int
    bytes_on_disk: int
    playlists: int
    soundcloud: int
    bandcamp: int
    youtube: int
    other: int


class Library:
    def __init__(self, db_path: Path):
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                identity TEXT PRIMARY KEY,
                spotify_id TEXT,
                isrc TEXT,
                artist TEXT,
                title TEXT,
                album TEXT,
                duration INTEGER,
                local_path TEXT,
                file_size INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'seen',
                source TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                downloaded_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_spotify_id
                ON tracks(spotify_id) WHERE spotify_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
            CREATE INDEX IF NOT EXISTS idx_tracks_isrc
                ON tracks(isrc) WHERE isrc IS NOT NULL;

            CREATE TABLE IF NOT EXISTS playlist_items (
                source_url TEXT NOT NULL,
                identity TEXT NOT NULL,
                playlist_name TEXT,
                position INTEGER,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(source_url, identity, position)
            );
            CREATE INDEX IF NOT EXISTS idx_playlist_items_source
                ON playlist_items(source_url, position);

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT,
                playlist_name TEXT,
                total_tracks INTEGER,
                already_present INTEGER,
                requested INTEGER,
                remaining INTEGER,
                started_at TEXT,
                finished_at TEXT
            );
            """
        )
        self.conn.commit()

    def upsert_track(self, track: dict[str, object], source_url: str | None = None) -> str:
        identity = track_identity(track)
        now = utcnow()
        sid = spotify_id(track)
        isrc = str(track.get("isrc") or "").strip().upper() or None
        title = str(track.get("name") or track.get("title") or "").strip()
        album = str(track.get("album_name") or track.get("album") or "").strip()
        try:
            duration = int(float(track.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        self.conn.execute(
            """
            INSERT INTO tracks(identity, spotify_id, isrc, artist, title, album, duration,
                               first_seen_at, last_seen_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity) DO UPDATE SET
                spotify_id=COALESCE(excluded.spotify_id, tracks.spotify_id),
                isrc=COALESCE(excluded.isrc, tracks.isrc),
                artist=CASE WHEN excluded.artist <> '' THEN excluded.artist ELSE tracks.artist END,
                title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE tracks.title END,
                album=CASE WHEN excluded.album <> '' THEN excluded.album ELSE tracks.album END,
                duration=CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE tracks.duration END,
                last_seen_at=excluded.last_seen_at
            """,
            (identity, sid, isrc, track_artist(track), title, album, duration, now, now),
        )
        if source_url:
            name = str(track.get("list_name") or "").strip() or None
            try:
                pos = int(track.get("list_position") or track.get("position") or 0)
            except (TypeError, ValueError):
                pos = 0
            self.conn.execute(
                """
                INSERT OR REPLACE INTO playlist_items(
                    source_url, identity, playlist_name, position, last_seen_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (source_url, identity, name, pos, now),
            )
        return identity

    def register_manifest(self, tracks: Iterable[dict[str, object]], source_url: str | None = None) -> None:
        if source_url:
            self.conn.execute("DELETE FROM playlist_items WHERE source_url=?", (source_url,))
        for track in tracks:
            self.upsert_track(track, source_url)
        self.conn.commit()

    def register_file(self, track: dict[str, object], path: Path, source: str | None = None) -> None:
        identity = self.upsert_track(track)
        resolved = path.resolve()
        try:
            size = resolved.stat().st_size
        except OSError:
            size = 0
        now = utcnow()
        self.conn.execute(
            """
            UPDATE tracks
            SET local_path=?, file_size=?, status='downloaded',
                source=COALESCE(source, ?), downloaded_at=COALESCE(downloaded_at, ?),
                last_seen_at=?
            WHERE identity=?
            """,
            (str(resolved), size, source, now, now, identity),
        )
        self.conn.commit()

    def mark_missing(self, identity: str) -> None:
        self.conn.execute(
            "UPDATE tracks SET status='missing', local_path=NULL, file_size=0 WHERE identity=?",
            (identity,),
        )
        self.conn.commit()

    def path_for_track(self, track: dict[str, object]) -> Path | None:
        identity = track_identity(track)
        row = self.conn.execute(
            "SELECT local_path FROM tracks WHERE identity=?", (identity,)
        ).fetchone()
        if row and row["local_path"]:
            path = Path(row["local_path"])
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                return path
            self.mark_missing(identity)

        # Spotify bazen aynı kaydı single/albüm altında farklı track ID ile sunar.
        # ISRC aynıysa ikinci fiziksel kopyayı oluşturmadan mevcut dosyayı kullan.
        isrc = str(track.get("isrc") or "").strip().upper()
        if isrc:
            duplicate = self.conn.execute(
                """
                SELECT local_path, source FROM tracks
                WHERE isrc=? AND local_path IS NOT NULL AND status='downloaded'
                ORDER BY downloaded_at ASC
                LIMIT 1
                """,
                (isrc,),
            ).fetchone()
            if duplicate and duplicate["local_path"]:
                path = Path(duplicate["local_path"])
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                    self.register_file(track, path, "isrc-dedupe")
                    return path
        return None

    def existing_paths(self, tracks: Iterable[dict[str, object]]) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for track in tracks:
            path = self.path_for_track(track)
            if path:
                result[track_identity(track)] = path
        return result

    def has_spotify_id(self, sid: str) -> bool:
        row = self.conn.execute(
            "SELECT local_path FROM tracks WHERE spotify_id=?", (sid,)
        ).fetchone()
        if not row or not row["local_path"]:
            return False
        path = Path(row["local_path"])
        if path.is_file():
            return True
        self.conn.execute(
            "UPDATE tracks SET status='missing', local_path=NULL, file_size=0 WHERE spotify_id=?",
            (sid,),
        )
        self.conn.commit()
        return False

    def scan_id_filenames(self, root: Path, tracks: Iterable[dict[str, object]], source: str | None = None) -> int:
        by_sid = {spotify_id(t): t for t in tracks if spotify_id(t)}
        count = 0
        if not root.exists():
            return 0
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            match = SPOTIFY_ID_RE.search(path.stem)
            if not match:
                continue
            track = by_sid.get(match.group(1))
            if not track:
                continue
            self.register_file(track, path, source)
            count += 1
        return count

    def beautify_id_files(
        self,
        root: Path,
        tracks: Iterable[dict[str, object]],
        destination_dir: Path,
        source: str | None = None,
    ) -> int:
        """Move temporary track-ID filenames to clean Artist - Title names."""
        by_sid = {spotify_id(t): t for t in tracks if spotify_id(t)}
        destination_dir.mkdir(parents=True, exist_ok=True)
        renamed = 0
        if not root.exists():
            return 0
        for path in list(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            match = SPOTIFY_ID_RE.search(path.stem)
            if not match:
                continue
            track = by_sid.get(match.group(1))
            if not track:
                continue
            desired = destination_dir / pretty_audio_filename(track, path.suffix)
            if path.resolve() == desired.resolve():
                self.register_file(track, path, source)
                continue
            candidate = desired
            counter = 2
            while candidate.exists() and candidate.resolve() != path.resolve():
                # Name collisions stay readable; never expose the Spotify ID.
                candidate = desired.with_name(f"{desired.stem} ({counter}){desired.suffix}")
                counter += 1
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                path.replace(candidate)
            except OSError:
                self.register_file(track, path, source)
                continue
            self.register_file(track, candidate, source)
            renamed += 1
        return renamed

    def stats(self) -> Stats:
        rows = self.conn.execute(
            "SELECT status, local_path, source, file_size FROM tracks"
        ).fetchall()
        downloaded = missing = bytes_on_disk = 0
        sources = {"soundcloud": 0, "bandcamp": 0, "youtube": 0, "other": 0}
        for row in rows:
            path = Path(row["local_path"]) if row["local_path"] else None
            if path and path.is_file():
                downloaded += 1
                try:
                    bytes_on_disk += path.stat().st_size
                except OSError:
                    bytes_on_disk += int(row["file_size"] or 0)
                src = (row["source"] or "other").lower()
                if "soundcloud" in src:
                    sources["soundcloud"] += 1
                elif "bandcamp" in src:
                    sources["bandcamp"] += 1
                elif "youtube" in src:
                    sources["youtube"] += 1
                else:
                    sources["other"] += 1
            else:
                missing += 1
        playlists = self.conn.execute(
            "SELECT COUNT(DISTINCT source_url) FROM playlist_items"
        ).fetchone()[0]
        return Stats(
            total=len(rows), downloaded=downloaded, missing=missing,
            bytes_on_disk=bytes_on_disk, playlists=int(playlists or 0),
            soundcloud=sources["soundcloud"], bandcamp=sources["bandcamp"],
            youtube=sources["youtube"], other=sources["other"],
        )


def _load_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def _metadata_candidates(root: Path) -> list[tuple[Path, list[dict[str, object]], str]]:
    found: list[tuple[Path, list[dict[str, object]], str]] = []
    for path in root.rglob("*.spotdl"):
        payload = _load_json(path)
        if isinstance(payload, list):
            tracks = [x for x in payload if isinstance(x, dict)]
            if tracks:
                name = str(tracks[0].get("list_name") or path.stem)
                found.append((path, tracks, name))
    for path in root.rglob("playlist.json"):
        payload = _load_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
            continue
        name = str(payload.get("playlist") or path.parent.name)
        tracks: list[dict[str, object]] = []
        for item in payload["tracks"]:
            if not isinstance(item, dict):
                continue
            converted = dict(item)
            converted["song_id"] = item.get("spotify_id")
            converted["list_position"] = item.get("position")
            converted["list_name"] = name
            if isinstance(converted.get("artists"), str):
                converted["artists"] = [x.strip() for x in str(converted["artists"]).split(",") if x.strip()]
            tracks.append(converted)
        if tracks:
            found.append((path, tracks, name))
    return found


def import_legacy(library: Library, root: Path) -> dict[str, int]:
    root = root.resolve()
    audio_files = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    by_sid: dict[str, Path] = {}
    by_position: dict[int, list[Path]] = {}
    for path in audio_files:
        sid_match = SPOTIFY_ID_RE.search(path.stem)
        if sid_match:
            by_sid[sid_match.group(1)] = path
        pos_match = POSITION_RE.match(path.name)
        if pos_match:
            by_position.setdefault(int(pos_match.group(1)), []).append(path)

    matched = 0
    id_matched = 0
    position_matched = 0
    manifests = _metadata_candidates(root)
    for manifest_path, tracks, playlist_name in manifests:
        library.register_manifest(tracks, f"legacy:{manifest_path}")
        playlist_norm = normalize_text(playlist_name)
        for track in tracks:
            sid = spotify_id(track)
            candidate: Path | None = by_sid.get(sid or "")
            if candidate:
                id_matched += 1
            else:
                try:
                    pos = int(track.get("list_position") or track.get("position") or 0)
                except (TypeError, ValueError):
                    pos = 0
                options = by_position.get(pos, [])
                if options:
                    title_norm = normalize_text(track.get("name") or track.get("title"))
                    artist_norm = normalize_text(track_artist(track))
                    ranked: list[tuple[int, Path]] = []
                    for option in options:
                        stem = normalize_text(option.stem)
                        parent = normalize_text(option.parent.name)
                        score = 0
                        if title_norm and title_norm in stem:
                            score += 4
                        if artist_norm and any(tok in stem for tok in artist_norm.split()[:2]):
                            score += 2
                        if playlist_norm and playlist_norm == parent:
                            score += 3
                        if manifest_path.parent in option.parents:
                            score += 1
                        ranked.append((score, option))
                    ranked.sort(key=lambda x: x[0], reverse=True)
                    if ranked and ranked[0][0] >= 3:
                        candidate = ranked[0][1]
                        position_matched += 1
            if candidate and candidate.is_file():
                library.register_file(track, candidate, "legacy")
                matched += 1

    # ID içeren ama manifestte olmayan dosyaları da temel metadata ile kaydet.
    for sid, path in by_sid.items():
        if library.has_spotify_id(sid):
            continue
        fake = {"song_id": sid, "name": path.stem, "artists": []}
        library.register_file(fake, path, "filename-import")
        matched += 1
        id_matched += 1

    return {
        "audio_files": len(audio_files),
        "manifests": len(manifests),
        "matched": matched,
        "id_matched": id_matched,
        "position_matched": position_matched,
    }

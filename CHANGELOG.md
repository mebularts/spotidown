# Changelog

## 2.0.0 — 2026-08-28

- Added persistent TR/EN terminal interface.
- Menu now returns after each operation instead of closing the terminal.
- Added SQLite-based global music library and Track-ID deduplication.
- Added ISRC fallback deduplication for the same recording under different Spotify IDs.
- Final audio filenames are now clean `Artist - Title.ext` names; Spotify IDs are temporary only.
- Added automatic Apple Music new-arrivals hardlink batches.
- Added legacy archive import without moving existing files.
- Added market-based new-release discovery.
- Added a single bilingual TR/EN GitHub README, branded `assets/spotidown-banner.png`, MIT license, CI, and one-command GitHub publishing/release script.
- Fixed first-publish handling for an unborn Git `HEAD` and other expected missing-state checks under Windows PowerShell 5.1.
- Added UTF-8/BOM compatibility for Turkish publisher output and `.gitattributes` for predictable line endings.

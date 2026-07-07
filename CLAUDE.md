# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> The codebase, comments, and user-facing strings are in **Turkish**. Match that language when editing comments and CLI output.

## What this is

`hayalet` is a headless (browser-less), pure-Python terminal tool for Dizipal. It bypasses Cloudflare/anti-bot via TLS impersonation, searches series, navigates seasons/episodes, resolves the `m3u8` stream + Turkish subtitle, then either **watches** (browser + hls.js) or **downloads** (ffmpeg mux). It is a Python port of a browser extension that lives in `reference/örnek eklenti/` — the ad blacklist and the media/subtitle heuristics come straight from that extension (`content.js`, `page-inject.js`).

## Commands

Setup (Windows; venv already present in repo):
```bash
cd hayalet
python -m venv venv
venv\Scripts\activate          # PowerShell: venv\Scripts\Activate.ps1
pip install -r requirements.txt
# or: pip install -e .   (installs the `hayalet` console entry point)
```

Run:
```bash
python -m hayalet                                   # interactive menu (recommended)
baslat.bat                                          # Windows launcher (sets UTF-8, uses venv python)

# Non-interactive / automation:
python -m hayalet --search "house of the dragon"    # list results with indices
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action extract
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action watch
python -m hayalet --search "..." --series 1 --season 1 --episodes 1,2,3 --action download --quality 1080
python -m hayalet --domain https://dizipalXXXX.com  # skip the resolver, pin a domain
```

There is **no test suite, linter, or build step**. Verification is manual: run `--action extract` (cheapest, network-only, no player/ffmpeg) to confirm the extraction chain still works end-to-end.

External tools (auto-detected via `shutil.which` + known install paths in `actions.py`):
- **ffmpeg** — required for `download` (must be on PATH).
- A browser — `watch` opens `webbrowser.open` on the local proxy's `/player.html`.

## Architecture

Everything hangs off two shared objects created once in `cli.main()` and threaded through every call:
- **`SessionState`** (`core/session.py`) — the single identity: `base_url`, `cookies`, `user_agent`, `impersonate` profile. The **same** UA/cookies/referer must reach curl-cffi, ffmpeg, and the browser or the CDN returns 403.
- **`Network`** (`core/network.py`) — the only HTTP layer. Uses **curl-cffi** (Chrome TLS/JA3 impersonation), never stdlib `requests`. Retries 403/429/503 as `BlockedError` and syncs cookies back into `SessionState`.

The end-to-end pipeline (all pure Python, no headless browser):

1. **`resolver.py`** — finds the current `dizipalXXXX.com` (cache → `config.KNOWN_DOMAIN` → incrementing scan), writes it to `.cache/domain.json`, sets `session.base_url`.
2. **`catalog.py`** — `search()` (POST `/bg/searchcontent`, needs the `cValue` token scraped from the homepage) and `get_episodes()` (scrapes `/bolum/...-SxE` links). Also owns dubbed↔original **counterpart matching** (`is_dubbed`, `find_counterpart`, `match_episode`).
3. **`extractor.py`** — the crypto chain: episode page → `data-rm-k` encrypted JSON → **`utils.decrypt_rmk`** (CryptoJS AES + PBKDF2-SHA512, params in `config.py`) → iframe → `openPlayer('<playList>')` + embedded Turkish `.vtt` → `source2.php` → real `master.m3u8`. Raises `DeadSourceError` (subclass of `ExtractError`) when the player host is a **dead/parked domain** (CHEQ→RTB→domain-parking, common on "Türkçe Dublaj" embeds) — this is *not* anti-bot, there is genuinely no video behind it.
4. **`merge.py`** — resolves **both** the dubbed and original versions of an episode and combines them into one `MergedStream`: original video + [Turkish-dub audio, original audio] + Turkish subtitle. This is the object `watch`/`download` consume. When the dub source is dead it gracefully falls back to just the original (see also `cli._extract_with_fallback`).
5. **`proxy.py`** — the crux of playback. A local `ThreadingHTTPServer` (`HLSProxy`) that players/ffmpeg connect to on `127.0.0.1`. It refetches every playlist/segment through curl-cffi (solving the 403), rewrites playlist URLs to point back at itself, serves disguised `.jpg` segments as `.ts`, and **injects the Turkish subtitle as an HLS `SUBTITLES` channel** into the master so every player sees it natively. It can also host **synthetic** playlists (`virtual()`) — the multi-audio master built by `merge` — and serves the `/player.html` hls.js watch page (hls.js is bundled locally at `hayalet/assets/hls.min.js`).
6. **`actions.py`** — `watch()` builds a synthetic master, opens the browser at the proxy's player page (hls.js gives audio/subtitle/quality dropdowns). `download()` runs a **single ffmpeg pass** mapping video + every audio track (language/title tagged) + softsubbed Turkish subtitle → `.mp4` (single audio) or `.mkv` (multi-audio or subtitle), with live progress parsed from `ffmpeg -progress`.

Supporting: `m3u8_parser.py` (variant/audio-track parsing via the `m3u8` lib), `menu.py` (questionary interactive prompts), `logs.py`, `utils.py` (crypto + heuristics + `safe_filename`).

## Working on this codebase — key facts

- **`config.py` is the single source of truth** for everything site-specific. When the site changes, you edit config, not code:
  - `RMK_PASSPHRASE` / `RMK_ITERATIONS` / `RMK_KEYSIZE` — the `data-rm-k` decryption params, extracted from the site's `app-dizipals.js`. If the site rotates its bundle, decryption breaks and `RMK_PASSPHRASE` is what needs re-extracting.
  - `SELECTORS` — regexes for the encrypted block, `/bolum/` links, and slug→(season,episode). Brittle by nature; update here when scraping breaks.
  - `KNOWN_DOMAIN` — resolver seed; bump when the domain rolls over.
  - `SEARCH_ENDPOINT`, `USER_AGENT`, `IMPERSONATE`, `AD_INTERCEPT_DOMAINS`.
- **curl-cffi only** for HTTP that touches the CDN/site — never stdlib `requests` (it gets 403'd on TLS fingerprint).
- The referer must propagate through the proxy for multi-source merges — proxied URLs carry a base64 `&r=` referer that sub-requests inherit (`HLSProxy.proxied` / `_rewrite`).
- Windows-first: `baslat.bat` forces UTF-8 (`chcp 65001`, `PYTHONUTF8=1`); `cli.py` reconfigures stdout/stderr to UTF-8 for Turkish/emoji output.
- The CHEQ/dead-source diagnosis (see `DeadSourceError` in Architecture above) and the ad-blacklist/subtitle heuristics both trace back to `reference/örnek eklenti/` (not tracked in git, local-only) — consult it before changing subtitle/ad detection logic.

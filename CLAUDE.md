# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> The codebase, comments, and user-facing strings are in **Turkish**. Match that language when editing comments and CLI output.

## What this is

`hayalet` is a headless (browser-less), pure-Python terminal tool for watching/downloading from Turkish streaming sites — **Dizipal** (TV series) and **hdfilmcehennemi.nl** (movies), selectable via `--site`. It bypasses Cloudflare/anti-bot via TLS impersonation, searches, navigates seasons/episodes (or resolves a movie directly), resolves the `m3u8` stream + Turkish subtitle, then either **watches** (browser + hls.js) or **downloads** (ffmpeg mux). The Dizipal-specific scraping/decryption logic is a Python port of a browser extension that lives in `reference/örnek eklenti/` — the ad blacklist and the media/subtitle heuristics come straight from that extension (`content.js`, `page-inject.js`).

Adding a third site means writing one new `hayalet/sites/<name>_adapter.py` (see **Multi-site adapter layer** below) — the shared pipeline (session/network, HLS proxy, ffmpeg mux, menu flow) never forks per site.

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
python -m hayalet --search "house of the dragon"    # list results with indices (default --site dizipal)
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action extract
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action watch
python -m hayalet --search "..." --series 1 --season 1 --episodes 1,2,3 --action download
python -m hayalet --domain https://dizipalXXXX.com  # skip the resolver, pin a domain
python -m hayalet --site hdfilmcehennemi --search "matrix" --series 0 --action extract   # movie site
```

`--quality` only affects `--action extract` output; **downloads always grab the highest-quality variant** (see `actions.download` below) — no quality prompt exists anymore.

There is **no test suite, linter, or build step**. Verification is manual: run `--action extract` (cheapest, network-only, no player/ffmpeg) to confirm the extraction chain still works end-to-end. **Player-page (`proxy._PLAYER_TEMPLATE`) changes can't be verified this way** — they need a real browser: syntax-check the extracted JS with `node --check`, and for behavior (menu, keyboard, subtitle rendering) drive a headless Chromium ad-hoc with Playwright (`pip install playwright` + `playwright install chromium`, then uninstall — it is **not** a project dependency).

External tools (auto-detected via `shutil.which` + known install paths in `actions.py`):
- **ffmpeg** — required for `download` (must be on PATH).
- A browser — `watch` opens `webbrowser.open` on the local proxy's `/player.html`.

## Architecture

Everything hangs off two shared objects created once in `cli.main()` and threaded through every call:
- **`SessionState`** (`core/session.py`) — the single identity: `base_url`, `cookies`, `user_agent`, `impersonate` profile, optional `proxy` URL. The **same** UA/cookies/referer must reach curl-cffi, ffmpeg, and the browser or the CDN returns 403. `cli.main()` populates `user_agent`/`impersonate` from a randomly-picked entry in `core/personas.py` on every run (a pool of consistent UA+impersonate device/browser combos — desktop Chrome/Edge/Firefox/Safari + Android Chrome + iOS Safari) so the tool doesn't always present one fixed, shared fingerprint. `--tor` sets `session.proxy` to the local Tor SOCKS port (127.0.0.1:9050) after a quick reachability check; falls back to a direct connection with a warning if Tor isn't running.
- **`Network`** (`core/network.py`) — the only HTTP layer. Uses **curl-cffi** (Chrome TLS/JA3 impersonation), never stdlib `requests`. Retries 403/429/503 as `BlockedError` (with jittered backoff) and syncs cookies back into `SessionState`. Builds its default headers from `session.user_agent` (not a hardcoded constant) so persona rotation actually takes effect.

### Multi-site adapter layer

`core/sites.py` defines the `SiteAdapter` Protocol (`resolve_domain`, `search`, `suggest`, `get_episodes`, `build_stream`) and a `SITES` registry dict. Each module under `sites/` implements one site and calls `register(...)` on import; `sites/__init__.py` imports all of them so `--site {dizipal,hdfilmcehennemi}` (argparse `choices=SITES.keys()`) can dispatch generically. `catalog.py`'s `Series`/`Episode` dataclasses and pure list helpers (`match_episode`, `next_episode`, `seasons_of`, `episodes_in_season`) live in `core/models.py` and are re-exported from `catalog.py` for backward compat — those stay adapter-agnostic and are used as-is by every site. `Series.site` (adapter name) is stamped by each adapter's `search`/`suggest` — this is what lets a merged multi-site result list route back to the right adapter after the user picks one.

**`--site` is optional and changes `cli.main()`'s shape**: with `--search` (non-interactive automation) it defaults to `dizipal`, single adapter, unchanged from before. Interactively, if `--site` is *not* given (the default), `main()` connects to **every** registered site in parallel (`concurrent.futures.ThreadPoolExecutor`, via `cli._connect`) and calls `run_interactive(contexts)` with a `dict[site_name, (Network, SessionState)]`; `menu.prompt_search_series(contexts)` then queries all of them at once via `sites.search_all`/`suggest_all` (also parallel — sites have fully independent `Network`/`SessionState`, no shared mutable state, so concurrent calls are safe) and shows one merged, site-tagged result list. Passing `--site` explicitly (even without `--search`) pins back to the old single-site interactive flow (`contexts` with one entry) — this is also how you'd test one adapter in isolation. `run_interactive` re-resolves `net`/`session`/`adapter` from `contexts[series.site]` after every pick (not fixed for the whole session), so "yeniden ara"/"yeni arama" naturally stays multi-site. `--domain` only applies when `--site` is also given (ambiguous across sites otherwise, and ignored with a warning if not). The "kaldığın yerden devam" resume record now carries `site` too (`prefs.json`'s `resume.site`, defaults to `"dizipal"` for older records saved before this field existed).

- **`sites/dizipal_adapter.py`** — a thin, zero-new-logic wrapper around `catalog.py`/`extractor.py`/`merge.py`/`resolver.py` (see pipeline below). `build_stream` = `merge.build_merged`.
- **`sites/hdfilmcehennemi_adapter.py`** — movie-only site, no dub/original split (the master `.m3u8` already embeds Turkish + original audio as separate HLS renditions), so `build_stream` resolves a single source directly instead of merging two. The trickiest part: the real `master.m3u8` URL is hidden behind a **"Dean Edwards packer"** JS blob (`_packer_unpack`, a=62 variant — reusable general algorithm) whose *payload* is further obfuscated by a custom "unmix" scheme (reverse / ROT-N-letters / base64-decode steps). Critically, **the count AND order of those steps is randomized on every single page load** — confirmed empirically across 15 live fetches, no two identical. `_parse_ops`/`_apply_ops` parse the operation sequence from the unpacked JS source itself (in appearance order) rather than assuming a fixed shape; only the final numeric byte-mixing step (`magic`/`offset` constants, also randomized per load but structurally fixed) is assumed constant in *shape*. If this ever stops working, re-survey a handful of fresh `/rplayer/<hash>/` fetches (see the pattern in git history) before assuming the site changed its scheme entirely — it may just be a combination `_OP_RE` hasn't seen yet.

The end-to-end Dizipal pipeline (all pure Python, no headless browser) — invoked by `DizipalAdapter`:

1. **`resolver.py`** — finds the current `dizipalXXXX.com` (cache → `config.KNOWN_DOMAIN` → incrementing scan), writes it to `.cache/domain.json`, sets `session.base_url`. This incrementing-domain-scan strategy is Dizipal-specific and does not generalize (`resolver.ResolverError` itself is reused generically — `HDFCAdapter.resolve_domain` raises it too on an unreachable static domain).
2. **`catalog.py`** — `search()` (POST `/bg/searchcontent`, needs the `cValue` token scraped from the homepage — cached per-process, see below) and `get_episodes()` (scrapes `/bolum/...-SxE` links). Also owns dubbed↔original **counterpart matching** (`is_dubbed`, `find_counterpart`, `match_episode`) and the fuzzy "şunu mu demek istedin" fallback (`suggest`).
3. **`extractor.py`** — the crypto chain: episode page → `data-rm-k` encrypted JSON → **`utils.decrypt_rmk`** (CryptoJS AES + PBKDF2-SHA512, params in `config.py`) → iframe → `openPlayer('<playList>')` + embedded Turkish `.vtt` → `source2.php` → real `master.m3u8`. Raises `DeadSourceError` (subclass of `ExtractError`) when the player host is a **dead/parked domain** (CHEQ→RTB→domain-parking, common on "Türkçe Dublaj" embeds) — this is *not* anti-bot, there is genuinely no video behind it.
4. **`merge.py`** — resolves **both** the dubbed and original versions of an episode and combines them into one `MergedStream`: original video + [Turkish-dub audio, original audio] + Turkish subtitle. This is the object `watch`/`download` consume, built via `adapter.build_stream(...)` (a single call now covers `extract`/`watch`/`download` alike — no separate/duplicate extraction path). When the dub source is dead it gracefully falls back to just the original.
5. **`proxy.py`** — the crux of playback. A local `ThreadingHTTPServer` (`HLSProxy`) that players/ffmpeg connect to on `127.0.0.1`. It refetches every playlist/segment through curl-cffi (solving the 403), rewrites playlist URLs to point back at itself, serves disguised `.jpg` segments as `.ts`, and **injects the Turkish subtitle as an HLS `SUBTITLES` channel** into the master so every player sees it natively. It can also host **synthetic** playlists (`virtual()`) — the multi-audio master built by `merge` — and serves the `/player.html` hls.js watch page (hls.js bundled locally at `hayalet/assets/hls.min.js`). Fully site-agnostic. **The player page (`_PLAYER_TEMPLATE`) has non-obvious workarounds — read these before touching it:** (a) subtitles are rendered by our **own `#capOverlay` div**, not `video::cue` — we force the hls.js text track to `mode='hidden'` (so the browser draws nothing) and poll `activeCues` ourselves, because native cue styling (size/color/bg) is unreliable across browsers and rendering both caused doubled subtitles; (b) `v.blur()` on the video's `focus` event — otherwise native `<video controls>` steals keyboard focus and the browser's built-in shortcuts swallow our keys (arrow-seek would double to ~20/40s); (c) the native fullscreen button is CSS-hidden and fullscreen goes through our own button / `f` key on `#wrap` (native FS fullscreens the `<video>` alone, losing the gear/menu/subtitle overlay); (d) controls auto-hide via a mousemove + inactivity timer (not `:hover`, which never hides in fullscreen). The extracted JS must pass `node --check`; watch for literal `\n` inside the Python triple-quoted template (must be `\\n` to reach JS as an escape).
6. **`actions.py`** — `watch()` builds a synthetic master, opens the browser at the proxy's player page (a single top-right ⚙ menu gives quality/audio/subtitle selection + subtitle size/color/background). `download()` runs a **single ffmpeg pass** mapping the **highest-quality video variant** (resolved via `list_variants`[0] — never the raw master, since ffmpeg's default variant pick is the *first*, which on some sites e.g. hdfilmcehennemi is the **lowest**) + every audio track (language/title tagged) + softsubbed Turkish subtitle → `.mp4` (single audio) or `.mkv` (multi-audio or subtitle), with live progress from `ffmpeg -progress`. Site-agnostic. **Download destination** (`cli._out_dir`/`_file_title`): base is the user's `~/Downloads` (`config.DOWNLOAD_DIR = Path.home()/"Downloads"`); a **series** goes to `Downloads/<Series>/<Series> - SxxExx.mkv` (episodes grouped in one subfolder), a **movie** goes straight to `Downloads/<Movie>.mkv` (single file, no subfolder) — the split is driven by `catalog.is_movie(series)`.

The interactive flow (`cli.run_interactive`) branches on `catalog.is_movie(series)`: a **movie** skips season/episode selection entirely and gets a flat `menu.prompt_movie_action` (İzle / İndir / Yeni arama / Çıkış) acting on the single synthetic episode (`cli._movie_flow`); a **series** keeps the season → mode → episode(s) flow. Result-list type labels are Turkish-ified (`menu._type_tr`: Series→Dizi, Movies/Film→Film) and search runs under a spinner.

Supporting: `m3u8_parser.py` (variant/audio-track parsing via the `m3u8` lib — fully generic HLS parsing, used by every adapter), `menu.py` (questionary interactive prompts, adapter-parameterized), `logs.py`, `utils.py` (crypto + heuristics + `safe_filename`), `personas.py` (device/browser fingerprint rotation), `prefs.py` (`.cache/prefs.json` — quality/resume persistence).

## Working on this codebase — key facts

- **`config.py` is the single source of truth for Dizipal specifically** (not the other sites — each adapter under `sites/` owns its own constants/regexes inline). When Dizipal changes, you edit config, not code:
  - `RMK_PASSPHRASE` / `RMK_ITERATIONS` / `RMK_KEYSIZE` — the `data-rm-k` decryption params, extracted from the site's `app-dizipals.js`. If the site rotates its bundle, decryption breaks and `RMK_PASSPHRASE` is what needs re-extracting.
  - `SELECTORS` — regexes for the encrypted block, `/bolum/` links, and slug→(season,episode). Brittle by nature; update here when scraping breaks.
  - `KNOWN_DOMAIN` — resolver seed; bump when the domain rolls over.
  - `SEARCH_ENDPOINT`, `USER_AGENT`, `IMPERSONATE`, `AD_INTERCEPT_DOMAINS`.
  - `catalog._cvalue_cache` — the search token is fetched once per process (not per search call) to cut redundant homepage requests; it's an in-memory dict keyed by `base_url`, never persisted.
- **curl-cffi only** for HTTP that touches the CDN/site — never stdlib `requests` (it gets 403'd on TLS fingerprint). Both Dizipal and hdfilmcehennemi were confirmed to need TLS impersonation (plain `WebFetch`/`requests` gets a 403 on either).
- The referer must propagate through the proxy for multi-source merges — proxied URLs carry a base64 `&r=` referer that sub-requests inherit (`HLSProxy.proxied` / `_rewrite`).
- Windows-first: `baslat.bat` forces UTF-8 (`chcp 65001`, `PYTHONUTF8=1`); `cli.py` reconfigures stdout/stderr to UTF-8 for Turkish/emoji output.
- The CHEQ/dead-source diagnosis (see `DeadSourceError` in Architecture above) and the ad-blacklist/subtitle heuristics both trace back to `reference/örnek eklenti/` (not tracked in git, local-only) — consult it before changing subtitle/ad detection logic. This only applies to Dizipal.
- Anti-detection posture (both sites): random device/browser persona per run (`personas.py`), jittered retry backoff and inter-episode download pacing (`network.py`/`cli._run_downloads`), no persisted cookies across runs, optional `--tor`. See `yapılcaklar.txt` history / prior session notes for the full reasoning — this is a best-effort, free-only posture, not a guarantee.

# Changelog

All notable changes to imozuru are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release numbers follow [Semantic Versioning](https://semver.org/). The plugin manifest intentionally has no version field; release numbers mark coherent changes in this changelog and in Git tags.

## [Unreleased]

### Added

- Show a “Loading document” card when an embedded dashboard document takes more than 0.8 seconds to load.
- Explain document-load failures and offer a retry action, distinguishing a stopped server, unreachable PC, rejected passphrase, and missing document.
- After ten seconds, probe the server and distinguish a slow document from a stopped server.
- Install an offline fallback through a service worker after the served dashboard is opened once. It stores only the fallback UI, probes every five seconds, and reloads automatically when the server returns; research content is never cached.
- Add “Open original page” links to extract and detailed-report quotations (#16, reported and proposed by yuki-kogetsu). URLs are resolved from the Original URL column of the source table, then from `fetch-log.jsonl`, with explicit quotation URLs as a final override. Only complete http(s) URLs become links.

### Fixed

- Avoid waiting the full fetch timeout for every unreachable IPv6 address before trying IPv4. IPv6 and IPv4 are interleaved, each address receives a short initial connection attempt, and slow candidates receive a final retry without changing post-connect read timeouts.
- Hide install guidance inside existing Edge and other minimal-ui app windows, not only standalone windows.

## [2.0.0] - 2026-10-25

Version 1.0 collected original sources and presented verified extracts. Version 2.0 broadened the human-reading side of the workflow.

### Highlights

- **Understand the research**: a detailed report explains the field at three reader levels, with source evidence for every sentence.
- **Review past research**: one dashboard shows all topics, progress, and stop reasons.
- **Read anywhere**: the dashboard can be served to a phone.
- **Use one visual system**: extracts, detailed reports, `map.md`, and `log.md` share the dashboard design.

### Added

- **Detailed reports** (`digest.py`, `digest.html`)
  - Three explanations for readers with no background, neighboring-field knowledge, and specialist knowledge.
  - Verbatim evidence for every explanatory sentence, verified against local source lines before HTML is written.
  - Retrieval questions at the end of each level.
  - A complete query and fetch record with site totals and saved, failed, and not-fetched states.
- **Query records** (`queries.jsonl`)
  - `digest.py add` and `chain.py --log` preserve every hit for every query.
- **Research dashboard** (`dashboard.py`)
  - Reads the research root and assembles one page for all topics.
  - Summarizes stop reasons, source hosts, and rounds.
  - Opens topic details, detailed reports, extracts, maps, and logs in the same interface.
  - Searches titles, questions, terms, people, and venues; legacy Japanese records also match hiragana and romaji.
  - Pages the sidebar and supports desktop, touch, and pull-to-refresh navigation.
  - Includes an illustrated help view.
- **Dashboard serving** (`dashboard.py --serve`)
  - Serves read-only content on 127.0.0.1 behind a passphrase URL for relaying through Tailscale or a similar tool.
  - Restricts Host values to loopback, `*.ts.net`, and explicit `IMOZURU_SERVE_HOSTS` entries.
  - Requires at least 16 characters for a fixed `IMOZURU_SERVE_TOKEN`.
  - Excludes symlinked topic folders and documents.
  - Includes home-screen and browser-tab icons.
- **Installable app**
  - Supports installation from Chrome, Edge, and mobile browsers; installed instances retain access without re-entering the passphrase.
  - Offers browser-specific installation guidance.
- **PDF output**
  - Prints only the selected document, opens evidence, removes interface chrome, and uses indented quotations.
  - Opens a standalone document before printing in Safari and on iPhone.
- **Feedback**
  - Sends a user-written report, imozuru version, and browser type to a configured Google Form without sending topic names, research content, or page URLs.
  - Configure with `IMOZURU_FEEDBACK_FORM`; use `off` to disable.
- **Document bundle** (`dashboard.py --bundle`)
  - Copies the dashboard and generated documents without original-source files.
- **PMC full text** (`fetch.py`; #12, @hattorimayumi0105)
  - Retrieves PMC articles from Europe PMC full-text XML when the article page is blocked.
  - Rejects XML entity declarations and enforces element-count and depth limits.
- **Map Title and Reading fields**
  - A short Title appears in the dashboard.
  - Reading aliases preserve hiragana/romaji search for legacy Japanese records.
- **Shared document styling** (`skin.py`, `palette.py`)
  - Renders extracts, detailed reports, maps, and logs with a shared dashboard-aligned theme.
  - Renders Markdown headings, lists, and tables rather than raw text.

### Changed

- Replaced internal vocabulary in generated documents with reader-facing terminology and retained compatibility with older status values.
- Made malformed Unicode URLs non-fatal in fetching, dashboards, detailed reports, and Markdown rendering.
- Kept only http(s) links from fetched HTML and PMC XML.
- Re-resolved and pinned checked destination IPs immediately before connections to reduce DNS-rebinding risk (#13, @akira0222). Proxy resolution remains outside this protection.
- Treat a partial `pdftoppm` failure as an OCR failure rather than accepting incomplete pages (#14, @Boba7567).
- Ignore unknown `IMOZURU_DASH_THEME` values instead of failing startup.
- Restore prior source files when saving a second output fails.
- Use unique temporary names for page writes.
- Add explicit language attributes to quotations.
- Remove explanatory boilerplate from extract and detailed-report pages.

## [1.0.0] - 2026-10-21

The first release collected original sources and presented verified extracts to strengthen AI judgment with explicit knowledge and know-how. It includes fixes accumulated since the initial publication on 2026-10-13.

### Added

- The `SKILL.md` workflow for tracing an unfamiliar field from terms, people, and venues to original sources.
- Evidence-based stop conditions: all points covered, saturation, round limit, login/payment blockage, or an agreed time/round limit.
- Source fetching (`fetch.py`), citation and author following (`chain.py`), feed monitoring (`watch.py`), and verified extract pages (`report.py`).
- Claude Code plugin packaging.

### Fixed

- Preserve saved sources when a refetch fails (#1, #2).
- Support Windows cp932 terminals and Python 3.10 (#3).
- Decode Shift_JIS and EUC-JP pages from declared encodings.
- Fetch only the original-language automatic YouTube captions to avoid translated-caption rate-limit failures (#6).
- Reject non-http(s) URLs (#10).
- Handle Unicode URLs and titles beginning with W (#7, #9).
- Treat URLs, fetched bodies, and feeds as untrusted input.
- Select installed OCR language packs and reject meaningless low-confidence OCR (#11).

[Unreleased]: https://github.com/OAFang2/imozuru/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/MaryCache/imozuru/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/MaryCache/imozuru/releases/tag/v1.0.0

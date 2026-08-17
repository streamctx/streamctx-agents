#   Changelog

   All notable changes to StreamCtx will be documented in this file.

## [0.4.4] - 2026-07-31

### Fixed
          -  `wrap()` now tracks calls per-client instance instead of relying on a
              global `_originals` check, fixing a silent failure where duck-typed
             (non-genuine SDK) clients were not tracked at all — `get_stats()` and
             `resume()` would silently return zero/empty data. (#5)

## [0.4.3] - 2026-07

Added
        •       CI matrix now covers Ubuntu, macOS, and Windows across Python 3.9–3.12 (12/12 passing).
        •       bug_report.yml issue template with severity labels.

Fixed
        •       Expired PyPI publishing token replaced with a fresh scoped token.

## [0.4.2] - 2026-07

Fixed
        •       SQLite concurrency hardening: resolved a 5-bug cascade (WAL mode singleton race,
                connection leak, read/write pool split, missing indexes). Verified with 50 concurrent
                workers completing in under 5 seconds with zero errors.

Security

        •       Full security audit completed: no real secrets leaked.

Added

        •       Full test suite passing: 89 passed, 1 skipped.

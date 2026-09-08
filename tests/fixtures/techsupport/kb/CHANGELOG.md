# Changelog

## [0.4.4]

### Fixed

`wrap()` now tracks calls per-client instance instead of relying on a
global check, fixing a silent failure for duck-typed clients.

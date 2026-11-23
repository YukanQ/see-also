# Changelog

## 0.4.0 – 2026-11-23

First public version. see_also has been in daily use on one Mac since mid-2026; this release
collects that state.

- Semantic search over a PDF collection with `intfloat/multilingual-e5-large`, exact cosine
  search over all passages, per-document ranking, contrast view.
- Index in one SQLite file (`see_also.db`) with stable passage IDs; incremental updates of new,
  changed and deleted files; cancel at any time without damage.
- Isolated PDF reading: crashing or hanging PDFs are recorded as problem files, the run continues.
- OCR for scanned PDFs with ocrmypdf into a separate folder; originals are never changed. Review
  pages for damaged or unreadable files.
- Search scope by area; requests from Obsidian via a small plugin.
- Offline operation once the model is downloaded.
- English user interface, test suite with network access blocked, GitHub Actions on Linux and
  macOS.

"""File conversion (plans/convert.md, Track I): readers and writers that turn
a dive computer's own file into `UnifiedDive`s and back, with no adapter,
engine, settings or credentials involved.

- `fit_reader`: a Garmin dive ``.fit`` (or Connect's "export original" zip
  around one) -> one `UnifiedDive`, including the carried fields of I1.
- `ssrf_writer`: `UnifiedDive`s -> Subsurface's XML divelog (``.ssrf``), in
  Subsurface's own layout and value formats.
- `formats`: the registry the page works from: detection of a chosen file,
  `read_file` / `read_files` for every readable format, `write_file` /
  `write_each` for every writable one, the file-dialog filters and the
  per-dive output file names.
"""

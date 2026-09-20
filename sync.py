#!/usr/bin/env python3
import os
import sys
import argparse
import logging
from src.core.sync_engine import SyncEngine

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )

def _table(headers, rows):
    """Plain fixed-width table, no dependencies."""
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(out)


def print_mapping(engine):
    from src.core.templates import validate_links
    links = engine.settings.field_links
    print(f"Mapping board for {engine.source_id} -> {engine.target_id} "
          f"(directionality: {engine.settings.directionality}, {len(links)} links)\n")
    rows = []
    for link in links:
        arrow = {"bidirectional": "<->", "to_target": "->", "to_source": "<-", "off": "off"}[link.direction]
        rows.append([link.id, " + ".join(link.source), arrow, link.target, link.conflict,
                     link.match_order if link.match_order is not None else "", link.template or ""])
    print(_table(["id", "source", "", "target", "conflict", "match", "template"], rows))
    problems = validate_links(links, engine.catalog)
    if problems:
        print("\nProblems:")
        for p in problems:
            print(f"  - {p}")


def validate_profile_file(engine, path) -> bool:
    from src.core.config import ProfileError, import_profile, read_profile
    from src.core.templates import validate_links
    try:
        data = read_profile(path)
        new_settings, summary = import_profile(data, engine.settings, engine.catalog)
    except ProfileError as e:
        print(f"Profile is not valid: {e}")
        return False
    print(summary.as_text())
    problems = validate_links(new_settings.field_links, engine.catalog)
    for job in new_settings.cron_jobs:
        if job.field_links:
            problems.extend(f"Job '{job.id}': {p}" for p in validate_links(job.field_links, engine.catalog))
    if problems:
        print("Problems with the field links:")
        for p in problems:
            print(f"  - {p}")
        return False
    print("Profile is valid.")
    return True


def import_profile_file(engine, path, assume_yes: bool = False) -> bool:
    from src.core.config import ConfigManager, ProfileError, import_profile, read_profile
    from src.core.templates import validate_links
    try:
        data = read_profile(path)
        new_settings, summary = import_profile(data, engine.settings, engine.catalog)
    except ProfileError as e:
        print(f"Profile cannot be imported: {e}")
        return False
    problems = validate_links(new_settings.field_links, engine.catalog)
    if problems:
        print("Profile cannot be imported, its field links are invalid:")
        for p in problems:
            print(f"  - {p}")
        return False
    print(summary.as_text())
    if not summary.changes and not summary.skipped_links:
        print("Nothing to change.")
        return True
    if not assume_yes:
        answer = input("Apply these changes to settings? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Import cancelled.")
            return False
    ConfigManager.save_settings(new_settings, engine.settings_path)
    print(f"Settings updated from {path}.")
    return True


def print_conflicts(engine):
    conflicts = engine.list_conflicts()
    if not conflicts:
        print(f"No conflicts waiting ({engine.conflicts_file}).")
        return
    rows = [[c.id, c.dive_time, c.link_id, f"{c.source_key}={c.source_value!r}",
             f"{c.target_key}={c.target_value!r}", c.seen_at] for c in conflicts]
    print(_table(["id", "dive", "link", "source", "target", "seen"], rows))
    print("\nResolve with: python sync.py --resolve <id> source|target")


def print_test_mapping(out):
    if not out.get("ok"):
        print("The board is not valid:")
        for p in out.get("problems", []):
            print(f"  - {p}")
        return
    print(f"Test mapping {out['source']} -> {out['target']} (directionality: {out['directionality']}): "
          f"fetched {out['fetched']}, {out['matched']} matched")
    for side, times in out["unmatched"].items():
        if times:
            print(f"  only on {side}: {', '.join(times)}")
    rows = [[r["dive_time"], r["link"], r["source_key"], r["source_value"], r["target_key"], r["target_value"],
             r["result"], "; ".join(r["warnings"])] for r in out["rows"]]
    print()
    print(_table(["dive", "link", "source", "value", "target", "value", "result", "warnings"], rows))
    print("\nRead-only: nothing was written.")


def main():
    parser = argparse.ArgumentParser(
        description="Dive Sync: Dive synchronization engine between Garmin Connect and Divelogs.org."
    )
    
    parser.add_argument(
        "--dry-run", 
        action="store_true", 
        help="Simulate the synchronization process without modifying remote accounts."
    )
    
    parser.add_argument(
        "--backup", 
        action="store_true", 
        help="Export Garmin Connect and Divelogs.org history into local JSON backup files."
    )
    
    parser.add_argument(
        "--garmin-path", 
        type=str, 
        default="garmin_backup.json", 
        help="Custom output file path for Garmin backup."
    )
    
    parser.add_argument(
        "--divelogs-path", 
        type=str, 
        default="divelogs_backup.json", 
        help="Custom output file path for Divelogs backup."
    )
    
    parser.add_argument(
        "--date-from", 
        type=str, 
        help="Override config: Sync start date (YYYY-MM-DD)."
    )
    
    parser.add_argument(
        "--date-to", 
        type=str, 
        help="Override config: Sync end date (YYYY-MM-DD)."
    )

    parser.add_argument(
        "--full-sync", "--no-incremental",
        dest="full_sync",
        action="store_true",
        help="Perform a full sync instead of incremental sync (defaults to incremental)."
    )
    
    parser.add_argument(
        "--direction",
        type=str,
        choices=["bidirectional", "to_garmin", "to_divelogs"],
        help="Override config: Sync flow directionality (bidirectional, to_garmin, to_divelogs)."
    )
    
    default_data_dir = os.environ.get("DATA_DIR", "./data")
    parser.add_argument(
        "--save-raw-data",
        type=str,
        nargs="?",
        const=default_data_dir,
        default=None,
        help=f"Download and save all raw data from Garmin and Divelogs to the specified local directory (default: {default_data_dir})."
    )

    parser.add_argument(
        "--mock-data-dir",
        type=str,
        nargs="?",
        const=default_data_dir,
        default=None,
        help=f"Run sync utilizing local mock/stored raw JSON data (default: {default_data_dir}) instead of communicating with remote APIs."
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite/clear existing local mock data directories before downloading raw data."
    )

    parser.add_argument(
        "--garmin",
        type=str,
        help="Garmin account username to use (optional if only one is configured)."
    )

    parser.add_argument(
        "--divelogs",
        type=str,
        help="Divelogs account username to use (optional if only one is configured)."
    )

    parser.add_argument(
        "--show-mapping",
        action="store_true",
        help="Print the mapping board (field links per sync pair) as a table and exit."
    )

    parser.add_argument(
        "--validate-profile",
        type=str,
        metavar="FILE",
        help="Check a sync profile file the same way Save does (version, links, templates) and exit."
    )

    parser.add_argument(
        "--export-profile",
        type=str,
        metavar="PATH",
        help="Write the current sync profile (rules, filters, jobs; never credentials) to PATH and exit."
    )

    parser.add_argument(
        "--import-profile",
        type=str,
        metavar="PATH",
        help="Replace the sections present in the profile file, after showing a summary, and exit."
    )

    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (used with --import-profile)."
    )

    parser.add_argument(
        "--list-conflicts",
        action="store_true",
        help="List the conflicts waiting for manual resolution and exit."
    )

    parser.add_argument(
        "--resolve",
        nargs=2,
        metavar=("CONFLICT_ID", "SIDE"),
        help="Resolve one conflict by writing the chosen SIDE ('source' or 'target') to the other service."
    )

    parser.add_argument(
        "--test-mapping",
        action="store_true",
        help="Read-only rehearsal: fetch the newest 10 dives per side and show what each link would do."
    )

    parser.add_argument(
        "-v", "--verbose", 
        action="store_true", 
        help="Enable verbose DEBUG logs."
    )

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger("dive_sync.sync_cli")

    logger.info("Dive Sync CLI initialized.")

    try:
        if args.save_raw_data:
            logger.info("Executing raw data downloader...")
            engine = SyncEngine(
                mock_data_dir=None,
                garmin_username=args.garmin,
                divelogs_username=args.divelogs
            )
            success = engine.download_and_save_raw_data(mock_data_dir=args.save_raw_data, overwrite=args.overwrite)
            if success:
                logger.info("Raw data download completed successfully.")
            else:
                logger.error("Raw data download encountered failures.")
                sys.exit(1)
            return

        engine = SyncEngine(
            mock_data_dir=args.mock_data_dir,
            garmin_username=args.garmin,
            divelogs_username=args.divelogs
        )

        if args.show_mapping:
            print_mapping(engine)
            return
        if args.validate_profile:
            sys.exit(0 if validate_profile_file(engine, args.validate_profile) else 1)
        if args.export_profile:
            from src.core.config import write_profile
            write_profile(engine.settings, args.export_profile)
            print(f"Profile written to {args.export_profile}")
            return
        if args.import_profile:
            sys.exit(0 if import_profile_file(engine, args.import_profile, assume_yes=args.yes) else 1)
        if args.list_conflicts:
            print_conflicts(engine)
            return
        if args.resolve:
            conflict_id, side = args.resolve
            resolved = engine.resolve_conflict(conflict_id, side)
            print(f"Resolved conflict {resolved.id} ({resolved.link_id}) in favour of the {side}.")
            return
        if args.test_mapping:
            print_test_mapping(engine.test_mapping())
            return

        if args.backup:
            logger.info("Executing history backup...")
            success = engine.backup(
                garmin_backup_path=args.garmin_path,
                divelogs_backup_path=args.divelogs_path
            )
            if success:
                logger.info("Backup completed successfully.")
            else:
                logger.error("Backup encountered failures.")
                sys.exit(1)
        else:
            logger.info("Executing dive data synchronization...")
            results = engine.run_sync(
                dry_run=args.dry_run,
                date_from_override=args.date_from,
                date_to_override=args.date_to,
                only_new_override=False if args.full_sync else True,
                direction_override=args.direction
            )
            
            # Print sync results summary
            print("\n" + "="*50)
            print("       DIVE SYNC RESULTS SUMMARY")
            print("="*50)
            print(f"Dry Run Mode:    {results['dry_run']}")
            print(f"Directionality:  {results['directionality']}")
            print(f"Matched Dives:   {results['matched_count']}")
            print("-"*50)
            print(f"Uploaded to Divelogs: {len(results['uploaded_to_divelogs'])}")
            for item in results['uploaded_to_divelogs']:
                print(f"  - {item['time']} (Garmin ID: {item['garmin_id']})")
                
            print(f"Uploaded to Garmin:    {len(results['uploaded_to_garmin'])}")
            for item in results['uploaded_to_garmin']:
                print(f"  - {item['time']} (Divelogs ID: {item['divelogs_id']})")
                
            print(f"Updated/Linked on Garmin:   {len(results['updated_on_garmin'])}")
            for item in results['updated_on_garmin']:
                print(f"  - {item['time']} (Garmin ID: {item['id']}, Linked: {item['linked_divelogs']})")
                
            print(f"Updated/Linked on Divelogs: {len(results['updated_on_divelogs'])}")
            for item in results['updated_on_divelogs']:
                print(f"  - {item['time']} (Divelogs ID: {item['id']}, Linked: {item['linked_garmin']})")
            conflicts = results.get("conflicts", [])
            if conflicts:
                print(f"Conflicts for manual resolution: {len(conflicts)} (see --list-conflicts)")
            print("="*50)

    except Exception as e:
        logger.exception("Synchronization engine failed: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()

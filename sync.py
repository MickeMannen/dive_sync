#!/usr/bin/env python3
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

def main():
    parser = argparse.ArgumentParser(
        description="Anti-Gravity: Dive synchronization engine between Garmin Connect and Divelogs.org."
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
    
    parser.add_argument(
        "--save-raw-data",
        type=str,
        nargs="?",
        const="./tests",
        default=None,
        help="Download and save all raw data from Garmin and Divelogs to the specified local directory (default: ./tests)."
    )

    parser.add_argument(
        "--mock-data-dir",
        type=str,
        nargs="?",
        const="./tests",
        default=None,
        help="Run sync utilizing local mock/stored raw JSON data (default: ./tests) instead of communicating with remote APIs."
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite/clear existing local mock data directories before downloading raw data."
    )

    parser.add_argument(
        "-v", "--verbose", 
        action="store_true", 
        help="Enable verbose DEBUG logs."
    )

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger("anti_gravity.sync_cli")

    logger.info("Anti-Gravity Dive Sync CLI initialized.")

    try:
        if args.save_raw_data:
            logger.info("Executing raw data downloader...")
            engine = SyncEngine(mock_data_dir=None)
            success = engine.download_and_save_raw_data(mock_data_dir=args.save_raw_data, overwrite=args.overwrite)
            if success:
                logger.info("Raw data download completed successfully.")
            else:
                logger.error("Raw data download encountered failures.")
                sys.exit(1)
            return

        engine = SyncEngine(mock_data_dir=args.mock_data_dir)
        
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
            print("       ANTI-GRAVITY SYNC RESULTS SUMMARY")
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
                
            print(f"Linked on Garmin:      {len(results['updated_on_garmin'])}")
            print(f"Linked on Divelogs:    {len(results['updated_on_divelogs'])}")
            print("="*50)

    except Exception as e:
        logger.exception("Synchronization engine failed: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()

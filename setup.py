#!/usr/bin/env python3
import sys
import os
import getpass
import logging
from src.core.config import ConfigManager, CredentialsModel, GarminCredentials, DivelogsCredentials
from src.core.services.garmin import GarminAdapter
from src.core.services.divelogs import DivelogsAdapter

def append_to_gitignore(entry: str):
    gitignore_path = ".gitignore"
    content = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            content = f.read()
    
    if entry not in content.splitlines():
        with open(gitignore_path, "a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{entry}\n")
        print(f"Added '{entry}' to {gitignore_path}")

def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("="*60)
    print("      DIVE SYNC INTERACTIVE PROVISIONER & CREDENTIAL SETUP")
    print("="*60)
    
    # Prompt for Garmin
    print("\n--- Garmin Connect Credentials ---")
    garmin_user = input("Username/Email: ").strip()
    garmin_pass = getpass.getpass("Password: ").strip()

    # Prompt for Divelogs
    print("\n--- Divelogs.org Credentials ---")
    divelogs_user = input("Username: ").strip()
    divelogs_pass = getpass.getpass("Password: ").strip()

    print("\nVerifying credentials...")

    # Verify Garmin
    garmin_ok = False
    if garmin_user and garmin_pass:
        print("Authenticating with Garmin Connect...")
        garmin_adapter = GarminAdapter(
            username=garmin_user, 
            password=garmin_pass, 
            token_dir="tokens/garmin", 
            cooldown_seconds=1.0
        )
        garmin_ok = garmin_adapter.login()
        if garmin_ok:
            print("✓ Garmin Connect authentication succeeded!")
        else:
            print("✗ Garmin Connect authentication failed.")
    else:
        print("ℹ Garmin credentials skipped.")

    # Verify Divelogs
    divelogs_ok = False
    if divelogs_user and divelogs_pass:
        print("Authenticating with Divelogs.org...")
        divelogs_adapter = DivelogsAdapter(
            username=divelogs_user, 
            password=divelogs_pass, 
            cooldown_seconds=1.0
        )
        divelogs_ok = divelogs_adapter.login()
        if divelogs_ok:
            print("✓ Divelogs.org authentication succeeded!")
        else:
            print("✗ Divelogs.org authentication failed.")
    else:
        print("ℹ Divelogs credentials skipped.")

    # Save credentials if any were provided
    if (garmin_user and garmin_pass) or (divelogs_user and divelogs_pass):
        save_anyway = "y"
        if not (garmin_ok or divelogs_ok):
            print("\nWARNING: None of the provided credentials could be authenticated successfully.")
            save_anyway = input("Do you still want to save these credentials? (y/n): ").strip().lower()

        if save_anyway == "y":
            creds = CredentialsModel(
                garmin=GarminCredentials(username=garmin_user, password=garmin_pass, token_dir="tokens/garmin"),
                divelogs=DivelogsCredentials(username=divelogs_user, password=divelogs_pass)
            )
            ConfigManager.save_credentials(creds)
            print("\n✓ Credentials saved to 'credentials.json'")
            
            # Ensure gitignored
            append_to_gitignore("credentials.json")
            append_to_gitignore("tokens/")
            append_to_gitignore("sync_state.json")
        else:
            print("\nCredentials setup aborted.")
    else:
        print("\nNo credentials provided. Setup complete.")

if __name__ == "__main__":
    main()

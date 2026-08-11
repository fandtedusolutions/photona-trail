import sys
from pathlib import Path

# Add the project root to sys.path to ensure correct imports
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_database import main as build_db_main
from scripts.search_person import main as search_person_main

def show_menu():
    print("=" * 60)
    print("      PHOTONA - Face Recognition Database Manager      ")
    print("=" * 60)
    print("1. Build / Rebuild Database (Index Event Folder)")
    print("2. Search for Person (Find matches using Selfie)")
    print("3. Exit")
    print("-" * 60)
    
    choice = input("Enter choice (1-3): ").strip()
    if choice == "1":
        print("\n--- Starting Database Build Process ---")
        build_db_main()
    elif choice == "2":
        print("\n--- Starting Person Search Process ---")
        try:
            search_person_main()
        except FileNotFoundError as e:
            print(f"\nError: {e}")
            print("Tip: If the database contains Windows file paths (e.g. G:\\...), you need to rebuild the database on this Mac first.")
    elif choice == "3":
        print("\nExiting. Goodbye!")
        sys.exit(0)
    else:
        print("\nInvalid choice. Please try again.")

if __name__ == "__main__":
    while True:
        try:
            show_menu()
            print("\n")
        except KeyboardInterrupt:
            print("\nExiting. Goodbye!")
            sys.exit(0)

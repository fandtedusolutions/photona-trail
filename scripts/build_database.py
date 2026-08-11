# import sys
from pathlib import Path

# Ensure project root directory is in sys.path
# PROJECT_ROOT = Path(__file__).resolve().parent.parent
# if str(PROJECT_ROOT) not in sys.path:
#     sys.path.insert(0, str(PROJECT_ROOT))

from config import EVENTS_DIR

from core.detector import load_face_model
from core.event_processor import process_event

from core.faiss_index import (
    build_faiss_index,
    save_faiss_index
)

from core.database import (
    save_image_records,
    save_face_records
)


# ==================================================
# Main
# ==================================================

def main():

    # ----------------------------------------
    # Load Face Model
    # ----------------------------------------

    print("Loading Face Model...")

    app = load_face_model()

    # ----------------------------------------
    # Event Folder
    # ----------------------------------------

    # event_folder = EVENTS_DIR / event_name

    event_folder = Path(
    input("Enter Event Folder Path: ").strip()
)


    # ----------------------------------------
    # Process Event
    # ----------------------------------------

    print("Processing Event...")

    image_records, face_records = process_event(
        event_folder,
        app
    )

    # ----------------------------------------
    # Build FAISS Index
    # ----------------------------------------

    print("Building FAISS Index...")

    index = build_faiss_index(
        face_records
    )

    # ----------------------------------------
    # Save Database
    # ----------------------------------------

    print("Saving Database...")

    save_image_records(
        image_records
    )

    save_face_records(
        face_records
    )

    save_faiss_index(
        index
    )

    # ----------------------------------------
    # Completed
    # ----------------------------------------

    print()

    print("Database Built Successfully!")


# ==================================================
# Entry Point
# ==================================================

if __name__ == "__main__":

    main()
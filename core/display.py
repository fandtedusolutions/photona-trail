from datetime import datetime
from pathlib import Path
import shutil

from config import OUTPUTS_DIR


# ==================================================
# Print Search Summary
# ==================================================

def print_search_summary(
    matching_images
):
    """
    Print matching image summary.
    """

    print()

    print("=" * 50)

    print("Search Completed Successfully")

    print(
        f"Total Matches : {len(matching_images)}"
    )

    print("=" * 50)

    print()

    for result in matching_images:

        image_record = result["image_record"]

        print(
            image_record["filename"]
        )

        print(
            f"Score : {result['score']:.4f}"
        )

        print()


# ==================================================
# Copy Matching Images
# ==================================================

def copy_matching_images(
    matching_images,
    person_name
):
    """
    Copy matching images into a new timestamped folder.

    Returns
    -------
    pathlib.Path
        Path to the created output folder.
    """

    # ----------------------------------------
    # Create Search Folder Name
    # ----------------------------------------

    timestamp = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    output_folder = (
        OUTPUTS_DIR /
        f"{person_name}_{timestamp}"
    )

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    # ----------------------------------------
    # Copy Images
    # ----------------------------------------

    for result in matching_images:

        image_record = result["image_record"]

        source = Path(
            image_record["image_path"]
        )

        destination = (
            output_folder /
            image_record["filename"]
        )

        shutil.copy2(
            source,
            destination
        )

    return output_folder
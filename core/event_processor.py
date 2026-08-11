from pathlib import Path

from tqdm import tqdm

from config import VALID_IMAGE_EXTENSIONS

from core.detector import process_image




def get_image_files(
    event_folder
):
    """
    Get all supported image files
    from an event folder.

    Parameters
    ----------
    event_folder : str | Path

    Returns
    -------
    list[Path]
    """

    event_folder = Path(event_folder)

    if not event_folder.exists():

        raise FileNotFoundError(
            f"Folder not found: {event_folder}"
        )

    image_files = [ file for file in event_folder.iterdir() if file.is_file() 
                   and file.suffix.lower() in VALID_IMAGE_EXTENSIONS]

    image_files.sort()

    return image_files





def process_event(
    event_folder,
    app
):
    """
    Process an entire event folder.
    """

    # ----------------------------------------
    # Get Image Files
    # ----------------------------------------

    image_files = get_image_files(
        event_folder
    )

    # ----------------------------------------
    # Create Databases
    # ----------------------------------------

    image_records = []

    face_records = []

    # ----------------------------------------
    # Process Every Image
    # ----------------------------------------

    for image_id, image_path in enumerate(
        tqdm(image_files),
        start=1
    ):

        image_record, face_record_list  = process_image(
            image_path=image_path,
            app=app,
            image_id=image_id
        )

        if image_record is None:
            continue

        image_records.append(
            image_record
        )

        face_records.extend(
            face_record_list 
        )

    return image_records, face_records
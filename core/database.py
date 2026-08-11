import pickle

from config import IMAGE_RECORDS_FILE, FACE_RECORDS_FILE


def save_image_records(image_records):
    """
    Save image records to disk.
    """

    with open( IMAGE_RECORDS_FILE, "wb") as file:

        pickle.dump( image_records, file)


def load_image_records():
    """
    Load image records from disk.
    """

    if not IMAGE_RECORDS_FILE.exists():
        raise FileNotFoundError( f"Image records not found: {IMAGE_RECORDS_FILE}" )

    with open( IMAGE_RECORDS_FILE, "rb") as file:

        image_records = pickle.load( file)

    return image_records


def save_face_records(face_records):
    """
    Save face records to disk.
    """

    with open( FACE_RECORDS_FILE, "wb") as file:

        pickle.dump( face_records, file )


def load_face_records():
    """
    Load face records from disk.
    """

    if not FACE_RECORDS_FILE.exists():
        raise FileNotFoundError( f"Image records not found: {FACE_RECORDS_FILE}" )

    with open( FACE_RECORDS_FILE, "rb" ) as file:

        face_records = pickle.load( file )

    return face_records
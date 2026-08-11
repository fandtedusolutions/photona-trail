from config import SIMILARITY_THRESHOLD


# ==========================================================
# Search Faces
# ==========================================================

def search_faces(
    query_embedding,
    index,
    face_records
):
    """
    Search the FAISS index for all matching faces.

    Parameters
    ----------
    query_embedding : numpy.ndarray
        Normalized query embedding.

    index : faiss.Index
        Loaded FAISS index.

    face_records : list
        List of face records.

    Returns
    -------
    list
        List of matching image IDs and similarity scores.
    """

    # --------------------------------------------------
    # Check FAISS Index
    # --------------------------------------------------

    if index.ntotal == 0:

        raise ValueError(
            "The FAISS index is empty."
        )

    # --------------------------------------------------
    # Search Entire Index
    # --------------------------------------------------

    distances, indices = index.search(
        query_embedding,
        index.ntotal
    )

    # --------------------------------------------------
    # Collect face_matches
    # --------------------------------------------------

    face_matches = []

    for distance, row_index in zip(
        distances[0],
        indices[0]
    ):

        if row_index == -1:
            continue

        if distance < SIMILARITY_THRESHOLD:
            continue

        image_id = face_records[row_index]["image_id"]

        face_matches.append({

            "image_id": image_id,

            "score": float(distance)

        })

    return face_matches




def get_matching_images(
    face_matches,
    image_records
):
    """
    Convert face matches into unique image records.
    """

    # ----------------------------------------
    # Create Image Lookup
    # ----------------------------------------

    image_lookup = {

        record["image_id"]: record

        for record in image_records

    }

    # ----------------------------------------
    # Remove Duplicate Images
    # ----------------------------------------

    seen_images = set()

    matching_images = []

    # ----------------------------------------
    # Build Result
    # ----------------------------------------

    for match in face_matches:

        image_id = match["image_id"]

        if image_id in seen_images:
            continue

        seen_images.add(image_id)

        image_record = image_lookup.get(
            image_id
        )

        if image_record is None:
            continue

        matching_images.append({

            "image_record": image_record,

            "score": match["score"]

        })

    return matching_images
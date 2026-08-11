import faiss
import numpy as np

from config import FAISS_INDEX_FILE


def build_faiss_index(
    face_records
):
    """
    Build a FAISS index from face embeddings.

    Parameters
    ----------
    face_records : list

    Returns
    -------
    faiss.Index
    """

    # ----------------------------------------
    # Extract Embeddings
    # ----------------------------------------

    embeddings = [

        record["embedding"]

        for record in face_records

    ]

    # ----------------------------------------
    # Convert to NumPy
    # ----------------------------------------

    embeddings = np.array(
        embeddings,
        dtype=np.float32
    )

    # ----------------------------------------
    # Normalize Embeddings
    # ----------------------------------------

    faiss.normalize_L2(
        embeddings
    )

    # ----------------------------------------
    # Create FAISS Index
    # ----------------------------------------

    dimension = embeddings.shape[1]

    index = faiss.IndexFlatIP(
        dimension
    )

    # ----------------------------------------
    # Add Embeddings
    # ----------------------------------------

    index.add(
        embeddings
    )

    return index



def save_faiss_index(index):
    """
    Save the FAISS index.
    """

    faiss.write_index(
        index,
        str(FAISS_INDEX_FILE)
    )



def load_faiss_index():
    """
    Load the FAISS index.
    """
    if not FAISS_INDEX_FILE.exists():
        raise FileNotFoundError(
            f"FAISS index not found: {FAISS_INDEX_FILE}"
        )
    
    index = faiss.read_index(
        str(FAISS_INDEX_FILE)
    )

    return index
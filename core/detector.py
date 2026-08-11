"""
detector.py

This module contains all functions related to:

1. Loading the InsightFace model.
2. Processing event images.
3. Processing query images (Coming Next).
"""

from pathlib import Path

import cv2
import numpy as np

import warnings
warnings.filterwarnings("ignore")

# import logging
# logger = logging.getLogger(__name__)

from insightface.app import FaceAnalysis

from config import MODEL_NAME, DETECTION_SIZE

import faiss

# ==========================================================
# Load Face Detection Model
# ==========================================================

def load_face_model():
    """
    Load and initialize the InsightFace model.

    Returns
    -------
    FaceAnalysis
        Initialized InsightFace model.
    """

    app = FaceAnalysis(
        name=MODEL_NAME
    )

    app.prepare(
        ctx_id=0,
        det_size=DETECTION_SIZE
    )

    return app


# ==========================================================
# Process Event Image
# ==========================================================

def process_image(
    image_path,
    app,
    image_id
):
    """
    Process a single event image.

    Parameters
    ----------
    image_path : str | Path
        Path to the event image.

    app : FaceAnalysis
        Initialized InsightFace model.

    image_id : int
        Unique image ID.

    Returns
    -------
    tuple

        image_record : dict

        face_records : list
    """

    # --------------------------------------------------
    # Read Image
    # --------------------------------------------------

    image = cv2.imread(str(image_path))

    if image is None:

        print(f"Could not read image: {image_path}")
        # logger.warning(f"Could not read image: {image_path}")

        return None, []

    # --------------------------------------------------
    # Convert BGR → RGB
    # --------------------------------------------------

    image = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2RGB
    )

    # --------------------------------------------------
    # Detect Faces
    # --------------------------------------------------

    faces = app.get(image)

    if len(faces) == 0:

        return None, []

    # --------------------------------------------------
    # Create Image Record
    # --------------------------------------------------

    image_record = {

        "image_id": image_id,

        "filename": Path(image_path).name,

        "image_path": str(image_path),

        "total_faces": len(faces)

    }

    # --------------------------------------------------
    # Create Face Records
    # --------------------------------------------------

    face_records = []

    for face in faces:

        face_record = {

            "image_id": image_id,

            "embedding": face.embedding.astype(np.float32)

        }

        face_records.append(face_record)

    return image_record, face_records


# ==========================================================
# Process Query Image
# ==========================================================

def process_query_image(
    image_path,
    app
):
    """
    Process a query (selfie) image.

    Parameters
    ----------
    image_path : str | Path
        Path to the query image.

    app : FaceAnalysis
        Initialized InsightFace model.

    Returns
    -------
    numpy.ndarray | None
        Normalized face embedding.
        Returns None if no valid face is found.
    """

    # --------------------------------------------------
    # Read Image
    # --------------------------------------------------

    image = cv2.imread(str(image_path))

    if image is None:

        print(f"Could not read image: {image_path}")

        return None

    # --------------------------------------------------
    # Convert BGR -> RGB
    # --------------------------------------------------

    image = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2RGB
    )

    # --------------------------------------------------
    # Detect Faces
    # --------------------------------------------------

    faces = app.get(image)

    # --------------------------------------------------
    # No Face Found
    # --------------------------------------------------

    if len(faces) == 0:

        print("No face detected.")

        return None

    # --------------------------------------------------
    # Multiple Faces Found
    # --------------------------------------------------

    if len(faces) > 1:

        print("Please upload an image containing only one face.")

        return None

    # --------------------------------------------------
    # Get Face Embedding
    # --------------------------------------------------

    query_embedding  = faces[0].embedding.astype(np.float32)

    query_embedding  = np.expand_dims(
        query_embedding ,
        axis=0
    )

    faiss.normalize_L2(query_embedding )

    return query_embedding 
from pathlib import Path

# =====================================================
# Project Directories
# =====================================================

PROJECT_DIR = Path(__file__).resolve().parent

EVENTS_DIR = PROJECT_DIR / "events"

DATABASE_DIR = PROJECT_DIR / "database"

FAISS_INDEX_FILE = DATABASE_DIR / "face_index.faiss"

IMAGE_RECORDS_FILE = DATABASE_DIR / "image_records.pkl"

FACE_RECORDS_FILE = DATABASE_DIR / "face_records.pkl"

MODELS_DIR = PROJECT_DIR / "models"

OUTPUTS_DIR = PROJECT_DIR / "outputs"


# =====================================================
# InsightFace Settings
# =====================================================

MODEL_NAME = "buffalo_l"

DETECTION_SIZE = (640, 640)

# =====================================================
# Search Settings
# =====================================================

SIMILARITY_THRESHOLD = 0.45

# =====================================================
# Supported Image Extensions
# =====================================================

VALID_IMAGE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
)
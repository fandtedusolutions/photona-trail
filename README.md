# Photona - Face Recognition Tool

A high-performance face recognition and sorting tool powered by **InsightFace** and **FAISS**. This project allows you to index local photos (events) and search for matching faces using a query selfie.

---

## 🚀 Features

- **Face Detection & Embedding Extraction**: Uses InsightFace (`buffalo_l`) to detect faces and extract robust face embeddings.
- **Fast Similarity Search**: Uses FAISS (Facebook AI Similarity Search) index for near-instantaneous face matching.
- **Auto Image Sorting**: Copies matches of a person into a separate output directory named after the query.

---

## 🛠️ Getting Started (macOS)

The repository has been configured with a macOS-compatible Python 3.12 virtual environment.

### 1. Activate the Virtual Environment
Activate the environment in your shell:
```bash
source venv/bin/activate
```

### 2. Run the Interactive Menu
Run the main script to build databases or search:
```bash
python main.py
```

---

## 📁 Project Structure

- `main.py`: Interactive CLI entry point.
- `config.py`: Configuration settings (detection size, similarity threshold, paths).
- `core/`: Core modules for database, detection, display, FAISS indexing, and search logic.
- `scripts/`:
  - `build_database.py`: Extract embeddings and build the FAISS index from an event folder.
  - `search_person.py`: Query the database with a selfie and output matching images.
- `database/`: Storage folder for the FAISS index and metadata.
# photona-trail

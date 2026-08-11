import cv2
import numpy as np
import faiss
from django.conf import settings
from insightface.app import FaceAnalysis
from config import MODEL_NAME, DETECTION_SIZE, SIMILARITY_THRESHOLD
from .models import GalleryImage, FaceEmbedding

# Singleton-like lazy loaded InsightFace model
_face_app = None

def get_face_model():
    global _face_app
    if _face_app is None:
        _face_app = FaceAnalysis(name=MODEL_NAME)
        _face_app.prepare(ctx_id=0, det_size=DETECTION_SIZE)
    return _face_app

def process_gallery_image(gallery_image):
    """
    Process an uploaded gallery image using InsightFace and save embeddings to DB.
    """
    app = get_face_model()
    image_path = gallery_image.file.path
    
    # Read image
    img = cv2.imread(image_path)
    if img is None:
        return 0
    
    # Convert BGR to RGB
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    
    # Detect faces
    faces = app.get(img)
    
    # Save face embeddings
    embeddings = []
    for face in faces:
        embedding_list = face.embedding.astype(float).tolist()
        embeddings.append(FaceEmbedding(
            image=gallery_image,
            embedding_data=embedding_list
        ))
    if embeddings:
        FaceEmbedding.objects.bulk_create(embeddings)
        
    # Update total faces count
    gallery_image.total_faces = len(faces)
    gallery_image.save()
    return len(faces)

def search_person_by_selfie(selfie_path, event_id=None, tenant_user=None):
    """
    Query the database of face embeddings using a query selfie.
    """
    app = get_face_model()
    
    # Read query selfie
    img = cv2.imread(selfie_path)
    if img is None:
        raise ValueError("Could not read query selfie image.")
        
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    faces = app.get(img)
    
    if len(faces) == 0:
        raise ValueError("No face detected in the uploaded selfie.")
    if len(faces) > 1:
        raise ValueError("Selfie contains more than one face. Please upload a clear photo of yourself only.")
        
    query_embedding = faces[0].embedding.astype(np.float32)
    query_embedding = np.expand_dims(query_embedding, axis=0)
    faiss.normalize_L2(query_embedding)
    
    # Load face embeddings from DB matching filters
    query = FaceEmbedding.objects.all()
    if event_id:
        query = query.filter(image__event_id=event_id)
    elif tenant_user and not tenant_user.is_superuser:
        query = query.filter(image__event__owner=tenant_user)
        
    face_records = list(query)
    if not face_records:
        return []
        
    embeddings_list = [record.get_numpy_embedding() for record in face_records]
    embeddings_matrix = np.array(embeddings_list, dtype=np.float32)
    faiss.normalize_L2(embeddings_matrix)
    
    # Create FAISS Index
    dimension = embeddings_matrix.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(embeddings_matrix)
    
    # Search index
    distances, indices = index.search(query_embedding, index.ntotal)
    
    # Collect matching images
    matching_images = []
    seen_images = set()
    
    for distance, row_index in zip(distances[0], indices[0]):
        if row_index == -1:
            continue
        if distance < SIMILARITY_THRESHOLD:
            continue
            
        record = face_records[row_index]
        image = record.image
        
        if image.id in seen_images:
            continue
            
        seen_images.add(image.id)
        matching_images.append({
            "image": image,
            "score": float(distance)
        })
        
    return matching_images


import re
import requests
import zipfile
import os
from django.core.files import File
from config import VALID_IMAGE_EXTENSIONS

def extract_gdrive_id(url):
    # Regex to find file ID in typical sharing URL formats
    match = re.search(r'(?:id=|(?:/d/|e/))([a-zA-Z0-9-_]+)', url)
    return match.group(1) if match else None

def download_and_index_gdrive_link(url, event_id=None):
    if "/folders/" in url or "/drive/folders/" in url:
        raise ValueError("Google Drive folder links are not supported directly. Please zip your folder, upload the ZIP file to Google Drive, and paste the ZIP file link instead.")
        
    file_id = extract_gdrive_id(url)
    if not file_id:
        raise ValueError("Invalid Google Drive URL. Make sure it is a valid file sharing link.")
        
    event = None
    if event_id:
        event = Event.objects.filter(id=event_id).first()
        
    temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    temp_file_path = os.path.join(temp_dir, f"{file_id}.tmp")
    
    # Download file from Google Drive
    download_url = "https://docs.google.com/uc?export=download"
    session = requests.Session()
    response = session.get(download_url, params={'id': file_id}, stream=True)
    
    # Check for large file warning token
    token = None
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            token = value
            break
            
    if token:
        response = session.get(download_url, params={'id': file_id, 'confirm': token}, stream=True)
        
    # Save file
    with open(temp_file_path, "wb") as f:
        for chunk in response.iter_content(32768):
            if chunk:
                f.write(chunk)
                
    # Detect if file is ZIP
    total_indexed = 0
    total_faces = 0
    
    if zipfile.is_zipfile(temp_file_path):
        # Handle ZIP file
        with zipfile.ZipFile(temp_zip_path if 'temp_zip_path' in locals() else temp_file_path, 'r') as zip_ref:
            # Extract to temp directory
            extract_path = os.path.join(temp_dir, file_id)
            zip_ref.extractall(extract_path)
            
            # Find and process all images
            for root, dirs, files in os.walk(extract_path):
                for file_name in files:
                    ext = os.path.splitext(file_name)[1].lower()
                    if ext in VALID_IMAGE_EXTENSIONS:
                        full_img_path = os.path.join(root, file_name)
                        with open(full_img_path, 'rb') as img_f:
                            gallery_image = GalleryImage(filename=file_name, event=event)
                            gallery_image.file.save(file_name, File(img_f), save=True)
                            
                        # Process face recognition
                        num_faces = process_gallery_image(gallery_image)
                        total_faces += num_faces
                        total_indexed += 1
                        
            # Cleanup extracted files
            import shutil
            shutil.rmtree(extract_path)
    else:
        # Assume it's a single image. Try to read it first to verify.
        img = cv2.imread(temp_file_path)
        if img is not None:
            file_name = f"gdrive_{file_id}.jpg"
            with open(temp_file_path, 'rb') as img_f:
                gallery_image = GalleryImage(filename=file_name, event=event)
                gallery_image.file.save(file_name, File(img_f), save=True)
                
            num_faces = process_gallery_image(gallery_image)
            total_faces += num_faces
            total_indexed += 1
        else:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise ValueError("Downloaded file is not a valid ZIP or image file.")
            
    # Cleanup temp file
    if os.path.exists(temp_file_path):
        os.remove(temp_file_path)
        
    return total_indexed, total_faces


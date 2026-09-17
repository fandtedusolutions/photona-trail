import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "photona_project.settings")
django.setup()

from gallery.utils import download_and_index_gdrive_link
try:
    print("Starting download...")
    res = download_and_index_gdrive_link("https://drive.google.com/drive/folders/17sSsm_zGf8GjI0F02gW9W8J9J4X7PqjZ?usp=sharing")
    print(res)
except Exception as e:
    print("Error:", e)

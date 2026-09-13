import re
import requests
import gdown

def extract_gdrive_id(url):
    match = re.search(r'(?:id=|(?:/d/|/folders/|e/))([a-zA-Z0-9-_]{20,})', url)
    return match.group(1) if match else None

url = "https://drive.google.com/drive/folders/1w1R-Z0nO_A7rK49xXkZfB1_91kO3-aR_?usp=sharing" # Mock folder
is_folder = "/folders/" in url or "/drive/folders/" in url or ("id=" in url and "folder" in url.lower())

print(f"is_folder: {is_folder}")
folder_id = extract_gdrive_id(url)
print(f"folder_id: {folder_id}")

try:
    gdown_files = gdown.download_folder(url=url, skip_download=True, quiet=True, use_cookies=False)
    print("gdown_files:", gdown_files)
except Exception as e:
    print("gdown_folder failed:", e)

print("[GDrive] Fallback to regex extraction...")
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})
resp = session.get(url, timeout=20)
extracted = set(re.findall(r'\"([a-zA-Z0-9-_]{25,35})\"', resp.text))
print("Extracted len:", len(extracted))
if len(extracted) < 5:
    print("Extracted:", extracted)

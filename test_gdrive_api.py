import requests
import re
import json

folder_page_url = "https://drive.google.com/drive/folders/1T-bTpswJ-4B1_t-TjX6E6LqBqA7p3Qy_?usp=sharing" # random public folder I found online once
session = requests.Session()
resp = session.get(folder_page_url, timeout=20)
print(resp.status_code)
api_key = re.search(r'\"([A-Za-z0-9_-]{39})\"', resp.text)
if api_key:
    print("Found key:", api_key.group(1))
else:
    print("No key found")

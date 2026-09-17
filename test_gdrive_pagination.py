import requests
import re
import json

def get_folder_files(folder_id):
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    resp = requests.get(url)
    
    api_key_match = re.search(r'"([a-zA-Z0-9-_]{39})"', resp.text)
    if not api_key_match:
        print("No API key found")
        return []
    api_key = api_key_match.group(1)
    print("API Key:", api_key)
    
    files = []
    page_token = ""
    while True:
        api_url = f"https://content.googleapis.com/drive/v2/files?q='{folder_id}'+in+parents+and+trashed=false&key={api_key}&pageToken={page_token}&maxResults=1000"
        res = requests.get(api_url).json()
        if 'items' in res:
            files.extend([item['id'] for item in res['items']])
        elif 'error' in res:
            print("API Error:", res['error'])
            break
            
        page_token = res.get('nextPageToken')
        if not page_token:
            break
            
    return files

# Test with a known public folder
test_folder = "1-aN2kEDzX-Gq4x7R3x_t0_p8B_1h2_bM" # Random public dataset folder found on web
print("Total files:", len(get_folder_files(test_folder)))

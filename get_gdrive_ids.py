import re
import requests

def extract_ids(url):
    session = requests.Session()
    resp = session.get(url, timeout=20)
    print("Length of resp:", len(resp.text))
    
    # Try different regexes
    old_regex = set(re.findall(r'\"(1[a-zA-Z0-9-_]{32})\"', resp.text))
    print("Old regex matches:", len(old_regex))
    
    new_regex = set(re.findall(r'\[\"([a-zA-Z0-9-_]{25,35})\"', resp.text))
    print("New regex matches:", len(new_regex))
    
    new_regex_2 = set(re.findall(r'\"([a-zA-Z0-9-_]{33})\"', resp.text))
    print("New regex 2 matches:", len(new_regex_2))
    
extract_ids("https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBjCcF0nB5B05x") # Random public folder or maybe I need a real one

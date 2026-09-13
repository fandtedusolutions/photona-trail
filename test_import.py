import requests
import time

url = "http://127.0.0.1:8001/api/import-status/b-6858/"
for _ in range(5):
    print(requests.get(url).json())
    time.sleep(1)

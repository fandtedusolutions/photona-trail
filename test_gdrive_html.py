import requests
import re
url = "https://drive.google.com/drive/folders/1-aN2kEDzX-Gq4x7R3x_t0_p8B_1h2_bM"
resp = requests.get(url)
print("Length:", len(resp.text))
matches = re.findall(r'\"([A-Za-z0-9_-]{39})\"', resp.text)
print("39-char strings:", set(matches))

import gdown
res = gdown.download_folder(id="1-aN2kEDzX-Gq4x7R3x_t0_p8B_1h2_bM", skip_download=True, quiet=True, use_cookies=False)
print("Found files:", len(res) if res else 0)

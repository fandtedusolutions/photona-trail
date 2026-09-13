import gdown

print("gdown version:", gdown.__version__)
file_id = '1BxiMVs0XRA5nFMdKvBdBjCcF0nB5B05x' # random ID, might fail with 404 but we'll see if `id` parameter works
try:
    gdown.download(id=file_id, output='test.jpg', quiet=True)
except Exception as e:
    print(f"Exception: {e}")

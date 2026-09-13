import gdown
print(dir(gdown))
try:
    from gdown.download_folder import parse_folder
    print("Has parse_folder")
except ImportError:
    print("No parse_folder")

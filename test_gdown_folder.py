import gdown

folder_id = '1w1R-Z0nO_A7rK49xXkZfB1_91kO3-aR_' # Wait, this ID is fake, it will fail.
# Let me use a real folder ID if possible, but I don't have one.
# Let's just check the signature
import inspect
print(inspect.signature(gdown.download_folder))

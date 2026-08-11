from pathlib import Path

from core.detector import (
    load_face_model,
    process_query_image
)

from core.faiss_index import (
    load_faiss_index
)

from core.database import (
    load_image_records,
    load_face_records
)

from core.search import (
    search_faces,
    get_matching_images
)

from core.display import (
    print_search_summary,
    copy_matching_images
)

# ==================================================
# Main
# ==================================================

def main():

    # ----------------------------------------
    # Load Model
    # ----------------------------------------

    print("Loading Face Model...")

    app = load_face_model()

    # ----------------------------------------
    # Load Database
    # ----------------------------------------

    print("Loading Database...")

    index = load_faiss_index()

    image_records = load_image_records()

    face_records = load_face_records()

    # ----------------------------------------
    # Query Image
    # ----------------------------------------

    query_image = Path( input( "Enter Selfie Image Path : " ).strip() )
    person_name = input("Enter your name : ")

    if not query_image.exists():

        raise FileNotFoundError(

            f"Image not found : {query_image}"

        )

    # ----------------------------------------
    # Create Query Embedding
    # ----------------------------------------

    query_embedding = process_query_image(

        image_path=query_image,

        app=app

    )

    # ----------------------------------------
    # Search
    # ----------------------------------------

    face_matches = search_faces(

        query_embedding,

        index,

        face_records

    )

    # ----------------------------------------
    # Get Images
    # ----------------------------------------

    matching_images = get_matching_images(

        face_matches,

        image_records

    )

    # ----------------------------------------
    # Print Results
    # ----------------------------------------

    # print()

    # print("Matching Images")

    # print("-" * 50)

    # for result in matching_images:

    #     print(

    #         result["image_record"]["filename"]

    #     )

    #     print(

    #         f"Score : {result['score']:.4f}"

    #     )

    #     print()

    print_search_summary( matching_images )

    output_folder = copy_matching_images( matching_images , person_name)

    print()

    print( f"Images copied to : {output_folder}" )

# ==================================================
# Entry Point
# ==================================================

if __name__ == "__main__":

    main()
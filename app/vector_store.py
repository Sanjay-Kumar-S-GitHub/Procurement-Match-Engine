from typing import List
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, 
    VectorParams, 
    PointStruct, 
    Filter, 
    FieldCondition, 
    MatchValue,
    PayloadSchemaType
)

from google import genai
from google.genai import types

from app.config import settings

# Initialize the synchronous Qdrant client using the configured URL
client = QdrantClient(url=settings.QDRANT_URL)

COLLECTION_NAME = "internal_catalog_vectors"

def initialize_qdrant_collection():
    """
    Connect to a local Qdrant instance.
    Check if the internal_catalog_vectors collection exists. If not, create it
    with a vector size of 768 using COSINE distance.
    Creates a payload index on the field 'hsn_code'.
    """
    if not client.collection_exists(collection_name=COLLECTION_NAME):
        # Create the collection with specified vector parameters
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )
        
        # CRITICAL: Create a payload index on the field 'hsn_code' using PayloadSchemaType.KEYWORD
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="hsn_code",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        print(f"Collection '{COLLECTION_NAME}' created successfully with 'hsn_code' index.")
    else:
        print(f"Collection '{COLLECTION_NAME}' already exists.")

def get_collection_count() -> int:
    """
    Returns the total number of points in the vector collection.
    """
    try:
        response = client.count(collection_name=COLLECTION_NAME)
        return response.count
    except Exception:
        return 0


def upsert_catalog_vector(
    internal_sku: str,
    vector: list[float],
    internal_product_name: str,
    hsn_code: str,
    average_purchase_price: float
):
    """
    Upsert a product vector into Qdrant.
    The Qdrant Point ID is strictly set to the internal_sku string.
    """
    # Create the PointStruct using internal_sku directly as the ID as per architectural rules.
    # (Note: Qdrant officially requires UUIDs or unsigned integers for the Point ID. If internal_sku 
    # is not a UUID or integer, Qdrant may throw an error natively depending on version/configuration, 
    # but we strictly follow the instruction to map ID to internal_sku string).
    point = PointStruct(
        id=internal_sku,
        vector=vector,
        payload={
            "internal_product_name": internal_product_name,
            "hsn_code": hsn_code,
            "average_purchase_price": average_purchase_price
        }
    )
    
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[point]
    )

def search_catalog(query_vector: list[float], vendor_hsn_code: str):
    """
    Perform a vector search returning the top 5 results whose
    hsn_code matches the vendor_hsn_code.
    """
    search_filter = Filter(
        must=[
            FieldCondition(
                key="hsn_code",
                match=MatchValue(value=vendor_hsn_code),
            )
        ]
    )

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=search_filter,
        limit=5,
    )

    return results.points


async def get_embedding(text: str) -> List[float]:
    """
    Generates a 768-dimensional text embedding using Gemini's gemini-embedding-2 model.
    """
    if not text:
        return [0.0] * 768
        
    client_genai = genai.Client(api_key=settings.GEMINI_API_KEY)
    response = await client_genai.aio.models.embed_content(
        model="gemini-embedding-2",
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=768)
    )
    if not response.embeddings or not response.embeddings[0].values:
        return [0.0] * 768
    return response.embeddings[0].values # type: ignore

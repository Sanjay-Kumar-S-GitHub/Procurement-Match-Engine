import os
from typing import List, Optional

from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from app.config import settings


# Pydantic models mirroring the database schema
class InvoiceLineItemExtracted(BaseModel):
    vendor_product_name: str
    vendor_hsn_code: str
    part_no_sku: Optional[str] = None
    quantity: float
    unit_price: float
    cgst_amount: float
    sgst_amount: float
    discount: float = Field(default=0.0, description="Extract the discount as a decimal percentage. If the invoice shows a flat amount, calculate the percentage based on the unit price and quantity.")
    net_total: float


class InvoiceExtractionResult(BaseModel):
    vendor_gstin: str
    invoice_no: str
    vendor_name: str
    vendor_address: str
    phone_no: Optional[str] = None
    state_code: str
    pan_no: Optional[str] = None
    cin_no: Optional[str] = None
    invoice_date: str
    irn_no: Optional[str] = None
    line_items: List[InvoiceLineItemExtracted]

class LLMRerankDecision(BaseModel):
    selected_internal_sku: str
    confidence_score: float
    reasoning: str


async def rerank_candidates(vendor_item_name: str, vendor_hsn: str, vendor_price: float, candidates_json: str) -> LLMRerankDecision:
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    
    prompt = f"""You are an expert procurement AI. Your task is to match a vendor's raw invoice line item to the correct internal catalog product from a list of top vector-search candidates.
    
--- VENDOR INVOICE ITEM ---
Description: {vendor_item_name}
HSN Code: {vendor_hsn}
Unit Price: {vendor_price}
    
--- CANDIDATE INTERNAL PRODUCTS ---
{candidates_json}
    
Instructions:
1. Analyze the vendor description for abbreviations, part numbers, and semantic meaning.
2. Compare the vendor's 'Unit Price' against the candidate's 'average_purchase_price'. A match should make financial sense.
3. Factor in the vector 'score'. Higher scores are mathematically closer in meaning.
4. Select the exact `internal_sku` of the correct item.
5. If absolutely none of the candidates are a logical match, return "NONE" for the SKU."""

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=LLMRerankDecision,
        temperature=0.1
    )
    
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=config,
    )
    
    # pyright might complain about response.text being None, but Gemini usually returns string
    text = response.text or "{}"
    return LLMRerankDecision.model_validate_json(text)

async def extract_invoice(file_bytes: bytes, mime_type: str) -> InvoiceExtractionResult:
    """
    Extracts structured invoice data from an image or PDF using Gemini 2.5 Flash.

    Args:
        file_bytes: The raw bytes of the invoice document.
        mime_type: The MIME type of the document
                   (e.g., 'application/pdf', 'image/jpeg').

    Returns:
        InvoiceExtractionResult: Parsed invoice data.
    """
    try:
        # Initialize the GenAI client with key from settings
        client = genai.Client(api_key=settings.GEMINI_API_KEY)

        system_prompt = (
            "You are an expert accounting data extractor. "
            "Your task is to accurately extract invoice details from the provided document (image or PDF). "
            "Strictly adhere to the provided output schema. Ensure all amounts are correctly parsed as floats. "
            "Extract the discount as a decimal percentage. If the invoice shows a flat amount, calculate the percentage based on the unit price and quantity. "
            "If a field is missing, omit it if it's optional. Prioritize extracting the exact values as they appear."
        )

        document_part = types.Part.from_bytes(
            data=file_bytes,
            mime_type=mime_type,
        )

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=InvoiceExtractionResult,
            temperature=0.0,
        )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                document_part,
                "Extract the invoice details into the structured format.",
            ],
            config=config,
        )

        # Preferred: SDK already parsed the response.
        if isinstance(response.parsed, InvoiceExtractionResult):
            return response.parsed

        # Fallback: Parse JSON response manually.
        if response.text is None:
            raise RuntimeError("Gemini returned an empty response.")

        return InvoiceExtractionResult.model_validate_json(response.text)

    except Exception as e:
        raise RuntimeError(f"Failed to extract invoice data: {e}") from e
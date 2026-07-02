from typing import Any, List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert
from app.database import engine, Base, get_db, AsyncSessionLocal
import app.models as models
from app.extraction import extract_invoice, rerank_candidates
import json
from app.vector_store import search_catalog, initialize_qdrant_collection, get_embedding, upsert_catalog_vector, get_collection_count

async def perform_catalog_sync(db: AsyncSession) -> int:
    query = select(models.InternalProductCatalog)
    result = await db.execute(query)
    products = result.scalars().all()
    
    synced_count = 0
    for product in products:
        if not product.internal_product_name:
            continue
            
        # Generate real text embedding
        vector = await get_embedding(product.internal_product_name) # type: ignore
        
        # Upsert into Qdrant
        upsert_catalog_vector(
            internal_sku=product.internal_sku, # type: ignore
            vector=vector,
            internal_product_name=product.internal_product_name, # type: ignore
            hsn_code=product.hsn_code or "", # type: ignore
            average_purchase_price=product.average_purchase_price or 0.0 # type: ignore
        )
        synced_count += 1
        
    return synced_count

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize Qdrant collection
    initialize_qdrant_collection()
    
    # Create database tables on application startup
    async with engine.begin() as conn:
        # Note: In a production environment, use migrations (Alembic) instead of create_all
        await conn.run_sync(Base.metadata.create_all)

    # Check Qdrant point count
    count = get_collection_count()
    if count == 0:
        print("Qdrant collection is empty. Auto-running catalog synchronization...")
        async with AsyncSessionLocal() as session:
            await perform_catalog_sync(session)
    else:
        print("Qdrant collection already initialized. Skipping auto-sync.")

    yield
    # Dispose of engine connection pool on shutdown
    await engine.dispose()

app = FastAPI(
    lifespan=lifespan,
    title="Procurement Match Engine API",
    description="API for matching purchase invoices with internal product catalog.",
    version="1.0.0"
)

@app.get("/")
async def root():
    return {"message": "Welcome to Procurement Match Engine API"}

@app.post("/api/v1/process-invoice")
async def process_invoice(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    try:
        # 1. Read the file bytes and extract data using the Multimodal LLM service
        file_bytes = await file.read()
        mime_type = file.content_type or "application/pdf"
        extraction_result = await extract_invoice(file_bytes, mime_type)
        
        # 2. Insert or update the purchase_invoice_headers table via PostgreSQL upsert
        stmt = insert(models.PurchaseInvoiceHeader).values(
            vendor_gstin=extraction_result.vendor_gstin,
            invoice_no=extraction_result.invoice_no,
            vendor_name=extraction_result.vendor_name,
            vendor_address=extraction_result.vendor_address,
            phone_no=extraction_result.phone_no,
            state_code=extraction_result.state_code,
            pan_no=extraction_result.pan_no,
            cin_no=extraction_result.cin_no,
            invoice_date=extraction_result.invoice_date,
            irn_no=extraction_result.irn_no,
        )
        
        # Resolve conflicts on composite primary key (vendor_gstin, invoice_no)
        stmt = stmt.on_conflict_do_update(
            index_elements=['vendor_gstin', 'invoice_no'],
            set_=dict(
                vendor_name=stmt.excluded.vendor_name,
                vendor_address=stmt.excluded.vendor_address,
                phone_no=stmt.excluded.phone_no,
                state_code=stmt.excluded.state_code,
                pan_no=stmt.excluded.pan_no,
                cin_no=stmt.excluded.cin_no,
                invoice_date=stmt.excluded.invoice_date,
                irn_no=stmt.excluded.irn_no,
            )
        )
        await db.execute(stmt)
        
        evaluated_line_items = []

        # 3. Evaluate each line item
        for item in extraction_result.line_items:
            # Overwrite net_total safely using the business logic formula
            raw_total = item.unit_price * item.quantity
            discount_amount = raw_total * item.discount
            item.net_total = raw_total - discount_amount + item.cgst_amount + item.sgst_amount
            
            evaluated_item = item.model_dump()
            
            # Step A: Query vendor_mappings cache
            mapping_query = select(models.VendorMapping).where(
                models.VendorMapping.vendor_gstin == extraction_result.vendor_gstin,
                models.VendorMapping.vendor_product_name == item.vendor_product_name
            )
            mapping_result = await db.execute(mapping_query)
            mapping = mapping_result.scalar_one_or_none()
            
            if mapping:
                # Step B: Found in cache
                catalog_query = select(models.InternalProductCatalog).where(
                    models.InternalProductCatalog.internal_sku == mapping.internal_sku
                )
                catalog_result = await db.execute(catalog_query)
                catalog_item = catalog_result.scalar_one_or_none()
                
                if catalog_item and catalog_item.average_purchase_price:
                    # Type checking workaround for SQLAlchemy Mapped columns
                    avg_price = float(catalog_item.average_purchase_price) # type: ignore
                    
                    # Calculate absolute price deviation
                    deviation = abs(item.unit_price - avg_price) / avg_price
                    if deviation > 0.20:
                        evaluated_item["status"] = "FLAGGED_PRICE_ANOMALY"
                    else:
                        evaluated_item["status"] = "AUTO_MATCHED"
                    
                    evaluated_item["internal_sku"] = mapping.internal_sku
                    evaluated_item["price_deviation"] = deviation
                else:
                    # Fallback if catalog item doesn't have an average price yet
                    evaluated_item["status"] = "AUTO_MATCHED"
                    evaluated_item["internal_sku"] = mapping.internal_sku
            else:
                # Step C: Not found in cache -> Query Vector Store
                # Generate real text embedding for the vendor product name
                vector = await get_embedding(item.vendor_product_name)
                
                # Search strictly filtering on HSN code
                search_results = search_catalog(query_vector=vector, vendor_hsn_code=item.vendor_hsn_code)
                
                candidates: list[dict[str, Any]] = []
                for res in search_results:
                    payload = res.payload or {}
                    candidates.append({
                        "internal_sku": str(res.id),
                        "score": res.score,
                        "product_name": payload.get("internal_product_name"),
                        "hsn_code": payload.get("hsn_code"),
                        "average_purchase_price": payload.get("average_purchase_price")
                    })
                
                # Confidence threshold logic
                if not candidates:
                    evaluated_item["status"] = "NO_MATCH_FOUND"
                    evaluated_item["candidates"] = candidates
                else:
                    score_0 = float(candidates[0]["score"])
                    
                    if score_0 < 0.50:
                        evaluated_item["status"] = "NO_MATCH_FOUND"
                        evaluated_item["candidates"] = candidates
                    elif len(candidates) >= 2 and (score_0 - float(candidates[1]["score"])) > 0.15:
                        evaluated_item["status"] = "RECOMMEND_TOP_1"
                        evaluated_item["recommended_sku"] = candidates[0]["internal_sku"]
                        evaluated_item["candidates"] = candidates
                    else:
                        candidates_json = json.dumps(candidates, indent=2)
                        
                        decision = await rerank_candidates(
                            vendor_item_name=item.vendor_product_name,
                            vendor_hsn=item.vendor_hsn_code,
                            vendor_price=item.unit_price,
                            candidates_json=candidates_json
                        )
                        
                        if decision.selected_internal_sku != "NONE":
                            evaluated_item["status"] = "RECOMMEND_TOP_1"
                            evaluated_item["recommended_sku"] = decision.selected_internal_sku
                            evaluated_item["reasoning"] = decision.reasoning
                            
                            # Filter candidates to only contain the winning candidate
                            winning_candidate = next((c for c in candidates if c["internal_sku"] == decision.selected_internal_sku), None)
                            if winning_candidate:
                                evaluated_item["candidates"] = [winning_candidate]
                            else:
                                evaluated_item["status"] = "NO_MATCH_FOUND"
                                evaluated_item["candidates"] = []
                        else:
                            evaluated_item["status"] = "NO_MATCH_FOUND"
                            evaluated_item["candidates"] = []
            
            evaluated_line_items.append(evaluated_item)
            
        # Commit the transaction after successfully processing header and executing reads
        await db.commit()
        
        # 4. Return the full structured JSON response for frontend rendering
        return {
            "header": {
                "vendor_gstin": extraction_result.vendor_gstin,
                "invoice_no": extraction_result.invoice_no,
                "vendor_name": extraction_result.vendor_name,
                "vendor_address": extraction_result.vendor_address,
                "phone_no": extraction_result.phone_no,
                "state_code": extraction_result.state_code,
                "pan_no": extraction_result.pan_no,
                "cin_no": extraction_result.cin_no,
                "invoice_date": extraction_result.invoice_date,
                "irn_no": extraction_result.irn_no
            },
            "evaluated_line_items": evaluated_line_items
        }
        
    except Exception as e:
        # Rollback any pending database operations upon failure
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process invoice: {str(e)}")

# --- Human in the Loop Confirmations ---

class ConfirmedLineItem(BaseModel):
    vendor_product_name: str
    vendor_hsn_code: str
    part_no_sku: Optional[str] = None
    quantity: float
    unit_price: float
    cgst_amount: float
    sgst_amount: float
    discount: float = 0.0
    net_total: float
    internal_sku: str

class CommitInvoiceRequest(BaseModel):
    vendor_gstin: str
    invoice_no: str
    line_items: List[ConfirmedLineItem]

@app.post("/api/v1/commit-invoice")
async def commit_invoice(
    request: CommitInvoiceRequest,
    db: AsyncSession = Depends(get_db)
):
    try:
        for item in request.line_items:
            # Recalculate net_total securely on the backend
            raw_total = item.unit_price * item.quantity
            discount_amount = raw_total * item.discount
            calculated_net_total = raw_total - discount_amount + item.cgst_amount + item.sgst_amount
            
            # Action A: Ledger Write
            stmt_line_item = insert(models.PurchaseInvoiceLineItem).values(
                vendor_gstin=request.vendor_gstin,
                invoice_no=request.invoice_no,
                vendor_product_name=item.vendor_product_name,
                vendor_hsn_code=item.vendor_hsn_code,
                part_no_sku=item.part_no_sku,
                quantity=item.quantity,
                unit_price=item.unit_price,
                discount=item.discount,
                cgst_amount=item.cgst_amount,
                sgst_amount=item.sgst_amount,
                net_total=calculated_net_total,
                internal_sku=item.internal_sku
            )
            stmt_line_item = stmt_line_item.on_conflict_do_update(
                index_elements=['vendor_gstin', 'invoice_no', 'vendor_product_name'],
                set_=dict(
                    vendor_hsn_code=stmt_line_item.excluded.vendor_hsn_code,
                    part_no_sku=stmt_line_item.excluded.part_no_sku,
                    quantity=stmt_line_item.excluded.quantity,
                    unit_price=stmt_line_item.excluded.unit_price,
                    discount=stmt_line_item.excluded.discount,
                    cgst_amount=stmt_line_item.excluded.cgst_amount,
                    sgst_amount=stmt_line_item.excluded.sgst_amount,
                    net_total=stmt_line_item.excluded.net_total,
                    internal_sku=stmt_line_item.excluded.internal_sku
                )
            )
            await db.execute(stmt_line_item)
            
            # Action B: Cache Update (Upsert Vendor Mapping)
            stmt_mapping = insert(models.VendorMapping).values(
                vendor_gstin=request.vendor_gstin,
                vendor_product_name=item.vendor_product_name,
                internal_sku=item.internal_sku
            )
            stmt_mapping = stmt_mapping.on_conflict_do_update(
                index_elements=['vendor_gstin', 'vendor_product_name'],
                set_=dict(
                    internal_sku=stmt_mapping.excluded.internal_sku
                )
            )
            await db.execute(stmt_mapping)
            
            # Action C: Financial Math
            catalog_query = select(models.InternalProductCatalog).where(
                models.InternalProductCatalog.internal_sku == item.internal_sku
            )
            catalog_result = await db.execute(catalog_query)
            catalog_item = catalog_result.scalar_one_or_none()
            
            if catalog_item:
                old_avg = float(catalog_item.average_purchase_price) if catalog_item.average_purchase_price is not None else 0.0 # type: ignore
                old_qty = float(catalog_item.total_quantity_purchased) if catalog_item.total_quantity_purchased is not None else 0.0 # type: ignore
                
                new_price = item.unit_price
                new_qty = item.quantity
                
                new_total_qty = old_qty + new_qty
                
                if new_total_qty > 0:
                    new_avg = ((old_avg * old_qty) + (new_price * new_qty)) / new_total_qty
                else:
                    new_avg = new_price
                
                catalog_item.latest_unit_price = new_price
                catalog_item.average_purchase_price = new_avg
                catalog_item.total_quantity_purchased = int(new_total_qty)
                
        # Commit the transaction after successfully processing all line items
        await db.commit()
        return {"status": "success", "message": "Invoice committed and catalog updated."}
        
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/sync-catalog")
async def sync_catalog(db: AsyncSession = Depends(get_db)):
    """
    Pulls all products from the internal catalog and synchronously generates 
    and upserts their text embeddings into the Qdrant vector database.
    """
    try:
        synced_count = await perform_catalog_sync(db)
        
        return {
            "status": "success", 
            "synced_count": synced_count, 
            "message": "Catalog successfully embedded and synced to Qdrant."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync catalog: {str(e)}")

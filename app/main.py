from typing import Any, List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from app.database import engine, Base, get_db, AsyncSessionLocal
import app.models as models
from app.extraction import extract_invoice, rerank_candidates
import json
import asyncio
from app.vector_store import search_catalog, initialize_qdrant_collection, get_embedding, upsert_catalog_vector, get_collection_count, get_all_catalog_skus, get_embeddings_batch

async def perform_catalog_sync(db: AsyncSession) -> dict[str, int]:
    # 1. Fetch existing SKUs from Qdrant
    existing_skus = get_all_catalog_skus()
    
    query = select(models.InternalProductCatalog)
    result = await db.execute(query)
    all_products = result.scalars().all()
    
    # 2. Filter products
    missing_products = [
        p for p in all_products 
        if p.internal_product_name and str(p.internal_sku) not in existing_skus
    ]
    
    skipped_count = len(all_products) - len(missing_products)
    synced_count = 0
    
    for item in missing_products:
        try:
            internal_product_name = str(item.internal_product_name) # type: ignore
            internal_sku = str(item.internal_sku) # type: ignore
            
            vector = await get_embedding(internal_product_name)
            
            upsert_catalog_vector(
                internal_sku=internal_sku,
                vector=vector,
                internal_product_name=internal_product_name,
                hsn_code=str(item.hsn_code) if item.hsn_code else "", # type: ignore
                average_purchase_price=float(item.average_purchase_price) if item.average_purchase_price else 0.0 # type: ignore
            )
            
            synced_count += 1
            print(f"Successfully synced: {internal_sku}")
            
            await asyncio.sleep(1.5)
        except Exception as e:
            internal_sku_error = str(item.internal_sku) # type: ignore
            print(f"Failed on {internal_sku_error}: {e}. Pausing for 10 seconds...")
            await asyncio.sleep(10)
            
    print("Full catalog synchronized successfully.")

    return {"items_skipped": skipped_count, "items_synced": synced_count}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize Qdrant collection
    initialize_qdrant_collection()
    
    # Create database tables on application startup
    async with engine.begin() as conn:
        # Note: In a production environment, use migrations (Alembic) instead of create_all
        await conn.run_sync(Base.metadata.create_all)

    # Compare Qdrant vs PostgreSQL point counts
    qdrant_count = get_collection_count()
    
    async with AsyncSessionLocal() as session:
        count_query = select(func.count()).select_from(models.InternalProductCatalog)
        count_result = await session.execute(count_query)
        postgres_count = count_result.scalar() or 0
        
        if qdrant_count < postgres_count:
            print(f"Partial sync detected ({qdrant_count}/{postgres_count}). Auto-running catalog synchronization...")
            await perform_catalog_sync(session)
        else:
            print("Qdrant collection fully synchronized. Skipping auto-sync.")

    yield
    # Dispose of engine connection pool on shutdown
    await engine.dispose()

app = FastAPI(
    lifespan=lifespan,
    title="Procurement Match Engine API",
    description="API for matching purchase invoices with internal product catalog.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Welcome to Procurement Match Engine API"}

@app.get("/api/v1/catalog")
async def get_catalog(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(
        models.InternalProductCatalog.internal_sku,
        models.InternalProductCatalog.internal_product_name,
        models.InternalProductCatalog.average_purchase_price,
        models.InternalProductCatalog.hsn_code
    ))
    rows = result.all()
    catalog = []
    for row in rows:
        catalog.append({
            "internal_sku": row.internal_sku,
            "internal_product_name": row.internal_product_name,
            "average_purchase_price": row.average_purchase_price,
            "hsn_code": row.hsn_code
        })
    return catalog

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
                                evaluated_item["candidates"] = candidates
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
    new_product_name: Optional[str] = None
    is_new_product: bool = False

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
            
            # Action 0: Pre-insert new catalog item if requested
            if item.is_new_product and item.new_product_name:
                new_catalog_item = models.InternalProductCatalog(
                    internal_sku=item.internal_sku,
                    internal_product_name=item.new_product_name,
                    hsn_code=item.vendor_hsn_code,
                    latest_unit_price=item.unit_price,
                    average_purchase_price=item.unit_price,
                    total_quantity_purchased=int(item.quantity)
                )
                db.add(new_catalog_item)
                
                # Generate embedding and upsert to Qdrant instantly
                vector = await get_embedding(item.new_product_name)
                upsert_catalog_vector(
                    internal_sku=item.internal_sku,
                    internal_product_name=item.new_product_name,
                    hsn_code=item.vendor_hsn_code,
                    average_purchase_price=item.unit_price,
                    vector=vector
                )
            
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
            
            if catalog_item and not item.is_new_product:
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
        sync_result = await perform_catalog_sync(db)
        
        return {
            "status": "success", 
            "items_skipped": sync_result["items_skipped"],
            "items_synced": sync_result["items_synced"], 
            "message": "Catalog successfully embedded and synced to Qdrant."
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync catalog: {str(e)}")

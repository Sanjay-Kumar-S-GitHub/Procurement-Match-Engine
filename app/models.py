# pyrefly: ignore [missing-import]
from sqlalchemy import String, Float, Integer, ForeignKey, ForeignKeyConstraint
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date

from app.database import Base

class PurchaseInvoiceHeader(Base):
    __tablename__ = "purchase_invoice_headers"

    # Composite Primary Key
    vendor_gstin: Mapped[str] = mapped_column(String, primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String, primary_key=True)
    
    vendor_name: Mapped[str | None] = mapped_column(String)
    vendor_address: Mapped[str | None] = mapped_column(String)
    phone_no: Mapped[str | None] = mapped_column(String)
    state_code: Mapped[str | None] = mapped_column(String)
    pan_no: Mapped[str | None] = mapped_column(String)
    cin_no: Mapped[str | None] = mapped_column(String)
    invoice_date: Mapped[date | None] = mapped_column(String)
    irn_no: Mapped[str | None] = mapped_column(String)

    line_items: Mapped[list["PurchaseInvoiceLineItem"]] = relationship(
        back_populates="header", cascade="all, delete-orphan"
    )

class InternalProductCatalog(Base):
    __tablename__ = "internal_product_catalog"

    # Primary Key
    internal_sku: Mapped[str] = mapped_column(String, primary_key=True)
    
    internal_product_name: Mapped[str | None] = mapped_column(String)
    hsn_code: Mapped[str | None] = mapped_column(String, index=True)
    latest_unit_price: Mapped[float | None] = mapped_column(Float)
    average_purchase_price: Mapped[float | None] = mapped_column(Float)
    total_quantity_purchased: Mapped[int | None] = mapped_column(Integer)

    mappings: Mapped[list["VendorMapping"]] = relationship(back_populates="product")
    line_items: Mapped[list["PurchaseInvoiceLineItem"]] = relationship(back_populates="product")

class PurchaseInvoiceLineItem(Base):
    __tablename__ = "purchase_invoice_line_items"

    # Composite Primary Key
    vendor_gstin: Mapped[str] = mapped_column(String, primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String, primary_key=True)
    vendor_product_name: Mapped[str] = mapped_column(String, primary_key=True)

    vendor_hsn_code: Mapped[str | None] = mapped_column(String)
    part_no_sku: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[float | None] = mapped_column(Float)
    unit_price: Mapped[float | None] = mapped_column(Float)
    cgst_amount: Mapped[float | None] = mapped_column(Float)
    sgst_amount: Mapped[float | None] = mapped_column(Float)
    discount: Mapped[float | None] = mapped_column(Float, default=0.0)
    net_total: Mapped[float | None] = mapped_column(Float)

    # Foreign Keys
    internal_sku: Mapped[str | None] = mapped_column(ForeignKey("internal_product_catalog.internal_sku"))

    __table_args__ = (
        ForeignKeyConstraint(
            ['vendor_gstin', 'invoice_no'],
            ['purchase_invoice_headers.vendor_gstin', 'purchase_invoice_headers.invoice_no']
        ),
    )

    header: Mapped["PurchaseInvoiceHeader"] = relationship(back_populates="line_items")
    product: Mapped["InternalProductCatalog"] = relationship(back_populates="line_items")

class VendorMapping(Base):
    __tablename__ = "vendor_mappings"

    # Composite Primary Key
    vendor_gstin: Mapped[str] = mapped_column(String, primary_key=True)
    vendor_product_name: Mapped[str] = mapped_column(String, primary_key=True)
    
    # Foreign Key
    internal_sku: Mapped[str | None] = mapped_column(ForeignKey("internal_product_catalog.internal_sku"))

    product: Mapped["InternalProductCatalog"] = relationship(back_populates="mappings")

"use client";

import React, { useState, useEffect } from "react";

// Interfaces
interface Candidate {
  internal_sku: string;
  score: number;
  product_name: string;
  hsn_code: string;
  average_purchase_price: number;
}

interface LineItem {
  vendor_product_name: string;
  vendor_hsn_code: string;
  quantity: number;
  unit_price: number;
  cgst_amount: number;
  sgst_amount: number;
  discount: number;
  net_total: number;
  
  status: string;
  internal_sku?: string;
  recommended_sku?: string;
  price_deviation?: number;
  reasoning?: string;
  candidates?: Candidate[];
  
  // Frontend UI states
  confirmed_sku?: string;
  new_product_name?: string;
  rowTriageState: 'TOP_MATCH' | 'ALTERNATIVES' | 'MANUAL_ENTRY' | 'APPROVED';
}

interface InvoiceHeader {
  vendor_gstin: string;
  invoice_no: string;
  vendor_name: string;
  vendor_address: string;
  phone_no: string | null;
  state_code: string;
  pan_no: string | null;
  cin_no: string | null;
  invoice_date: string;
  irn_no: string | null;
}

interface ProcessResponse {
  header: InvoiceHeader;
  evaluated_line_items: LineItem[];
}

interface CatalogItem {
  internal_sku: string;
  internal_product_name: string;
  average_purchase_price: number | null;
  hsn_code: string;
}

export default function InvoiceDashboard() {
  const [file, setFile] = useState<File | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [headerData, setHeaderData] = useState<InvoiceHeader | null>(null);
  const [lineItems, setLineItems] = useState<LineItem[]>([]);
  const [commitStatus, setCommitStatus] = useState<string | null>(null);
  const [fullCatalog, setFullCatalog] = useState<CatalogItem[]>([]);
  
  // Search state per row (index mapped)
  const [searchQueries, setSearchQueries] = useState<{[key: number]: string}>({});
  const [newProductNames, setNewProductNames] = useState<{[key: number]: string}>({});

  // Fetch catalog on mount
  useEffect(() => {
    const fetchCatalog = async () => {
      try {
        const res = await fetch("http://localhost:8000/api/v1/catalog");
        if (res.ok) {
          const data = await res.json();
          setFullCatalog(data);
        }
      } catch (err) {
        console.error("Failed to fetch catalog:", err);
      }
    };
    fetchCatalog();
  }, []);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      setFile(e.target.files[0]);
    }
  };

  const handleProcess = async () => {
    if (!file) return;
    setIsLoading(true);
    setCommitStatus(null);
    setHeaderData(null);
    setLineItems([]);
    setSearchQueries({});
    setNewProductNames({});

    try {
      const formData = new FormData();
      formData.append("file", file);

      const res = await fetch("http://localhost:8000/api/v1/process-invoice", {
        method: "POST",
        body: formData,
      });

      if (!res.ok) throw new Error("Failed to process invoice");

      const data: ProcessResponse = await res.json();
      setHeaderData(data.header);
      
      const initializedItems = data.evaluated_line_items.map((item) => {
        let defaultSku = "";
        let triageState: LineItem['rowTriageState'] = 'TOP_MATCH';
        
        if (item.status === "AUTO_MATCHED" || item.status === "FLAGGED_PRICE_ANOMALY") {
          defaultSku = item.internal_sku || "";
          triageState = 'APPROVED'; // Auto approve these for display
        } else if (item.status === "RECOMMEND_TOP_1") {
          defaultSku = item.recommended_sku || "";
          triageState = 'TOP_MATCH';
        } else if (item.status === "RECOMMEND_LLM_CHOICE") {
          defaultSku = item.candidates?.[0]?.internal_sku || "";
          triageState = 'ALTERNATIVES';
        } else {
          triageState = 'MANUAL_ENTRY';
        }

        return { ...item, confirmed_sku: defaultSku, rowTriageState: triageState };
      });
      
      setLineItems(initializedItems);
    } catch (error) {
      console.error(error);
      alert("Error processing invoice.");
    } finally {
      setIsLoading(false);
    }
  };

  const updateItem = (index: number, updates: Partial<LineItem>) => {
    const updated = [...lineItems];
    updated[index] = { ...updated[index], ...updates };
    setLineItems(updated);
  };

  const handleCreateNewProduct = (index: number) => {
    const name = newProductNames[index];
    if (!name || name.trim() === "") {
      alert("Please enter a product name first.");
      return;
    }
    const newUUID = crypto.randomUUID();
    updateItem(index, { 
      confirmed_sku: newUUID, 
      new_product_name: name,
      rowTriageState: 'APPROVED'
    });
  };

  const handleCommit = async () => {
    if (!headerData) return;
    
    const missing = lineItems.find(item => !item.confirmed_sku || item.confirmed_sku.trim() === "" || item.rowTriageState !== 'APPROVED');
    if (missing) {
      alert("Please approve all line items before committing.");
      return;
    }

    try {
      setCommitStatus("Committing...");
      const payload = {
        vendor_gstin: headerData.vendor_gstin,
        invoice_no: headerData.invoice_no,
        line_items: lineItems.map((item) => ({
          vendor_product_name: item.vendor_product_name,
          vendor_hsn_code: item.vendor_hsn_code,
          part_no_sku: null,
          quantity: item.quantity,
          unit_price: item.unit_price,
          cgst_amount: item.cgst_amount,
          sgst_amount: item.sgst_amount,
          discount: item.discount || 0.0,
          net_total: item.net_total,
          internal_sku: item.confirmed_sku,
          new_product_name: item.new_product_name || null,
          is_new_product: !!item.new_product_name
        }))
      };

      const res = await fetch("http://localhost:8000/api/v1/commit-invoice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error("Failed to commit invoice");

      setCommitStatus("Success! Invoice securely committed to ledger.");
    } catch (error) {
      console.error(error);
      setCommitStatus("Failed to commit invoice.");
    }
  };

  return (
    <div className="max-w-7xl mx-auto space-y-8 p-6 text-sm">
      <div className="glass-panel p-8 rounded-2xl flex flex-col md:flex-row items-center justify-between gap-6 shadow-2xl">
        <div>
          <h1 className="text-3xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-accent to-cyan-400">
            Invoice Verification Hub
          </h1>
          <p className="text-gray-400 mt-2">Validate extraction anomalies before ledger commit.</p>
        </div>
        <div className="flex items-center gap-4">
          <input 
            type="file" 
            accept=".pdf,image/*" 
            onChange={handleFileChange} 
            className="block w-full text-sm text-gray-400 file:mr-4 file:py-2.5 file:px-4 file:rounded-full file:border-0 file:text-sm file:font-medium file:bg-gray-800 file:text-accent hover:file:bg-gray-700 transition-colors"
          />
          <button onClick={handleProcess} disabled={!file || isLoading} className="glass-button px-6 py-2.5 rounded-full whitespace-nowrap">
            {isLoading ? "Processing..." : "Extract & Process"}
          </button>
        </div>
      </div>

      {headerData && (
        <div className="glass-panel p-6 rounded-2xl animate-fade-in shadow-2xl space-y-6">
          <h2 className="text-xl font-semibold text-white/90 border-b border-white/10 pb-2">Invoice Header Details</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <div><p className="text-xs text-gray-400 uppercase tracking-wider font-semibold">Vendor Name</p><p className="font-medium text-base mt-1 text-white">{headerData.vendor_name}</p></div>
            <div><p className="text-xs text-gray-400 uppercase tracking-wider font-semibold">Vendor GSTIN</p><p className="font-medium text-base mt-1 font-mono text-accent">{headerData.vendor_gstin}</p></div>
            <div><p className="text-xs text-gray-400 uppercase tracking-wider font-semibold">Invoice No</p><p className="font-medium text-base mt-1 text-white">{headerData.invoice_no}</p></div>
            <div><p className="text-xs text-gray-400 uppercase tracking-wider font-semibold">Date</p><p className="font-medium text-base mt-1 text-white">{headerData.invoice_date}</p></div>
            <div className="lg:col-span-2"><p className="text-xs text-gray-400 uppercase tracking-wider font-semibold">Address</p><p className="font-medium text-sm mt-1 text-white">{headerData.vendor_address}</p></div>
            <div><p className="text-xs text-gray-400 uppercase tracking-wider font-semibold">PAN</p><p className="font-medium text-sm mt-1 font-mono text-white/80">{headerData.pan_no || 'N/A'}</p></div>
            <div><p className="text-xs text-gray-400 uppercase tracking-wider font-semibold">IRN</p><p className="font-medium text-sm mt-1 font-mono text-white/80 overflow-hidden text-ellipsis">{headerData.irn_no || 'N/A'}</p></div>
          </div>
        </div>
      )}

      {lineItems.length > 0 && (
        <div className="space-y-6 animate-fade-in">
          <h2 className="text-2xl font-semibold text-white/90 px-2">Line Items Verification</h2>
          <div className="space-y-4">
            {lineItems.map((item, index) => {
              const isAnomaly = item.status === "FLAGGED_PRICE_ANOMALY";
              const topCandidate = item.candidates?.find(c => c.internal_sku === item.recommended_sku) || item.candidates?.[0];
              
              return (
                <div key={index} className={`glass-panel p-6 rounded-2xl shadow-xl transition-all border-l-4 ${isAnomaly ? 'border-warning' : 'border-white/10 hover:border-white/30'} flex flex-col lg:flex-row gap-8`}>
                  
                  {/* Left Column: Extracted Financial Math */}
                  <div className="flex-1 space-y-4 border-b lg:border-b-0 lg:border-r border-white/10 pb-6 lg:pb-0 lg:pr-8">
                    <div>
                      <p className="text-xs text-gray-400 uppercase tracking-wider font-semibold mb-1">Vendor Description</p>
                      <p className="text-lg font-medium text-white">{item.vendor_product_name}</p>
                      <p className="text-xs text-gray-500 font-mono mt-1">HSN: {item.vendor_hsn_code}</p>
                    </div>

                    {item.reasoning && (
                      <div className="text-xs bg-white/5 p-3 rounded-lg border border-white/10 text-gray-300">
                        🤖 <span className="italic">{item.reasoning}</span>
                      </div>
                    )}

                    <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 bg-black/20 p-4 rounded-xl border border-white/5">
                      <div><p className="text-xs text-gray-500">Qty</p><p className="font-medium text-white mt-0.5">{item.quantity}</p></div>
                      <div>
                        <p className="text-xs text-gray-500">Unit Price</p>
                        <p className="font-medium text-white mt-0.5">₹{item.unit_price.toFixed(2)}</p>
                        {isAnomaly && <span className="block text-xs text-warning mt-0.5">⚠️ {((item.price_deviation||0)*100).toFixed(1)}% Var</span>}
                      </div>
                      <div><p className="text-xs text-gray-500">Discount</p><p className="font-medium text-white mt-0.5">{item.discount > 0 ? `${(item.discount * 100).toFixed(1)}%` : '-'}</p></div>
                      <div><p className="text-xs text-gray-500">CGST</p><p className="font-medium text-white mt-0.5">₹{item.cgst_amount.toFixed(2)}</p></div>
                      <div><p className="text-xs text-gray-500">SGST</p><p className="font-medium text-white mt-0.5">₹{item.sgst_amount.toFixed(2)}</p></div>
                      <div className="bg-accent/10 p-2 -m-2 rounded-lg border border-accent/20">
                        <p className="text-xs text-accent">Net Total</p>
                        <p className="font-semibold text-accent mt-0.5 text-base">₹{item.net_total.toFixed(2)}</p>
                      </div>
                    </div>
                  </div>

                  {/* Right Column: Triage & Action */}
                  <div className="flex-1 flex flex-col justify-center">
                    <p className="text-xs text-gray-400 uppercase tracking-wider font-semibold mb-4">Verification Actions</p>
                    
                    {item.rowTriageState === 'APPROVED' && (
                      <div className="flex flex-col sm:flex-row items-center justify-between bg-success/10 border border-success/20 p-4 rounded-xl gap-4">
                        <div className="flex-1">
                          <div className="flex items-center gap-2 mb-1">
                            <span className="text-xs text-success font-bold uppercase tracking-wider px-2 py-0.5 bg-success/20 rounded-full">✓ Approved</span>
                            {item.new_product_name && <span className="text-xs text-blue-400 font-bold uppercase tracking-wider px-2 py-0.5 bg-blue-500/20 rounded-full">✨ New Product</span>}
                          </div>
                          {item.new_product_name ? (
                            <p className="font-medium text-white mt-2">{item.new_product_name}</p>
                          ) : (
                            <p className="font-medium text-white mt-2">Matched to Catalog</p>
                          )}
                          <p className="font-mono text-xs text-gray-400 mt-1">ID: {item.confirmed_sku}</p>
                        </div>
                        <button onClick={() => updateItem(index, { rowTriageState: 'MANUAL_ENTRY' })} className="glass-button px-4 py-2 rounded-lg text-xs whitespace-nowrap">
                          Edit Match
                        </button>
                      </div>
                    )}

                    {item.rowTriageState === 'TOP_MATCH' && topCandidate && (
                      <div className="space-y-4">
                        <div className="bg-white/5 border border-white/10 p-4 rounded-xl">
                          <div className="flex justify-between items-start mb-2">
                            <p className="text-xs text-accent font-semibold uppercase tracking-wider">Recommended Match</p>
                            <span className="text-xs bg-white/10 px-2 py-0.5 rounded text-gray-300 font-mono">Score: {(topCandidate.score * 100).toFixed(1)}%</span>
                          </div>
                          <p className="font-medium text-white text-base">{topCandidate.product_name}</p>
                          <div className="flex flex-wrap gap-x-6 gap-y-2 mt-3 font-mono text-xs text-gray-400 bg-black/20 p-2 rounded-lg">
                            <span>SKU: <span className="text-gray-300">{topCandidate.internal_sku}</span></span>
                            <span>HSN: <span className="text-gray-300">{topCandidate.hsn_code}</span></span>
                            <span>Avg Price: <span className="text-gray-300">₹{topCandidate.average_purchase_price?.toFixed(2) || '0.00'}</span></span>
                          </div>
                        </div>
                        <div className="flex gap-3">
                          <button onClick={() => updateItem(index, { confirmed_sku: topCandidate.internal_sku, rowTriageState: 'APPROVED' })} className="flex-1 bg-success/20 hover:bg-success/30 text-success text-sm py-2.5 rounded-xl font-medium transition-colors border border-success/30">
                            ✅ Approve Match
                          </button>
                          <button onClick={() => updateItem(index, { rowTriageState: 'ALTERNATIVES' })} className="flex-1 bg-danger/20 hover:bg-danger/30 text-danger text-sm py-2.5 rounded-xl font-medium transition-colors border border-danger/30">
                            ❌ Reject
                          </button>
                        </div>
                      </div>
                    )}

                    {item.rowTriageState === 'ALTERNATIVES' && (
                      <div className="space-y-4">
                        <div className="bg-white/5 border border-white/10 p-4 rounded-xl">
                          <p className="text-xs text-gray-400 mb-2 font-semibold">Select from Alternatives:</p>
                          <select 
                            value={item.confirmed_sku || ""}
                            onChange={(e) => updateItem(index, { confirmed_sku: e.target.value })}
                            className="glass-input p-3 rounded-xl text-sm w-full outline-none bg-black/40 text-white"
                          >
                            <option value="" disabled>Select Catalog Item...</option>
                            {item.candidates?.map((c) => (
                              <option key={c.internal_sku} value={c.internal_sku}>
                                {c.product_name} (Score: {(c.score*100).toFixed(0)}% | ₹{c.average_purchase_price?.toFixed(2)})
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="flex gap-3">
                          <button onClick={() => updateItem(index, { rowTriageState: 'APPROVED' })} disabled={!item.confirmed_sku} className="flex-1 bg-accent/20 hover:bg-accent/30 text-accent text-sm py-2.5 rounded-xl font-medium disabled:opacity-50 disabled:cursor-not-allowed border border-accent/30">
                            Approve Selected
                          </button>
                          <button onClick={() => updateItem(index, { rowTriageState: 'MANUAL_ENTRY' })} className="flex-1 glass-button py-2.5 rounded-xl text-sm font-medium">
                            None Match (Manual)
                          </button>
                        </div>
                      </div>
                    )}

                    {item.rowTriageState === 'MANUAL_ENTRY' && (
                      <div className="space-y-4 bg-white/5 border border-white/10 p-4 rounded-xl">
                        {/* Panel A: Catalog Search */}
                        <div>
                          <p className="text-xs text-gray-400 mb-2 font-semibold">Search Catalog (Filtered by HSN):</p>
                          <input 
                            type="text"
                            placeholder="Type product name or SKU..."
                            value={searchQueries[index] || ""}
                            onChange={(e) => setSearchQueries({...searchQueries, [index]: e.target.value})}
                            className="glass-input p-3 rounded-xl text-sm w-full outline-none bg-black/40 mb-2"
                          />
                          {(searchQueries[index] || "").length > 2 && (
                            <div className="max-h-48 overflow-y-auto bg-black/60 rounded-xl p-1 border border-white/10 space-y-1">
                              {fullCatalog
                                .filter(c => 
                                  c.hsn_code === item.vendor_hsn_code &&
                                  (c.internal_product_name.toLowerCase().includes(searchQueries[index].toLowerCase()) || 
                                   c.internal_sku.toLowerCase().includes(searchQueries[index].toLowerCase()))
                                )
                                .slice(0, 5)
                                .map(c => (
                                  <div key={c.internal_sku} onClick={() => updateItem(index, { confirmed_sku: c.internal_sku, new_product_name: undefined, rowTriageState: 'APPROVED' })} className="p-3 hover:bg-accent/20 cursor-pointer rounded-lg border border-transparent hover:border-accent/30 transition-colors">
                                    <p className="font-medium text-white text-sm">{c.internal_product_name}</p>
                                    <div className="flex gap-4 mt-1.5 text-xs text-gray-400 font-mono">
                                      <span className="bg-white/5 px-1.5 py-0.5 rounded">SKU: {c.internal_sku}</span>
                                      <span className="bg-white/5 px-1.5 py-0.5 rounded">₹{c.average_purchase_price?.toFixed(2) || '0.00'}</span>
                                    </div>
                                  </div>
                                ))}
                            </div>
                          )}
                        </div>

                        <div className="flex items-center gap-4 my-2">
                          <div className="h-px bg-white/10 flex-1"></div>
                          <span className="text-xs text-gray-500 font-medium">OR</span>
                          <div className="h-px bg-white/10 flex-1"></div>
                        </div>

                        {/* Panel B: Create New Product */}
                        <div>
                          <p className="text-xs text-blue-400 mb-2 font-semibold">✨ Create Brand New Product:</p>
                          <div className="flex gap-2">
                            <input 
                              type="text"
                              placeholder="Standard Product Name"
                              value={newProductNames[index] || ""}
                              onChange={(e) => setNewProductNames({...newProductNames, [index]: e.target.value})}
                              className="glass-input p-3 rounded-xl text-sm flex-1 outline-none bg-black/40 focus:ring-1 focus:ring-blue-500/50"
                            />
                            <button onClick={() => handleCreateNewProduct(index)} className="bg-blue-500 hover:bg-blue-600 text-white text-sm px-6 rounded-xl font-medium shadow-lg transition-colors whitespace-nowrap">
                              Create & Approve
                            </button>
                          </div>
                          <p className="text-xs text-gray-500 mt-2 italic">A unique UUID will be auto-generated and permanently assigned upon commit.</p>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
          
          {/* Footer Commit Banner */}
          <div className="glass-panel p-6 rounded-2xl flex flex-col sm:flex-row items-center justify-between gap-4 sticky bottom-6 shadow-2xl border-t border-accent/20 mt-8">
            <div>
              {commitStatus && (
                <div className={`px-4 py-2 rounded-lg text-sm font-medium ${commitStatus.includes("Success") ? 'bg-success/20 text-success border border-success/30' : 'bg-accent/20 text-accent border border-accent/30'}`}>
                  {commitStatus}
                </div>
              )}
            </div>
            <button onClick={handleCommit} className="bg-gradient-to-r from-accent to-cyan-500 hover:from-accent-hover hover:to-cyan-400 text-white px-10 py-4 rounded-xl shadow-lg font-bold text-lg transition-transform hover:scale-105 active:scale-95 whitespace-nowrap">
              Commit Verified Invoice
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import { PaymentBadge } from "../../components/StatusBadge.jsx";

// Whole-sheet line where the wholesale price is in effect (qty reached the min).
function isWholesale(line) {
  return (
    line.mode === "PIECE" &&
    Number(line.wholesale_price) > 0 &&
    Number(line.wholesale_min_qty) > 0 &&
    line.qty >= Number(line.wholesale_min_qty)
  );
}
// Per-unit price of a cart line (for simple lines).
function unitPrice(line) {
  if (line.kind === "material" || line.kind === "material-area") {
    if (line.mode === "PIECE" && isWholesale(line)) return Number(line.wholesale_price);
    return Number(line.price);
  }
  return Number(line.unit_price); // per-piece (exterior) or fixed service
}
// Quantity that price multiplies by (area-material bills by area).
function lineQty(line) {
  return line.kind === "material-area" ? Number(line.area) : line.qty;
}
function lineTotal(line) {
  // Резка = работа (погонный метр × ставка) + материал (площадь × цена/кв.м).
  // Погонный метр вводится вручную; если пусто — берём площадь куска.
  if (line.kind === "cutting") {
    const work = Number(line.rate) * Number(line.runM || line.area || 0);
    return work + Number(line.materialPrice) * Number(line.area || 0);
  }
  return unitPrice(line) * lineQty(line);
}

export default function Checkout() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [materials, setMaterials] = useState([]);
  const [services, setServices] = useState([]);
  const [cart, setCart] = useState([]);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [paymentMethod, setPaymentMethod] = useState("CASH");
  const [prepay, setPrepay] = useState("");
  const [client, setClient] = useState({ type: "PHYSICAL", full_name: "", company_name: "", phone: "" });
  const [clientId, setClientId] = useState(null);
  const [referredBy, setReferredBy] = useState("");
  const [clientsList, setClientsList] = useState([]);
  const [matches, setMatches] = useState([]);
  const [receipt, setReceipt] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [cut, setCut] = useState(null); // unified material / service config modal

  useEffect(() => {
    api.get("/warehouse/materials/", { params: { ordering: "name" } }).then((r) => setMaterials(r.data.results));
    api.get("/services/services/").then((r) => setServices(r.data.results));
    api.get("/clients/clients/").then((r) => setClientsList(r.data.results));
  }, []);

  useEffect(() => {
    // Живой поиск клиента по ИМЕНИ (ФИО или название компании), не по телефону.
    const name = (client.type === "OSOO" ? client.company_name : client.full_name) || "";
    if (name.trim().length < 2 || clientId) {
      setMatches([]);
      return;
    }
    const id = setTimeout(() => {
      api.get("/clients/clients/", { params: { search: name.trim() } }).then((r) => setMatches(r.data.results.slice(0, 5)));
    }, 250);
    return () => clearTimeout(id);
  }, [client.full_name, client.company_name, client.type, clientId]);

  const products = useMemo(() => {
    const svc = services
      .filter((s) => s.is_active !== false && !s.uses_running_meter)
      .map((s) => ({
      key: `S${s.id}`,
      kind: "service",
      serviceKind: s.kind,
      uses_area: s.uses_area,
      uses_material: s.uses_material,
      uses_pieces: s.uses_pieces,
      id: s.id,
      name: s.name,
      category: t(`serviceKind.${s.kind}`),
      base_price: Number(s.base_price),
      rate_flat: Number(s.rate_flat),
      rate_per_piece: Number(s.rate_per_piece),
    }));
    const mat = materials.map((m) => ({
      key: `M${m.id}`,
      kind: "material",
      id: m.id,
      name: m.name,
      category: m.category,
      price: Number(m.price_per_unit),
      sqm_price: Number(m.sqm_price ?? m.price_per_sqm ?? 0),
      piece_price: Number(m.piece_price ?? 0),
      piece_area: Number(m.piece_area ?? 0),
      cut_rate_per_pm: Number(m.cut_rate_per_pm ?? 0),
      is_roll_material: m.is_roll_material,
      unit: m.unit,
    }));
    return [...svc, ...mat];
  }, [materials, services, t]);

  const areaMaterials = materials.filter((m) => m.is_roll_material);
  const categories = [...new Set(materials.map((m) => m.category))];
  // The cutting service (work priced per running metre at the material's rate).
  const cuttingService = useMemo(
    () => services.find((s) => s.uses_running_meter && s.is_active !== false),
    [services]
  );

  const visibleProducts = products.filter((p) => {
    if (search && !p.name.toLowerCase().includes(search.toLowerCase())) return false;
    if (category) return p.kind === "material" && p.category === category;
    return true;
  });

  const total = useMemo(() => cart.reduce((s, l) => s + lineTotal(l), 0), [cart]);

  function tapProduct(p) {
    setError("");
    if (p.kind === "material") {
      // Sheet material → one unified modal (резка toggle + live price).
      if (p.is_roll_material) {
        const m = materials.find((x) => x.id === p.id) || p;
        setCut({
          material: m,
          saleMode: "AREA", // AREA (отрезать кусок = материал + резка) | PIECE (весь лист)
          cutting: true, // «кусок» всегда режется → работа реза по пог.м
          width: "",
          length: "",
          running_meters: "",
          qty: "1",
          matPrice: String(matSqm(m)),
          cutRate: String(m.cut_rate_per_pm ?? 0),
        });
        return;
      }
      return addOrInc({ ...p, price: p.price, mode: "SQM" });
    }
    if (p.uses_area) {
      // Interior install configurator (area × work rate + material).
      const m0 = areaMaterials[0];
      setCut({
        service: p,
        materialId: m0?.id ? String(m0.id) : "",
        width: "",
        length: "",
        running_meters: "",
        matPrice: m0 ? String(matSqm(m0)) : "",
        cutRate: m0 ? String(m0.cut_rate_per_pm ?? 0) : "",
      });
      return;
    }
    // Per-piece (exterior install) or fixed service → simple line with stepper.
    const unit_price = p.uses_pieces ? p.rate_per_piece : p.base_price;
    addOrInc({ ...p, unit_price });
  }

  function addOrInc(product) {
    setCart((prev) => {
      const found = prev.find((l) => l.key === product.key && l.kind !== "cutting");
      if (found) return prev.map((l) => (l.key === product.key ? { ...l, qty: l.qty + 1 } : l));
      const kind = product.kind === "material" ? "material" : "service";
      return [...prev, { ...product, kind, qty: 1 }];
    });
  }

  function matSqm(m) {
    return Number(m.sqm_price ?? m.price_per_sqm ?? m.price_per_unit ?? 0);
  }

  function addCutting() {
    const matPrice = Number(cut.matPrice || 0); // overridable material price/кв.м

    // --- Unified material modal (opened by tapping a sheet material) ---
    if (cut.material && !cut.service) {
      const m = cut.material;
      // Whole sheet/roll.
      if (cut.saleMode === "PIECE") {
        const q = Number(cut.qty) || 1;
        addOrInc({
          key: `M${m.id}-PIECE`, kind: "material", id: m.id, name: m.name,
          price: Number(m.piece_price || 0), mode: "PIECE",
          wholesale_price: Number(m.wholesale_price || 0),
          wholesale_min_qty: Number(m.wholesale_min_qty || 0),
        });
        // addOrInc adds qty 1; bump to requested count.
        if (q > 1) setCart((prev) => prev.map((l) => (l.key === `M${m.id}-PIECE` ? { ...l, qty: q } : l)));
        setCut(null);
        return;
      }
      const w = Number(cut.width);
      const l = Number(cut.length);
      if (!w || !l) return;
      const area = +(w * l).toFixed(3);
      // «Отрезать кусок» = материал (площадь) + работа реза (погонный метр).
      // Погонный метр можно ввести вручную; если пусто — берём площадь (Ш×Д).
      const runM = Number(cut.running_meters) || area;
      if (!cuttingService) {
        // В каталоге нет услуги резки → продаём только кусок материала.
        setCart((prev) => [...prev, {
          key: `MA${m.id}-${cart.length}`, kind: "material-area",
          id: m.id, name: m.name, price: matPrice, width: w, length: l, area, qty: 1,
        }]);
        setCut(null);
        return;
      }
      setCart((prev) => [...prev, {
        key: `C${m.id}-${cart.length}`, kind: "cutting",
        serviceId: cuttingService.id, name: cuttingService.name || "Резка",
        materialId: m.id, materialName: m.name, materialPrice: matPrice,
        rate: Number(cut.cutRate || 0),
        width: w, length: l, area, runM, qty: 1,
      }]);
      setCut(null);
      return;
    }

    // --- Service-tile path (interior install): area × rate + material ---
    const w = Number(cut.width);
    const l = Number(cut.length);
    if (!w || !l) return;
    const area = +(w * l).toFixed(3);
    const s = cut.service;
    const mat = materials.find((m) => m.id === Number(cut.materialId));
    if (!mat) return;
    setCart((prev) => [...prev, {
      key: `C${s.id}-${mat.id}-${cart.length}`, kind: "cutting",
      serviceId: s.id, name: s.name,
      materialId: mat.id, materialName: mat.name, materialPrice: matPrice,
      rate: Number(s.rate_flat),
      width: w, length: l, area, runM: area, qty: 1,
    }]);
    setCut(null);
  }

  function changeQty(key, delta) {
    setCart((prev) => prev.map((l) => (l.key === key ? { ...l, qty: l.qty + delta } : l)).filter((l) => l.qty > 0));
  }
  function removeLine(key) {
    setCart((prev) => prev.filter((l) => l.key !== key));
  }

  function pickClient(c) {
    setClientId(c.id);
    setClient({ type: c.type, full_name: c.full_name || "", company_name: c.company_name || "", phone: c.phone });
    setMatches([]);
  }

  async function submit() {
    setError("");
    if (!cart.length) return setError(t("checkout.emptyCart"));
    if (!clientId && client.phone) {
      if (client.type === "PHYSICAL" && !client.full_name.trim()) return setError(t("checkout.needName"));
      if (client.type === "OSOO" && !client.company_name.trim()) return setError(t("checkout.needCompany"));
    }
    setBusy(true);
    const items = cart.map((l) => {
      if (l.kind === "material")
        return { type: "MATERIAL", material: l.id, quantity: l.qty, mode: l.mode || "SQM" };
      if (l.kind === "material-area")
        return {
          type: "MATERIAL", material: l.id, quantity: l.area, mode: "SQM",
          ...(isAdmin ? { material_price: l.price } : {}),
        };
      if (l.kind === "cutting")
        return {
          type: "SERVICE", service: l.serviceId, material: l.materialId,
          width: l.width, length: l.length, running_meters: l.runM,
          ...(isAdmin ? { material_price: l.materialPrice, cut_rate: l.rate } : {}),
        };
      return { type: "SERVICE", service: l.id, quantity: l.qty };
    });
    const payload = { payment_method: paymentMethod, items };
    if (paymentMethod === "CASH" && prepay !== "" && Number(prepay) >= 0)
      payload.amount_paid = Number(prepay);
    if (clientId) payload.client_id = clientId;
    else if (client.phone)
      payload.client = { ...client, ...(referredBy ? { referred_by: Number(referredBy) } : {}) };
    try {
      const { data } = await api.post("/sales/receipts/checkout/", payload);
      setReceipt(data);
      setCart([]);
      setClient({ type: "PHYSICAL", full_name: "", company_name: "", phone: "" });
      setClientId(null);
      setReferredBy("");
      setPrepay("");
      api.get("/clients/clients/").then((r) => setClientsList(r.data.results));
    } catch (e) {
      setError(e.response?.data?.detail || t("common.error"));
    } finally {
      setBusy(false);
    }
  }

  const isMatModal = !!(cut && cut.material && !cut.service); // unified material modal
  const cutPiece = isMatModal && cut.saleMode === "PIECE";
  const cutArea = cut && Number(cut.width) && Number(cut.length) ? +(Number(cut.width) * Number(cut.length)).toFixed(3) : 0;
  const cutMat = cut ? (cut.material || materials.find((m) => m.id === Number(cut.materialId))) : null;
  // Editable (overridable) prices — default to the material's catalogue values.
  const cutMatSqm = cut ? Number(cut.matPrice || 0) : 0;
  // Work is on for: interior-install service, or the material modal «Резка» switch.
  const cutWorkOn = cut?.service ? true : !!cut?.cutting;
  // Work rate per кв.м (cutting → material rate; interior → service rate).
  const cutWorkRate = cut?.service
    ? (cut.service.uses_running_meter ? Number(cut.cutRate || 0) : Number(cut.service.rate_flat))
    : (cut?.cutting ? Number(cut.cutRate || 0) : 0);
  // Работа резки — по погонному метру: ручной ввод, иначе из площади (Ш×Д).
  const cutRunM = cut?.cutting && Number(cut?.running_meters) > 0 ? Number(cut.running_meters) : cutArea;
  const cutWork = cutWorkOn ? cutWorkRate * cutRunM : 0;
  const cutMaterialSum = cutMatSqm * cutArea;
  const cutPieceQty = Number(cut?.qty) || 1;
  const cutWholeMin = cutPiece ? Number(cut.material.wholesale_min_qty || 0) : 0;
  const cutWholePrice = cutPiece ? Number(cut.material.wholesale_price || 0) : 0;
  const cutPieceWholesale = cutPiece && cutWholePrice > 0 && cutWholeMin > 0 && cutPieceQty >= cutWholeMin;
  const cutPieceUnit = cutPieceWholesale ? cutWholePrice : Number(cut?.material?.piece_price || 0);
  const cutPieceTotal = cutPiece ? cutPieceQty * cutPieceUnit : 0;
  const cutTotal = cutPiece ? cutPieceTotal : cutWork + cutMaterialSum;

  return (
    <>
      <h1>{t("checkout.title")}</h1>

      <div className="pos">
        <div className="pos-main">
          <div className="toolbar">
            <input className="search" placeholder={t("common.search")} value={search} onChange={(e) => setSearch(e.target.value)} />
            <select value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {categories.map((c) => (<option key={c} value={c}>{c}</option>))}
            </select>
          </div>

          <div className="pos-grid">
            {visibleProducts.map((p) => (
              <button key={p.key} className="pos-product" onClick={() => tapProduct(p)}>
                {p.kind === "service" && <span className="p-tag">{t(`serviceKind.${p.serviceKind}`)}</span>}
                <div>
                  <div className="p-name">{p.name}</div>
                  <div className="p-cat">{p.category}</div>
                </div>
                <div className="p-price">
                  {p.kind === "material"
                    ? p.piece_price > 0
                      ? `${p.piece_price} сом/${t("checkout.pieceUnit")}`
                      : `${p.price} сом`
                    : p.uses_area
                    ? `${p.rate_flat} сом/кв.м`
                    : p.uses_pieces
                    ? `${p.rate_per_piece} сом/букву`
                    : `${p.base_price} сом`}
                </div>
              </button>
            ))}
            {!visibleProducts.length && <p className="muted">{t("common.empty")}</p>}
          </div>
        </div>

        <div className="pos-cart card">
          <h3>{t("checkout.receipt")}</h3>
          {cart.length ? (
            cart.map((l) => (
              <div className="cart-line" key={l.key}>
                <div className="cl-info">
                  <div className="cl-name">{l.name}</div>
                  {l.kind === "cutting" ? (
                    <div className="cl-sub">
                      {l.width}×{l.length} = {l.area} кв.м · {l.materialName} · {t("checkout.rateWork")} {l.rate}
                    </div>
                  ) : l.kind === "material-area" ? (
                    <div className="cl-sub">{l.width}×{l.length} = {l.area} кв.м · {l.price} сом/кв.м</div>
                  ) : l.mode === "PIECE" ? (
                    <div className="cl-sub">
                      {t("checkout.sellWhole")} · {unitPrice(l)} сом / {t("checkout.pieceUnit")}
                      {isWholesale(l) && (
                        <span className="badge ok" style={{ marginLeft: 6 }}>{t("checkout.wholesale")}</span>
                      )}
                    </div>
                  ) : (
                    <div className="cl-sub">{unitPrice(l)} сом / ед.</div>
                  )}
                </div>
                {l.kind !== "cutting" && l.kind !== "material-area" && (
                  <div className="stepper">
                    <button onClick={() => changeQty(l.key, -1)}>−</button>
                    <span className="qty">{l.qty}</span>
                    <button onClick={() => changeQty(l.key, 1)}>+</button>
                  </div>
                )}
                <div className="cl-total">{lineTotal(l).toFixed(0)}</div>
                <button className="ghost" onClick={() => removeLine(l.key)} title={t("common.delete")} aria-label={t("common.delete")}><Icon name="x" size={16} /></button>
              </div>
            ))
          ) : (
            <div className="pos-empty">{t("checkout.tapToAdd")}</div>
          )}

          <div className="pos-total"><span>{t("common.total")}</span><span>{total.toFixed(0)} сом</span></div>

          <div className="row">
            <div className="field" style={{ width: 120, margin: 0 }}>
              <label>{t("clients.type")}</label>
              <select value={client.type} onChange={(e) => { setClient({ ...client, type: e.target.value }); setClientId(null); }}>
                <option value="PHYSICAL">{t("clients.physical")}</option>
                <option value="OSOO">{t("clients.osoo")}</option>
              </select>
            </div>
            <div className="field grow" style={{ margin: 0, position: "relative" }}>
              <label>{client.type === "OSOO" ? t("clients.companyName") : t("clients.fullName")}</label>
              {client.type === "OSOO" ? (
                <input
                  value={client.company_name}
                  onChange={(e) => { setClient({ ...client, company_name: e.target.value }); setClientId(null); }}
                  placeholder={t("checkout.clientNamePlaceholder")}
                />
              ) : (
                <input
                  value={client.full_name}
                  onChange={(e) => { setClient({ ...client, full_name: e.target.value }); setClientId(null); }}
                  placeholder={t("checkout.clientNamePlaceholder")}
                />
              )}
              {matches.length > 0 && (
                <div className="card" style={{ position: "absolute", zIndex: 5, width: "100%", padding: 6 }}>
                  {matches.map((m) => (
                    <div key={m.id} className="crow" style={{ cursor: "pointer" }} onClick={() => pickClient(m)}>
                      <span>{m.display_name}</span><span className="muted">{m.phone}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="field">
            <label>{t("clients.phone")}</label>
            <input value={client.phone} onChange={(e) => setClient({ ...client, phone: e.target.value })} placeholder="+996…" />
          </div>

          {/* Referral — only when registering a NEW client */}
          {!clientId && client.phone && (
            <div className="field">
              <label>{t("clients.referredByLabel")}</label>
              <select value={referredBy} onChange={(e) => setReferredBy(e.target.value)}>
                <option value="">— {t("clients.noReferrer")} —</option>
                {clientsList.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.display_name} ({c.phone})
                  </option>
                ))}
              </select>
            </div>
          )}

          <label style={{ marginTop: 10 }}>{t("checkout.paymentMethod")}</label>
          <div className="row" style={{ gap: 8 }}>
            {["CASH", "ONLINE"].map((m) => (
              <button key={m} className={`grow ${paymentMethod === m ? "" : "secondary"}`} onClick={() => setPaymentMethod(m)}>
                {t(`checkout.${m.toLowerCase()}`)}
              </button>
            ))}
          </div>

          {paymentMethod === "CASH" && (
            <div className="field" style={{ marginTop: 10 }}>
              <label>{t("checkout.prepay")}</label>
              <input
                type="number"
                min="0"
                value={prepay}
                onChange={(e) => setPrepay(e.target.value)}
                placeholder={`${total.toFixed(0)} сом`}
              />
              <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
                {t("checkout.prepayHint")}
              </p>
              {prepay !== "" && Number(prepay) < total && (
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {t("receipts.debt")}:{" "}
                  <strong style={{ color: "var(--danger)" }}>
                    {(total - Number(prepay)).toFixed(0)} сом
                  </strong>
                </div>
              )}
            </div>
          )}

          {error && <div className="error">{error}</div>}
          <button style={{ marginTop: 14, width: "100%", height: 52 }} onClick={submit} disabled={busy || !cart.length}>
            {busy ? t("common.loading") : `${t("checkout.submit")} · ${total.toFixed(0)} сом`}
          </button>
        </div>
      </div>

      {/* Unified configurator: material (резка-toggle) or interior-install service */}
      {cut && (
        <Modal
          title={cut.service ? cut.service.name : cut.material.name}
          onClose={() => setCut(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setCut(null)}>{t("common.cancel")}</button>
              <button
                onClick={addCutting}
                disabled={
                  cutPiece
                    ? !(Number(cut.qty) > 0) || !(Number(cut.material.piece_price) > 0)
                    : !cutArea || (cut.service && !cut.materialId)
                }
              >
                {t("common.add")}
              </button>
            </>
          }
        >
          {/* Material modal: sale-mode (по площади / целиком) + резка toggle */}
          {isMatModal && (
            <>
              <div className="tabs" style={{ marginTop: 0 }}>
                <button className={cut.saleMode === "AREA" ? "active" : ""} onClick={() => setCut({ ...cut, saleMode: "AREA" })}>
                  {t("checkout.sellByArea")}
                </button>
                {Number(cut.material.piece_price) > 0 && (
                  <button className={cut.saleMode === "PIECE" ? "active" : ""} onClick={() => setCut({ ...cut, saleMode: "PIECE" })}>
                    {t("checkout.sellWhole")}
                  </button>
                )}
              </div>
              {cut.saleMode === "AREA" && (
                <p className="muted" style={{ fontSize: 13, margin: "10px 0 2px" }}>
                  {t("checkout.cutInfo")}
                </p>
              )}
            </>
          )}

          {/* Interior-install service: material picker */}
          {cut.service && (
            <div className="field">
              <label>{t("checkout.cutMaterial")}</label>
              <select
                value={cut.materialId}
                onChange={(e) => {
                  const m = materials.find((x) => x.id === Number(e.target.value));
                  setCut({ ...cut, materialId: e.target.value, matPrice: m ? String(matSqm(m)) : "", cutRate: m ? String(m.cut_rate_per_pm ?? 0) : "" });
                }}
              >
                <option value="">—</option>
                {areaMaterials.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name} ({matSqm(m)} сом/кв.м, ост. {m.quantity} кв.м
                    {m.sheets_remaining != null ? ` ≈${Math.round(Number(m.sheets_remaining))} ${t("warehouse.sheetsShort")}` : ""})
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Whole-sheet sale: quantity only */}
          {cutPiece ? (
            <div className="field">
              <label>{t("common.quantity")} ({t("checkout.pieceUnit")})</label>
              <input type="number" value={cut.qty} onChange={(e) => setCut({ ...cut, qty: e.target.value })} />
            </div>
          ) : (
            <>
              <div className="row">
                <div className="field grow"><label>{t("supply.width")}</label><input type="number" step="any" value={cut.width} onChange={(e) => setCut({ ...cut, width: e.target.value })} /></div>
                <div className="field grow"><label>{t("supply.length")}</label><input type="number" step="any" value={cut.length} onChange={(e) => setCut({ ...cut, length: e.target.value })} /></div>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>{t("checkout.sizeHint")}</p>
              {cut.cutting && (
                <div className="field">
                  <label>{t("checkout.runningMeters")}</label>
                  <input
                    type="number"
                    step="any"
                    value={cut.running_meters}
                    onChange={(e) => setCut({ ...cut, running_meters: e.target.value })}
                    placeholder={cutArea ? String(cutArea) : ""}
                  />
                  <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("checkout.runMetersHint")}</p>
                </div>
              )}
              {/* Admin-only: override catalogue prices at sale time */}
              {isAdmin && (
                <div className="row">
                  <div className="field grow" style={{ margin: 0 }}>
                    <label>{t("checkout.matPriceLabel")}</label>
                    <input type="number" step="any" value={cut.matPrice ?? ""} onChange={(e) => setCut({ ...cut, matPrice: e.target.value })} />
                  </div>
                  {cutWorkOn && (
                    <div className="field grow" style={{ margin: 0 }}>
                      <label>{t("checkout.cutRateLabel")}</label>
                      <input type="number" step="any" value={cut.cutRate ?? ""} onChange={(e) => setCut({ ...cut, cutRate: e.target.value })} />
                    </div>
                  )}
                </div>
              )}
            </>
          )}

          {/* Live total */}
          {((cutPiece && Number(cut.qty) > 0) || (!cutPiece && cutArea > 0 && cutMat)) && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
              {cutPiece ? (
                <>
                  <div className="crow">
                    <span className="k">{cutPieceUnit} сом × {cut.qty}</span>
                    {cutPieceWholesale && <span className="badge ok">{t("checkout.wholesale")}</span>}
                  </div>
                  {!cutPieceWholesale && cutWholePrice > 0 && cutWholeMin > 0 && (
                    <div className="crow">
                      <span className="muted" style={{ fontSize: 12 }}>
                        {t("checkout.wholesaleFrom", { n: cutWholeMin, price: cutWholePrice })}
                      </span>
                      <span />
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="crow"><span className="k">{t("supply.area")}</span><strong>{cutArea} кв.м</strong></div>
                  {cutWorkOn && cutWorkRate > 0 && (
                    <div className="crow"><span className="k">{t("checkout.rateWork")}</span><span>{cutWorkRate} × {cutRunM} = {cutWork.toFixed(0)}</span></div>
                  )}
                  <div className="crow"><span className="k">{t("checkout.rateMaterial")}</span><span>{cutMatSqm} × {cutArea} = {cutMaterialSum.toFixed(0)}</span></div>
                </>
              )}
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6 }}>
                <strong>{t("common.total")}</strong>
                <strong style={{ fontSize: 18 }}>{cutTotal.toFixed(0)} сом</strong>
              </div>
            </div>
          )}
        </Modal>
      )}

      {receipt && (
        <Modal title={`${t("checkout.receipt")} № ${String(receipt.id).slice(0, 8)}`} onClose={() => setReceipt(null)}>
          {receipt.items.map((it) => (
            <div className="crow" key={it.id}>
              <span>{(it.type === "SERVICE" ? it.service_name : it.material_name)} × {it.quantity}</span>
              <span>{it.line_total} сом</span>
            </div>
          ))}
          <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 8 }}>
            <strong>{t("common.total")}</strong><strong>{receipt.total_price} сом</strong>
          </div>
          <div className="crow"><span className="k">{t("receipts.status")}</span><PaymentBadge status={receipt.payment_status} /></div>
          {receipt.payment_status === "PENDING" && receipt.payment_url && (
            <>
              {receipt.payment_qr && (
                <div style={{ textAlign: "center", marginTop: 14 }}>
                  <img src={receipt.payment_qr} alt="QR" style={{ width: 200, height: 200, borderRadius: "var(--r-lg)" }} />
                  <div className="muted" style={{ fontSize: 13 }}>{t("checkout.scanQr")}</div>
                </div>
              )}
              <a className="btn" href={receipt.payment_url} target="_blank" rel="noreferrer" style={{ width: "100%", marginTop: 10, textAlign: "center" }}>{t("checkout.payOnline")}</a>
            </>
          )}
        </Modal>
      )}
    </>
  );
}

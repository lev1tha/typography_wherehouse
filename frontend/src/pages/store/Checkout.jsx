import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import { PaymentBadge } from "../../components/StatusBadge.jsx";
import { areaOf } from "../../utils/area.js";

// Цену округляем ВВЕРХ до целого сома (решение заказчика), как на бэкенде
// (TransactionItem.line_total). Эпсилон гасит float-шум, чтобы целое не «прыгало».
const ceilSom = (v) => Math.max(0, Math.ceil((Number(v) || 0) - 1e-6));

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
  // Резка = 2 строки в чеке (работа + материал), каждая округляется вверх
  // отдельно — как на бэкенде.
  if (line.kind === "cutting") {
    const work = Number(line.rate) * Number(line.runM || 0);
    const material = Number(line.materialPrice) * Number(line.area || 0);
    return ceilSom(work) + ceilSom(material);
  }
  // Работа реза на целом листе (без материала по площади): пог.м × ставка.
  if (line.kind === "cut-work") return ceilSom(Number(line.rate) * Number(line.runM || 0));
  return ceilSom(unitPrice(line) * lineQty(line));
}

// Сегодня в формате YYYY-MM-DD по МЕСТНОЙ дате: toISOString() отдаёт UTC и в
// Бишкеке вечером показывал бы завтрашний день.
const todayStr = () => new Date().toLocaleDateString("sv-SE");

// Четыре способа продать материал по кв.м — так их называет заказчик.
// Раньше вкладок было две («отрезать кусок» и «весь лист»), и в первую из них
// были свалены три разных случая: фигурный рез, обычный рез и продажа площади
// без реза. Отличались они только тем, что мастер вписывал (или не вписывал) в
// поле погонных метров, — то есть не отличались ничем, что видно в кассе.
//   CURVE — фигурный рез: длину кривой вводит мастер;
//   SIDE  — обычный рез: в работу идёт ОДНА сторона, «Длина»;
//   SQM   — только материал по площади, работы реза нет;
//   PIECE — лист/рулон целиком по цене за штуку.
const MODES = ["SIDE", "CURVE", "SQM", "PIECE"];
// Режимы, где материал считается по площади куска (нужны ширина и длина).
const AREA_MODES = ["SIDE", "CURVE", "SQM"];
// Режимы с работой реза.
const CUT_MODES = ["SIDE", "CURVE"];

// Длина реза в погонных метрах для режима. У обычного реза она НЕ вводится:
// это одна сторона куска — та, что вписана в «Длину».
function runMetersFor(cut) {
  if (cut.mode === "SIDE") return Number(cut.length) || 0;
  if (cut.mode === "CURVE") return Number(cut.running_meters) || 0;
  return 0;
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
  // «Вся сумма» — не число, а намерение: сервер сам зачтёт ровно итог чека
  // (`pay_full`). Касса не должна угадывать сумму, которую посчитает сервер:
  // расхождение округлений в сом превращалось в фантомный долг.
  const [payFull, setPayFull] = useState(false);
  const [orderTitle, setOrderTitle] = useState(""); // наименование заказа
  const [titleHints, setTitleHints] = useState([]); // ранее использованные
  // Дата заказа: по умолчанию сегодня, админ может поставить прошедшую.
  const [orderDate, setOrderDate] = useState(todayStr);
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
    // page_size обязателен: без него приходит первая страница из 25 материалов
    // (PAGE_SIZE в настройках). В кассе не было видно остальной номенклатуры, а
    // выпадашка «категория» собиралась по этим же 25 — и половины категорий в
    // ней просто не существовало.
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results));
    api.get("/services/services/").then((r) => setServices(r.data.results));
    api.get("/clients/clients/").then((r) => setClientsList(r.data.results));
    // Подсказки по названию заказа — как живой поиск клиента, чтобы повторные
    // работы назывались одинаково, а не «вывеска», «Вывеска», «вывеска2».
    api.get("/sales/receipts/titles/").then((r) => setTitleHints(r.data)).catch(() => {});
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
      // Поля `category` у материала больше нет — номенклатура разобрана на
      // поля, и его заменил ТИП из справочника (Акрил, Форекс, Оргстекло).
      // Фильтр в кассе всё это время подставлял `undefined`: выпадашка была с
      // одним пустым пунктом, а React ругался на key={undefined}.
      category: m.type_name || "",
      price: Number(m.price_per_unit),
      sqm_price: Number(m.sqm_price ?? m.price_per_sqm ?? 0),
      piece_price: Number(m.piece_price ?? 0),
      piece_area: Number(m.piece_area ?? 0),
      cut_rate_per_pm: Number(m.cut_rate_per_pm ?? 0),
      is_roll_material: m.is_roll_material,
      intake_form: m.intake_form || "SHEET",
      unit: m.unit,
      quantity: Number(m.quantity ?? 0),
      is_below_critical: !!m.is_below_critical,
      sheets_remaining: m.sheets_remaining != null ? Number(m.sheets_remaining) : null,
    }));
    return [...svc, ...mat];
  }, [materials, services, t]);

  const areaMaterials = materials.filter((m) => m.is_roll_material);
  const categories = [...new Set(materials.map((m) => m.type_name).filter(Boolean))].sort();
  // Услуги резки — по одной на станок (ЧПУ, лазер). Работа считается по
  // погонному метру; ставку берём у станка, а если у него своей нет — у
  // материала (так было до разделения на станки).
  const MACHINE_ORDER = { CNC: 0, LASER: 1 };
  const cuttingServices = useMemo(
    () =>
      services
        .filter((s) => s.uses_running_meter && s.is_active !== false)
        .sort((a, b) => (MACHINE_ORDER[a.machine] ?? 9) - (MACHINE_ORDER[b.machine] ?? 9)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [services]
  );
  const cuttingService = cuttingServices[0];

  const rateFor = (svc, mat) =>
    String(Number(svc?.rate_per_pm) || Number(mat?.cut_rate_per_pm) || 0);
  const svcById = (id) => cuttingServices.find((s) => s.id === Number(id)) || cuttingService;

  const visibleProducts = products.filter((p) => {
    if (search && !p.name.toLowerCase().includes(search.toLowerCase())) return false;
    if (category) return p.kind === "material" && p.category === category;
    return true;
  });

  const total = useMemo(() => cart.reduce((s, l) => s + lineTotal(l), 0), [cart]);

  // Remaining stock shown on EVERY material card during a sale: quantity in its
  // unit, plus ≈ whole sheets when the material has a sheet area. Null for services.
  function stockLabel(p) {
    if (p.kind !== "material") return null;
    const qty = Number(p.quantity ?? 0);
    const unitLabel = t(`unit.${p.unit}`) || "";
    let text = `${+qty.toFixed(2)} ${unitLabel}`.trim();
    if (p.sheets_remaining != null)
      text += ` · ≈${Math.round(p.sheets_remaining)} ${t("warehouse.sheetsShort")}`;
    return text;
  }

  // Stock problem state for styling / badge / blocking: out of stock (qty ≤ 0)
  // vs low (≤ critical balance). Null when healthy or for services.
  function stockState(p) {
    if (p.kind !== "material") return null;
    const qty = Number(p.quantity ?? 0);
    const out = qty <= 0;
    const low = !out && p.is_below_critical;
    return out || low ? { out, low } : null;
  }

  function tapProduct(p) {
    setError("");
    if (stockState(p)?.out) return; // нет на складе — продажа заблокирована
    if (p.kind === "material") {
      // Материал по кв.м → окно с четырьмя режимами продажи.
      if (p.is_roll_material) {
        const m = materials.find((x) => x.id === p.id) || p;
        setCut({
          material: m,
          // Обычный рез — самый частый случай, с него и открываемся.
          mode: "SIDE",
          width: "",
          length: "",
          running_meters: "",
          qty: "1",
          matPrice: String(matSqm(m)),
          piecePrice: String(Number(m.piece_price || 0)),
          priceEdited: false,
          cutServiceId: cuttingService?.id ?? "",
          cutRate: rateFor(cuttingService, m),
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
  // Лист или рулон — как задано на самом материале. «Цена за лист» у плёнки,
  // которая приходит рулоном, отвечает не на тот вопрос.
  const wholeUnitOf = (m) =>
    (m?.intake_form || "SHEET") === "ROLL" ? t("warehouse.unitRoll") : t("warehouse.unitSheet");

  function addCutting() {
    const matPrice = Number(cut.matPrice || 0); // overridable material price/кв.м

    // --- Окно материала: четыре режима продажи ---
    if (cut.material && !cut.service) {
      const m = cut.material;
      // Штучно: лист/рулон целиком.
      if (cut.mode === "PIECE") {
        const q = Number(cut.qty) || 1;
        // Цену за штуку админ может перебить прямо в кассе. Пока он её не
        // трогал, шлём её на сервер НЕ ЯВНО — иначе оптовая цена, которую
        // сервер включает сам от нужного количества, была бы затёрта розничной.
        const edited = !!cut.priceEdited;
        addOrInc({
          key: `M${m.id}-PIECE`, kind: "material", id: m.id, name: m.name,
          price: edited ? Number(cut.piecePrice || 0) : Number(m.piece_price || 0),
          mode: "PIECE",
          priceEdited: edited,
          unitWord: wholeUnitOf(m),
          wholesale_price: edited ? 0 : Number(m.wholesale_price || 0),
          wholesale_min_qty: edited ? 0 : Number(m.wholesale_min_qty || 0),
        });
        // addOrInc adds qty 1; bump to requested count.
        if (q > 1) setCart((prev) => prev.map((l) => (l.key === `M${m.id}-PIECE` ? { ...l, qty: q } : l)));
        // Целый лист тоже можно резать: отдельная строка работы (пог.м × ставка).
        const runM = Number(cut.running_meters) || 0;
        const pieceSvc = svcById(cut.cutServiceId);
        if (cut.pieceCut && pieceSvc && runM > 0) {
          setCart((prev) => [...prev, {
            key: `CW${m.id}-${prev.length}-${runM}`, kind: "cut-work",
            serviceId: pieceSvc.id, name: pieceSvc.name || "Резка",
            materialId: m.id, materialName: m.name,
            rate: Number(cut.cutRate || 0), runM, qty: 1,
          }]);
        }
        setCut(null);
        return;
      }
      const w = Number(cut.width);
      const l = Number(cut.length);
      if (!w || !l) return;
      // Площадь — как её посчитает и сохранит сервер (до 0.001, половина
      // вверх): иначе касса и чек расходились на сом.
      const area = areaOf(cut.width, cut.length);
      const runM = runMetersFor(cut);
      // «Квадратный метр» — материал по площади и всё: работы реза в этом
      // режиме нет. Так же ведём себя, если услуги резки нет в каталоге.
      if (cut.mode === "SQM" || !cuttingService) {
        setCart((prev) => [...prev, {
          key: `MA${m.id}-${prev.length}`, kind: "material-area",
          id: m.id, name: m.name, price: matPrice, width: w, length: l, area, qty: 1,
        }]);
        setCut(null);
        return;
      }
      const areaSvc = svcById(cut.cutServiceId);
      setCart((prev) => [...prev, {
        key: `C${m.id}-${prev.length}`, kind: "cutting",
        serviceId: areaSvc.id, name: areaSvc.name || "Резка",
        materialId: m.id, materialName: m.name, materialPrice: matPrice,
        rate: Number(cut.cutRate || 0),
        cutMode: cut.mode,
        width: w, length: l, area, runM, qty: 1,
      }]);
      setCut(null);
      return;
    }

    // --- Service-tile path (interior install): area × rate + material ---
    const w = Number(cut.width);
    const l = Number(cut.length);
    if (!w || !l) return;
    const area = areaOf(cut.width, cut.length);
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
        return {
          type: "MATERIAL", material: l.id, quantity: l.qty, mode: l.mode || "SQM",
          // Цену шлём, только если админ правил её руками. Иначе сервер сам
          // решит, розничная тут цена или оптовая (опт включается от количества).
          ...(isAdmin && l.priceEdited ? { material_price: l.price } : {}),
        };
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
      if (l.kind === "cut-work")
        // Материал передаём (для ставки реза), но без размеров → площадь 0 →
        // бэкенд создаёт только строку работы, без материала по площади.
        return {
          type: "SERVICE", service: l.serviceId, material: l.materialId,
          running_meters: l.runM,
          ...(isAdmin ? { cut_rate: l.rate } : {}),
        };
      return { type: "SERVICE", service: l.id, quantity: l.qty };
    });
    const payload = { payment_method: paymentMethod, items };
    if (orderTitle.trim()) payload.title = orderTitle.trim();
    // Дату шлём, только когда она не сегодняшняя: у складовщика этого поля нет,
    // и лишний параметр упёрся бы в проверку прав на пустом месте.
    if (isAdmin && orderDate && orderDate !== todayStr()) payload.order_date = orderDate;
    // Пустое поле = ничего не приняли, весь заказ уходит в долг. Раньше пустое
    // молча означало «оплачено полностью», и долг терялся.
    if (paymentMethod !== "ONLINE") {
      if (payFull) payload.pay_full = true;
      else payload.amount_paid = Math.max(0, Number(prepay) || 0);
    }
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
      setPayFull(false);
      setOrderTitle("");
      // Дату НЕ сбрасываем: заказы задним числом заносят пачкой за один день,
      // и возврат на сегодня после каждой продажи заставлял бы вводить её снова.
      api.get("/clients/clients/").then((r) => setClientsList(r.data.results));
    } catch (e) {
      setError(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  const isMatModal = !!(cut && cut.material && !cut.service); // окно материала
  const cutPiece = isMatModal && cut.mode === "PIECE";
  const cutArea = cut && Number(cut.width) && Number(cut.length) ? areaOf(cut.width, cut.length) : 0;
  const cutMat = cut ? (cut.material || materials.find((m) => m.id === Number(cut.materialId))) : null;
  // Editable (overridable) prices — default to the material's catalogue values.
  const cutMatSqm = cut ? Number(cut.matPrice || 0) : 0;
  // Работа реза считается у услуги «внутренний монтаж» и в режимах реза.
  const cutWorkOn = cut?.service ? true : isMatModal && CUT_MODES.includes(cut.mode);
  // Ставка: у монтажа — своя за кв.м, у реза — ставка станка/материала.
  const cutWorkRate = cut?.service
    ? (cut.service.uses_running_meter ? Number(cut.cutRate || 0) : Number(cut.service.rate_flat))
    : cutWorkOn
    ? Number(cut?.cutRate || 0)
    : 0;
  // Длина реза: у кривой — то, что ввёл мастер; у обычного реза — ОДНА сторона
  // куска, та, что вписана в «Длину». Площадь сюда не подставляется: кв.м и
  // пог.м разные величины, и работа от такой подстановки выходила втрое дешевле.
  const cutRunM = cut?.service ? cutArea : isMatModal ? runMetersFor(cut) : 0;
  const cutWork = cutWorkOn ? cutWorkRate * cutRunM : 0;
  const cutMaterialSum = cutMatSqm * cutArea;
  const cutPieceQty = Number(cut?.qty) || 1;
  // Цену за штуку админ может перебить прямо в кассе. Пока не трогал — работает
  // оптовая цена; вписанная руками цена оптовую отменяет (иначе непонятно, что
  // победило: та, что видишь в поле, или та, что включилась сама).
  const cutPriceEdited = !!cut?.priceEdited;
  const cutWholeMin = cutPiece && !cutPriceEdited ? Number(cut.material.wholesale_min_qty || 0) : 0;
  const cutWholePrice = cutPiece && !cutPriceEdited ? Number(cut.material.wholesale_price || 0) : 0;
  const cutPieceWholesale = cutPiece && cutWholePrice > 0 && cutWholeMin > 0 && cutPieceQty >= cutWholeMin;
  const cutPieceUnit = cutPieceWholesale ? cutWholePrice : Number(cut?.piecePrice || 0);
  const cutPieceTotal = cutPiece ? cutPieceQty * cutPieceUnit : 0;
  // «Весь лист» тоже можно резать: работа = пог.м × ставка реза материала.
  const cutPieceRunM = Number(cut?.running_meters) || 0;
  const cutPieceRate = Number(cut?.cutRate || 0);
  const cutPieceWork = cutPiece && cut?.pieceCut ? cutPieceRate * cutPieceRunM : 0;
  // Слово для целой единицы в этом окне: лист или рулон.
  const cutWholeUnit = wholeUnitOf(cutMat);
  // Итог = сумма округлённых вверх строк (как в чеке): лист/материал + работа.
  const cutTotal = cutPiece
    ? ceilSom(cutPieceTotal) + ceilSom(cutPieceWork)
    : ceilSom(cutWork) + ceilSom(cutMaterialSum);

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
            {visibleProducts.map((p) => {
              const st = stockState(p);
              const stock = stockLabel(p);
              return (
              <button
                key={p.key}
                className={`pos-product${st ? (st.out ? " is-out" : " is-low") : ""}`}
                onClick={() => tapProduct(p)}
                disabled={!!st?.out}
                title={st?.out ? t("checkout.outOfStockBlock") : undefined}
              >
                {p.kind === "service" && <span className="p-tag">{t(`serviceKind.${p.serviceKind}`)}</span>}
                {st && (
                  <span className={`badge ${st.out ? "red" : "amber"}`} style={{ alignSelf: "flex-start", marginBottom: 6 }}>
                    {st.out ? t("checkout.outOfStock") : t("warehouse.lowStock")}
                  </span>
                )}
                <div>
                  <div className="p-name">{p.name}</div>
                  <div className="p-cat">{p.category}</div>
                  {stock && <div className="p-stock">{t("checkout.stockLeft")} {stock}</div>}
                </div>
                <div className="p-price">
                  {p.kind === "material" ? (
                    p.is_roll_material ? (
                      <>
                        {/* Обе цены сразу: за квадрат — по ней считается кусок,
                            за лист — по ней продают целиком. Раньше на плитке
                            стояла одна, и у материала без цены за лист касса
                            показывала «0 сом» (price_per_unit у листового
                            материала не заполняется). */}
                        {ceilSom(p.sqm_price)} сом/кв.м
                        {p.piece_price > 0 && (
                          <div className="muted" style={{ fontSize: 12 }}>
                            {ceilSom(p.piece_price)}{" "}
                            {t("checkout.perPieceShort", { unit: wholeUnitOf(p) })}
                          </div>
                        )}
                      </>
                    ) : (
                      `${ceilSom(p.price)} сом`
                    )
                  ) : p.uses_area ? (
                    `${ceilSom(p.rate_flat)} сом/кв.м`
                  ) : p.uses_pieces ? (
                    `${ceilSom(p.rate_per_piece)} сом/букву`
                  ) : (
                    `${ceilSom(p.base_price)} сом`
                  )}
                </div>
              </button>
              );
            })}
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
                      {l.width}×{l.length} = {l.area} кв.м · {l.materialName} ·{" "}
                      {t("checkout.rateWork")} {l.rate} × {l.runM} {t("checkout.pmShort")}
                    </div>
                  ) : l.kind === "cut-work" ? (
                    <div className="cl-sub">
                      {t("checkout.rateWork")} {l.rate} × {l.runM} {t("checkout.pmShort")} · {l.materialName}
                    </div>
                  ) : l.kind === "material-area" ? (
                    <div className="cl-sub">{l.width}×{l.length} = {l.area} кв.м · {l.price} сом/кв.м</div>
                  ) : l.mode === "PIECE" ? (
                    <div className="cl-sub">
                      {unitPrice(l)} {t("checkout.perPieceShort", { unit: l.unitWord || t("warehouse.unitSheet") })}
                      {isWholesale(l) && (
                        <span className="badge ok" style={{ marginLeft: 6 }}>{t("checkout.wholesale")}</span>
                      )}
                    </div>
                  ) : (
                    <div className="cl-sub">{unitPrice(l)} сом / ед.</div>
                  )}
                </div>
                {l.kind !== "cutting" && l.kind !== "material-area" && l.kind !== "cut-work" && (
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

          {/* Название заказа — чтобы потом в чеках узнавать работу, а не гадать
              по номеру. Необязательное. */}
          <div className="field" style={{ marginTop: 10 }}>
            <label>{t("checkout.orderTitle")}</label>
            <input
              value={orderTitle}
              onChange={(e) => setOrderTitle(e.target.value)}
              placeholder={t("checkout.orderTitlePh")}
              list="order-title-hints"
            />
            <datalist id="order-title-hints">
              {titleHints.map((x) => (
                <option key={x} value={x} />
              ))}
            </datalist>
          </div>

          {/* Дата заказа задним числом — только админу: по ней считаются
              выручка, прибыль по дням и складской лист, то есть она правит
              деньги уже закрытых месяцев. Складовщик оформляет сегодняшним. */}
          {isAdmin && (
            <div className="field">
              <label>{t("checkout.orderDate")}</label>
              <input
                type="date"
                value={orderDate}
                max={todayStr()}
                onChange={(e) => setOrderDate(e.target.value)}
              />
              <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
                {orderDate !== todayStr() ? t("checkout.orderDateBack") : t("checkout.orderDateHint")}
              </p>
            </div>
          )}

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
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {["CASH", "MBANK", "DEMIRBANK", "ONLINE"].map((m) => (
              <button
                key={m}
                className={paymentMethod === m ? "" : "secondary"}
                style={{ flex: "1 1 45%" }}
                onClick={() => setPaymentMethod(m)}
              >
                {t(`checkout.${m.toLowerCase()}`)}
              </button>
            ))}
          </div>

          {paymentMethod !== "ONLINE" && (
            <div className="field" style={{ marginTop: 10 }}>
              <label>{t("checkout.prepay")}</label>
              <div className="row" style={{ gap: 8, margin: 0 }}>
                <input
                  type="number"
                  min="0"
                  value={payFull ? String(total.toFixed(0)) : prepay}
                  onChange={(e) => {
                    setPayFull(false);
                    setPrepay(e.target.value);
                  }}
                  placeholder="0"
                  style={{ flex: 1 }}
                />
                {/* Обычный случай — заплатили всю сумму: одна кнопка вместо
                    набора цифр, чтобы касса не тормозила на каждой продаже.
                    Пока кнопка нажата, поле показывает живой итог, а серверу
                    уходит флаг «вся сумма», а не число. Вписал своё — флаг снят. */}
                <button
                  type="button"
                  className={payFull ? "" : "secondary"}
                  aria-pressed={payFull}
                  onClick={() => setPayFull(true)}
                  disabled={!total}
                >
                  {t("checkout.payFull")}
                </button>
              </div>
              <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
                {t("checkout.prepayHint")}
              </p>
              {!payFull && Number(prepay || 0) < total && (
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {t("receipts.debt")}:{" "}
                  <strong style={{ color: "var(--danger)" }}>
                    {(total - Number(prepay || 0)).toFixed(0)} сом
                  </strong>
                </div>
              )}
              {/* Переплата больше не пропадает: она запоминается сдачей за
                  клиентом. Отдали на месте — нажать «Выдать» в Чеках, и она
                  спишется; не отдали (в кассе не было мелочи) — останется
                  видна, пока не отдадут. */}
              {!payFull && Number(prepay || 0) > total && total > 0 && (
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {t("checkout.change")}:{" "}
                  <strong style={{ color: "var(--accent-strong)" }}>
                    {(Number(prepay) - total).toFixed(0)} сом
                  </strong>
                  <div style={{ fontSize: 12 }}>{t("checkout.changeHint")}</div>
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
                    ? !(Number(cut.qty) > 0) || !(cutPieceUnit > 0)
                    : !cutArea || (cut.service && !cut.materialId)
                }
              >
                {t("common.add")}
              </button>
            </>
          }
        >
          {/* Четыре способа продажи материала. Раньше вкладок было две, и в
              первую («отрезать кусок») попадали три разных случая — фигурный
              рез, обычный рез и продажа площади без реза; отличались они лишь
              тем, что мастер вписал в поле погонных метров. */}
          {isMatModal && (
            <>
              <div className="tabs tabs-grid" style={{ marginTop: 0 }}>
                {MODES.map((mode) => {
                  // Продажа целиком возможна, только когда у материала задана
                  // цена за лист. Вкладку всё равно показываем — но неактивной
                  // и с объяснением: заказчик знает свои четыре способа по
                  // именам, и молча пропавший из них читается как поломка.
                  const noPiecePrice = mode === "PIECE" && !(Number(cut.material.piece_price) > 0);
                  return (
                    <button
                      key={mode}
                      className={cut.mode === mode ? "active" : ""}
                      disabled={noPiecePrice}
                      title={noPiecePrice ? t("checkout.modePieceNoPrice") : undefined}
                      onClick={() => setCut({ ...cut, mode })}
                    >
                      {t(`checkout.mode${mode[0]}${mode.slice(1).toLowerCase()}`)}
                    </button>
                  );
                })}
              </div>
              <p className="muted" style={{ fontSize: 13, margin: "10px 0 16px" }}>
                {t(`checkout.mode${cut.mode[0]}${cut.mode.slice(1).toLowerCase()}Hint`, {
                  unit: cutWholeUnit,
                })}
              </p>
            </>
          )}

          {/* Станок: ЧПУ или лазер. Показываем, только когда станков правда
              несколько — если он один, спрашивать не о чем, и лишний ряд кнопок
              в кассе только мешает. Смена станка подставляет ЕГО ставку. */}
          {isMatModal && cuttingServices.length > 1 && (CUT_MODES.includes(cut.mode) || cut.pieceCut) && (
            <div className="field">
              <label>{t("checkout.cutMachine")}</label>
              <div className="tabs" style={{ marginTop: 0 }}>
                {cuttingServices.map((s) => (
                  <button
                    key={s.id}
                    className={Number(cut.cutServiceId) === s.id ? "active" : ""}
                    onClick={() =>
                      setCut({ ...cut, cutServiceId: s.id, cutRate: rateFor(s, cut.material) })
                    }
                  >
                    {s.machine_display || s.name}
                  </button>
                ))}
              </div>
            </div>
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
                  <option key={m.id} value={m.id} disabled={Number(m.quantity) <= 0}>
                    {m.name} ({matSqm(m)} сом/кв.м, ост. {m.quantity} кв.м
                    {m.sheets_remaining != null ? ` ≈${Math.round(Number(m.sheets_remaining))} ${t("warehouse.sheetsShort")}` : ""})
                    {Number(m.quantity) <= 0 ? ` — ${t("checkout.outOfStock")}` : ""}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Штучно: количество, цена за штуку и (по желанию) рез */}
          {cutPiece ? (
            <>
              <div className="row">
                <div className="field grow" style={{ margin: 0 }}>
                  <label>{t("common.quantity")} ({t("checkout.pieceUnit")})</label>
                  <input type="number" value={cut.qty} onChange={(e) => setCut({ ...cut, qty: e.target.value })} />
                </div>
                {/* Цена за лист/рулон — на виду и правится прямо здесь, как
                    цена за кв.м у куска. Раньше её в кассе не было вообще:
                    продать лист по особой цене можно было только через
                    справочник, то есть переписав цену всем следующим заказам. */}
                {isAdmin && (
                  <div className="field grow" style={{ margin: 0 }}>
                    <label>{t("checkout.piecePriceLabel", { unit: cutWholeUnit })}</label>
                    <input
                      type="number"
                      step="any"
                      value={cut.piecePrice ?? ""}
                      onChange={(e) => setCut({ ...cut, piecePrice: e.target.value, priceEdited: true })}
                    />
                  </div>
                )}
              </div>
              <label className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  style={{ width: 20, height: 20, minHeight: 0 }}
                  checked={!!cut.pieceCut}
                  onChange={(e) => setCut({ ...cut, pieceCut: e.target.checked })}
                />
                {t("checkout.addCutting")}
              </label>
              {cut.pieceCut && (
                <>
                  <div className="field">
                    <label>{t("checkout.runningMeters")}</label>
                    <input
                      type="number"
                      step="any"
                      value={cut.running_meters}
                      onChange={(e) => setCut({ ...cut, running_meters: e.target.value })}
                      autoFocus
                    />
                    <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("checkout.pieceCutHint")}</p>
                  </div>
                  {isAdmin && (
                    <div className="field">
                      <label>{t("checkout.cutRateLabel")}</label>
                      <input type="number" step="any" value={cut.cutRate ?? ""} onChange={(e) => setCut({ ...cut, cutRate: e.target.value })} />
                    </div>
                  )}
                </>
              )}
            </>
          ) : (
            <>
              <div className="row">
                <div className="field grow"><label>{t("supply.width")}</label><input type="number" step="any" value={cut.width} onChange={(e) => setCut({ ...cut, width: e.target.value })} /></div>
                <div className="field grow"><label>{t("supply.length")}</label><input type="number" step="any" value={cut.length} onChange={(e) => setCut({ ...cut, length: e.target.value })} /></div>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>{t("checkout.sizeHint")}</p>
              {/* Длина кривой — только у фигурного реза. У обычного её вводить
                  не нужно: в рез идёт одна сторона куска, и система берёт её
                  сама из «Длины». */}
              {isMatModal && cut.mode === "CURVE" && (
                <div className="field">
                  <label>{t("checkout.runningMeters")}</label>
                  <input
                    type="number"
                    step="any"
                    value={cut.running_meters}
                    onChange={(e) => setCut({ ...cut, running_meters: e.target.value })}
                  />
                  <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("checkout.runMetersHint")}</p>
                </div>
              )}
              {isMatModal && cut.mode === "SIDE" && cutRunM > 0 && (
                <p className="muted" style={{ fontSize: 12, margin: "0 0 12px" }}>
                  {t("checkout.sideAuto", { value: cutRunM })}
                </p>
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
                  {/* Строка «подпись — расчёт», как в режимах по площади ниже.
                      Раньше здесь болталось одинокое «4381 сом × 1» слева, без
                      пары справа, и итог не читался как продолжение строки. */}
                  <div className="crow">
                    <span className="k">
                      {/* Подпись строки — с заглавной, как «Площадь» и
                          «Материал» в соседних режимах. */}
                      {cutWholeUnit.charAt(0).toUpperCase() + cutWholeUnit.slice(1)}
                      {cutPieceWholesale && (
                        <span className="badge ok" style={{ marginLeft: 6 }}>{t("checkout.wholesale")}</span>
                      )}
                    </span>
                    <span>{cutPieceUnit} × {cutPieceQty} = {ceilSom(cutPieceTotal)}</span>
                  </div>
                  {!cutPieceWholesale && cutWholePrice > 0 && cutWholeMin > 0 && (
                    <div className="crow" style={{ paddingTop: 0 }}>
                      <span className="muted" style={{ fontSize: 12 }}>
                        {t("checkout.wholesaleFrom", { n: cutWholeMin, price: cutWholePrice })}
                      </span>
                    </div>
                  )}
                  {cut.pieceCut && cutPieceWork > 0 && (
                    <div className="crow">
                      <span className="k">{t("checkout.rateWork")}</span>
                      <span>{cutPieceRate} × {cutPieceRunM} = {ceilSom(cutPieceWork)}</span>
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="crow"><span className="k">{t("supply.area")}</span><strong>{cutArea} кв.м</strong></div>
                  {cutWorkOn && cutWorkRate > 0 && (
                    <div className="crow"><span className="k">{t("checkout.rateWork")}</span><span>{cutWorkRate} × {cutRunM} = {ceilSom(cutWork)}</span></div>
                  )}
                  <div className="crow"><span className="k">{t("checkout.rateMaterial")}</span><span>{cutMatSqm} × {cutArea} = {ceilSom(cutMaterialSum)}</span></div>
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
        <Modal title={`${t("checkout.receipt")} №${receipt.order_number}`} onClose={() => setReceipt(null)}>
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

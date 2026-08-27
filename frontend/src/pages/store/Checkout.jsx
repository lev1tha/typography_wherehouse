import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import { PaymentBadge } from "../../components/StatusBadge.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { areaOf } from "../../utils/area.js";

// Цену округляем ВВЕРХ до целого сома (решение заказчика), как на бэкенде
// (TransactionItem.line_total). Эпсилон гасит float-шум, чтобы целое не «прыгало».
const ceilSom = (v) => Math.max(0, Math.ceil((Number(v) || 0) - 1e-6));
// Сумма целыми сомами с разрядами — как в чеках и печатных формах.
const somFmt = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
// Количество без хвоста нулей: 1.230 → 1.23, 2.000 → 2.
const trimQty = (n) => String(+Number(n || 0).toFixed(3));

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
  // Цена ЗА ЕДИНИЦУ, а не сумма строки: `lineTotal` домножит её на `lineQty`
  // (у рулона это длина). Вернуть отсюда полную сумму значит умножить длину
  // дважды — 1.5 м по 1000 давали 2 250 вместо 1 500.
  if (line.kind === "material-metre") return Number(line.price);
  if (line.kind === "material" || line.kind === "material-area") {
    if (line.mode === "PIECE" && isWholesale(line)) return Number(line.wholesale_price);
    return Number(line.price);
  }
  return Number(line.unit_price); // per-piece (exterior) or fixed service
}
// Quantity that price multiplies by (area-material bills by area).
function lineQty(line) {
  if (line.kind === "material-metre") return Number(line.length);
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

/** Строки прошлого заказа → строки корзины: «сделать как в прошлый раз».
 *
 * Состав берём из чека, а ЦЕНЫ — сегодняшние из каталога. Повтор прошлогоднего
 * заказа по прошлогодним ценам — это продажа в убыток, о которой никто не
 * узнает; кассир видит итог до оформления и всегда может его поправить.
 *
 * Резка лежит в чеке ДВУМЯ строками — работа (в ней и размеры куска) и сразу
 * за ней материал по площади. Их склеиваем обратно в одну строку кассы: иначе
 * в корзине окажутся две половинки одной позиции, и убрать её целиком нельзя.
 */
function cartFromReceipt(items, materials, services, t) {
  const matById = Object.fromEntries(materials.map((m) => [String(m.id), m]));
  const svcById = Object.fromEntries(services.map((s) => [String(s.id), s]));
  const sqmPrice = (m) => Number(m.sqm_price ?? m.price_per_sqm ?? m.price_per_unit ?? 0);
  // Цена — сегодняшняя, но НЕ ноль: ставка реза может быть не проставлена в
  // каталоге (её вводили руками в момент продажи), и «повторить» тихо отдало
  // бы работу даром. Нет сегодняшней цены — берём ту, по которой продали.
  const priceOrLast = (current, item) => Number(current) || Number(item.price_per_item) || 0;
  // Цена взята из старого чека, потому что в каталоге её нет: на сервер она
  // уходит ЯВНО (иначе сервер отклонит неявный ноль каталога), и админ видит
  // её как правленную руками — так и есть.
  const fromLast = (current, item) => !Number(current) && Number(item.price_per_item) > 0;
  const lines = [];
  const used = new Set();
  let skipped = 0;

  items.forEach((it, i) => {
    if (used.has(i)) return;
    const qty = Number(it.quantity) || 0;

    if (it.type === "SERVICE") {
      const s = it.service ? svcById[String(it.service)] : null;
      if (!s) return void skipped++;
      // Парный материал — следующая строка чека: сервер пишет их подряд.
      const next = items[i + 1];
      const pair =
        next && next.type === "MATERIAL" && next.sale_mode !== "PIECE" && next.material
          ? next
          : null;
      const m = pair ? matById[String(pair.material)] : null;
      if (pair && !m) return void skipped++;

      if (s.uses_area && m) {
        used.add(i + 1);
        // Ставка: своя у станка, иначе с материала. Так же её выбирает сервер.
        const rate = priceOrLast(
          s.uses_running_meter ? s.rate_per_pm || m.cut_rate_per_pm : s.rate_flat,
          it
        );
        lines.push({
          key: `C${m.id}-${i}`, kind: "cutting",
          serviceId: s.id, name: s.name,
          materialId: m.id, materialName: m.name,
          materialPrice: priceOrLast(sqmPrice(m), pair),
          materialPriceEdited: fromLast(sqmPrice(m), pair),
          rate,
          rateEdited: fromLast(s.uses_running_meter ? s.rate_per_pm || m.cut_rate_per_pm : s.rate_flat, it),
          width: Number(it.width) || 0, length: Number(it.length) || 0,
          area: Number(pair.quantity) || 0, runM: qty, qty: 1,
        });
        return;
      }
      // Работа реза без материала по площади (режут лист целиком или чужой
      // материал): только пог.м × ставка.
      if (s.uses_running_meter) {
        // Из чего резали. У строки работы своего материала НЕТ (в базе она его
        // не хранит — и не должна: отчёты относят резку к товарной строке
        // чека), поэтому берём материал соседней строки этого же чека — так же,
        // как это делает отчёт по материалам. Без этого подпись при повторе
        // обрывалась: «Ставка реза 45 × 4.9 пог.м · » и пустота после точки.
        const own = it.material ? matById[String(it.material)] : null;
        const sibling = items.find(
          (x) => x.type === "MATERIAL" && x.material && matById[String(x.material)]
        );
        const cutMat = own || (sibling ? matById[String(sibling.material)] : null);
        lines.push({
          key: `CW${s.id}-${i}`, kind: "cut-work",
          serviceId: s.id, name: s.name,
          materialId: it.material || null, materialName: cutMat?.name || "",
          rate: priceOrLast(s.rate_per_pm, it), rateEdited: fromLast(s.rate_per_pm, it), runM: qty, qty: 1,
        });
        return;
      }
      // Обычная услуга по прейскуранту: цена берётся из каталога.
      lines.push({
        key: `S${s.id}`, kind: "service", id: s.id, name: s.name,
        unit_price: priceOrLast(s.uses_pieces ? s.rate_per_piece : s.base_price, it),
        qty: qty || 1,
      });
      return;
    }

    // Материала больше нет в каталоге (удалён или скрыт) — не подставляем
    // молча: кассир должен знать, что позиция выпала.
    const m = it.material ? matById[String(it.material)] : null;
    if (!m || qty <= 0) return void skipped++;

    // Рулон — только длиной. Раньше METER-строка проваливалась в «материал по
    // площади»: 1 пог.м становился 1 кв.м по цене за кв.м, которой у рулона
    // нет, — повтор продавал рулон за 0 сом и списывал не ту площадь. Строку
    // старого чека, где рулон продан не метрами (или материал с тех пор стал
    // рулоном), повторить нечем — сервер её отклонит, поэтому пропускаем вслух.
    if (m.sells_by_metre) {
      if (it.sale_mode !== "METER") return void skipped++;
      lines.push({
        key: `MM${m.id}-${i}`, kind: "material-metre", id: m.id, name: m.name,
        price: priceOrLast(m.price_per_pm, it), priceEdited: fromLast(m.price_per_pm, it),
        length: qty, qty: 1,
        // Рулон не переносим: тот, из которого резали в прошлый раз, мог
        // кончиться; сервер возьмёт початый (FIFO). Ширина изделия — та же:
        // это свойство самой работы, а не рулона.
        rollId: null, rollName: "", usedWidth: Number(it.used_width) || null,
      });
      return;
    }
    if (it.sale_mode === "METER") return void skipped++;

    if (it.sale_mode === "PIECE") {
      lines.push({
        key: `M${m.id}-PIECE`, kind: "material", id: m.id, name: m.name,
        price: priceOrLast(m.piece_price || m.price_per_unit, it), mode: "PIECE", qty,
        priceEdited: fromLast(m.piece_price || m.price_per_unit, it),
        unitWord: t((m.intake_form || "SHEET") === "ROLL" ? "warehouse.unitRoll" : "warehouse.unitSheet"),
        wholesale_price: Number(m.wholesale_price || 0),
        wholesale_min_qty: Number(m.wholesale_min_qty || 0),
      });
      return;
    }
    // Штучный материал (крепёж, клей) — обычная строка по количеству.
    if (!m.is_roll_material) {
      lines.push({
        key: `M${m.id}`, kind: "material", id: m.id, name: m.name,
        price: priceOrLast(m.price_per_unit, it), priceEdited: fromLast(m.price_per_unit, it), qty,
      });
      return;
    }
    lines.push({
      key: `MA${m.id}-${i}`, kind: "material-area", id: m.id, name: m.name,
      price: priceOrLast(sqmPrice(m), it), priceEdited: fromLast(sqmPrice(m), it),
      width: Number(it.width) || 0, length: Number(it.length) || 0,
      area: qty, qty: 1,
    });
  });

  return { lines, skipped };
}

export default function Checkout() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { toast } = useUI();
  // ?repeat=<id> — «повторить заказ» из «Чеков» или карточки клиента.
  const [searchParams, setSearchParams] = useSearchParams();
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
  // Невыданная сдача выбранного клиента и решение кассира: зачесть её в этот
  // заказ или нет. Раньше сдача просто лежала на прошлом чеке и в новом заказе
  // не участвовала никак — её приходилось выдавать на руки и тут же принимать
  // обратно. По умолчанию зачитываем: это то, чего ждёт и клиент, и касса.
  const [clientChange, setClientChange] = useState(0);
  const [useChange, setUseChange] = useState(true);
  // Долг клиента по прошлым заказам и решение кассира: берём ли деньги за него
  // сейчас. Зеркало сдачи, но галочка ВЫКЛЮЧЕНА по умолчанию: сдача — деньги
  // цеха, а долг — деньги клиента, и решает он, платить ли сегодня.
  const [clientDebt, setClientDebt] = useState(0);
  const [payDebt, setPayDebt] = useState(false);
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

  // Повтор заказа: состав прошлого чека перекладывается в корзину. Ждём, пока
  // подгрузятся каталог и услуги — по ним берутся сегодняшние цены. Параметр
  // из адреса убираем сразу, чтобы перезагрузка страницы не задвоила корзину.
  const repeatId = searchParams.get("repeat");
  useEffect(() => {
    if (!repeatId || !materials.length || !services.length) return;
    api
      .get(`/sales/receipts/${repeatId}/`)
      .then((r) => {
        const src = (r.data.items || []).filter((i) => !i.is_returned);
        const { lines, skipped } = cartFromReceipt(src, materials, services, t);
        if (!lines.length) {
          toast(t("checkout.repeatEmpty"), "error");
          return;
        }
        setCart(lines);
        if (r.data.title) setOrderTitle(r.data.title);
        if (r.data.client) {
          setClientId(r.data.client);
          api
            .get(`/clients/clients/${r.data.client}/`)
            .then((c) =>
              setClient({
                type: c.data.type,
                full_name: c.data.full_name || "",
                company_name: c.data.company_name || "",
                phone: c.data.phone || "",
              })
            )
            .catch(() => {});
        }
        toast(
          skipped
            ? t("checkout.repeatedPartly", { n: lines.length, skipped })
            : t("checkout.repeated", { n: lines.length })
        );
      })
      .catch(() => toast(t("common.error"), "error"))
      .finally(() => {
        const next = new URLSearchParams(searchParams);
        next.delete("repeat");
        setSearchParams(next, { replace: true });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repeatId, materials.length, services.length]);

  // Сдача выбранного клиента. Спрашиваем сервер, а не берём из списка: клиент
  // мог получить её на руки в соседнем окне, а список в кассе живёт до
  // перезагрузки страницы.
  useEffect(() => {
    if (!clientId) {
      setClientChange(0);
      setClientDebt(0);
      setPayDebt(false);
      return;
    }
    api
      .get(`/clients/clients/${clientId}/`)
      .then((r) => {
        setClientChange(Number(r.data.change_due) || 0);
        setClientDebt(Number(r.data.debt) || 0);
        setPayDebt(false);
      })
      .catch(() => { setClientChange(0); setClientDebt(0); });
  }, [clientId]);

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
      // Рулон: цена и остаток у него в ПОГОННЫХ метрах. Без этих полей плитка
      // показывала «0 сом/кв.м» (у рулона `price_per_sqm` законно нулевой) и
      // остаток в квадратах, которые владелец в уме делит на ширину.
      sells_by_metre: !!m.sells_by_metre,
      price_per_pm: Number(m.price_per_pm ?? 0),
      metres_remaining: m.metres_remaining != null ? Number(m.metres_remaining) : null,
    }));
    return [...svc, ...mat];
  }, [materials, services, t]);

  // Партии рулонных материалов: мастеру надо знать, какой физический рулон он
  // режет. «Остаток 2.9 м» — это не один рулон, а початый на 0.9 и целый на 2.0.
  const [rolls, setRolls] = useState([]);
  useEffect(() => {
    api
      .get("/warehouse/rolls/", { params: { page_size: 500 } })
      .then((r) => setRolls((r.data.results ?? r.data).filter((x) => Number(x.remaining_area) > 0)))
      .catch(() => setRolls([]));
  }, [materials.length]);

  // Материалы к площадной услуге (внутренний монтаж) — лист или кусок по кв.м.
  // Рулон сюда не идёт: у него нет цены за кв.м, строка материала ушла бы за 0,
  // и сервер такую услугу отклоняет; метры рулона — отдельной строкой.
  const areaMaterials = materials.filter((m) => m.is_roll_material && !m.sells_by_metre);
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
  // Сколько сдачи уйдёт в этот заказ и сколько после этого брать деньгами.
  // Сдача не может закрыть больше, чем стоит заказ: остаток так и лежит
  // сдачей — «зачли 1 500 из 4 000» это нормальная ситуация, а не ошибка.
  const appliedChange = useChange && clientChange > 0 ? Math.min(clientChange, total) : 0;
  // Долг НЕ уменьшается сдачей: сдача закрывает этот заказ, а долг — прошлые.
  const debtNow = payDebt && clientDebt > 0 ? clientDebt : 0;
  const toPay = Math.max(0, total - appliedChange);
  // Сколько денег кассир берёт с клиента сейчас: заказ после зачёта плюс долг.
  const cashNow = toPay + debtNow;
  // От чего считать «долг» и «сдачу» под полем: без погашения — от заказа,
  // с погашением — от заказа и долга вместе (в поле вписывают всё принесённое).
  const owedNow = cashNow;

  // Remaining stock shown on EVERY material card during a sale: quantity in its
  // unit, plus ≈ whole sheets when the material has a sheet area. Null for services.
  function stockLabel(p) {
    if (p.kind !== "material") return null;
    // Рулон меряют метрами — в них и показываем, без пересчёта в уме.
    if (p.sells_by_metre && p.metres_remaining != null) {
      return `${+p.metres_remaining.toFixed(2)} ${t("unit.METER")}`;
    }
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
    // У РУЛОНА розничная цена живёт за погонный метр: `price_per_sqm` у него
    // законно нулевой, и подстановка «цены за кв.м» оставляла поле пустым —
    // итог 0, кнопка «Добавить» заблокирована, продать нечем.
    if (m?.sells_by_metre) return Number(m.price_per_pm ?? 0);
    return Number(m.sqm_price ?? m.price_per_sqm ?? m.price_per_unit ?? 0);
  }
  // Лист или рулон — как задано на самом материале. «Цена за лист» у плёнки,
  // которая приходит рулоном, отвечает не на тот вопрос.
  // Подпись рулона: маркировка, если её писали при приёмке, иначе номер. Плюс
  // остаток и почём он куплен — мастер выбирает не «партию №8», а «тот, что
  // подороже, но целый».
  // Себестоимость метра — только админу (сервер складовщику её и не отдаёт):
  // в подписи рулона у складовщика остаются номер и остаток.
  const rollLabel = (r) =>
    `${r.code || `№${r.id}`} · ${r.metres_remaining} ${t("unit.METER")}` +
    (isAdmin && r.cost_per_pm != null
      ? ` · ${Math.round(Number(r.cost_per_pm))} ${t("checkout.perPieceShort", { unit: t("unit.METER") })}`
      : "");

  // Партия ЛИСТА: остаток считаем в листах — пачками их и считают на стеллаже.
  const sheetLotLabel = (r, m) => {
    const area = Number(m?.piece_area) || 0;
    const left = area ? `${(Number(r.remaining_area) / area).toFixed(2)} ${t("warehouse.unitSheetShort")}`
                      : `${r.remaining_area} ${t("unit.SQM")}`;
    const cost = isAdmin && r.cost_per_sqm != null && area
      ? ` · ${Math.round(Number(r.cost_per_sqm) * area)} ${t("checkout.perPieceShort", { unit: t("warehouse.unitSheet") })}`
      : "";
    return `${r.code || `№${r.id}`} · ${left}${cost}`;
  };

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
          lotId: cutSheetLotPicked ? cutSheetLotPicked.id : null,
          lotName: cutSheetLotPicked ? (cutSheetLotPicked.code || `№${cutSheetLotPicked.id}`) : "",
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
            rate: Number(cut.cutRate || 0), rateEdited: !!cut.cutRateEdited, runM, qty: 1,
          }]);
        }
        setCut(null);
        return;
      }
      // Рулон: в корзину уходит ДЛИНА, ширину не спрашиваем — она постоянная.
      if (m.sells_by_metre) {
        const len = Number(cut.length);
        if (!len) return;
        setCart((prev) => [...prev, {
          key: `MM${m.id}-${prev.length}`, kind: "material-metre",
          id: m.id, name: m.name,
          price: Number(cut.matPrice ?? m.price_per_pm) || 0,
          priceEdited: !!cut.priceEdited,
          length: len, qty: 1,
          rollId: cutRollPicked ? cutRollPicked.id : null,
          rollName: cutRollPicked ? (cutRollPicked.code || `№${cutRollPicked.id}`) : "",
          usedWidth: cutUsedWidth > 0 && cutUsedWidth < cutFullWidth ? cutUsedWidth : null,
        }]);
        // Контурная резка по рулону — своей строкой работы, тем же путём, что
        // и рез целого листа: материал уходит в неё только ради ставки, без
        // размеров куска (иначе сервер посчитал бы рулон ещё и по площади).
        const rollRunM = Number(cut.running_meters) || 0;
        const rollSvc = svcById(cut.cutServiceId);
        if (cut.rollCut && rollSvc && rollRunM > 0) {
          setCart((prev) => [...prev, {
            key: `CW${m.id}-${prev.length}-${rollRunM}`, kind: "cut-work",
            serviceId: rollSvc.id, name: rollSvc.name || "Резка",
            materialId: m.id, materialName: m.name,
            rate: Number(cut.cutRate || 0), rateEdited: !!cut.cutRateEdited,
            runM: rollRunM, qty: 1,
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
          id: m.id, name: m.name, price: matPrice, priceEdited: !!cut.matPriceEdited,
          width: w, length: l, area, qty: 1,
          lotId: cutSheetLotPicked ? cutSheetLotPicked.id : null,
          lotName: cutSheetLotPicked ? (cutSheetLotPicked.code || `№${cutSheetLotPicked.id}`) : "",
        }]);
        setCut(null);
        return;
      }
      const areaSvc = svcById(cut.cutServiceId);
      setCart((prev) => [...prev, {
        key: `C${m.id}-${prev.length}`, kind: "cutting",
        serviceId: areaSvc.id, name: areaSvc.name || "Резка",
        lotId: cutSheetLotPicked ? cutSheetLotPicked.id : null,
        lotName: cutSheetLotPicked ? (cutSheetLotPicked.code || `№${cutSheetLotPicked.id}`) : "",
        materialId: m.id, materialName: m.name, materialPrice: matPrice,
        materialPriceEdited: !!cut.matPriceEdited,
        rate: Number(cut.cutRate || 0), rateEdited: !!cut.cutRateEdited,
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
    // Резка без длины реза — работа за ноль; сервер такой заказ отклонит, но
    // причину лучше назвать здесь, вместе со строкой, которую надо переделать.
    const noRunM = cart.find((l) => (l.kind === "cutting" || l.kind === "cut-work") && !(Number(l.runM) > 0));
    if (noRunM) return setError(t("checkout.runMetersRequiredCart", { name: noRunM.name }));
    // Гасить долг нечем, если не сказано, сколько принесли: пустое поле —
    // это «ничего не приняли», и долг из ничего не закрывается.
    if (payDebt && debtNow > 0 && paymentMethod !== "ONLINE" && !payFull && !(Number(prepay) > 0))
      return setError(t("checkout.payDebtNeedsAmount"));
    if (!clientId && client.phone) {
      if (client.type === "PHYSICAL" && !client.full_name.trim()) return setError(t("checkout.needName"));
      if (client.type === "OSOO" && !client.company_name.trim()) return setError(t("checkout.needCompany"));
    }
    setBusy(true);
    const items = cart.map((l) => {
      if (l.kind === "material")
        return {
          type: "MATERIAL", material: l.id, quantity: l.qty, mode: l.mode || "SQM",
          // Пачка листа уходит тем же полем, что и рулон: на сервере это одна
          // и та же партия (Roll), просто формы разные.
          ...(l.lotId ? { roll: l.lotId } : {}),
          // Цену шлём, только если админ правил её руками. Иначе сервер сам
          // решит, розничная тут цена или оптовая (опт включается от количества).
          ...(isAdmin && l.priceEdited ? { material_price: l.price } : {}),
        };
      if (l.kind === "material-metre")
        // Режим шлём ЯВНО: сервер его не угадывает, иначе площадь и длина
        // молча поменялись бы местами.
        return {
          type: "MATERIAL", material: l.id, quantity: l.length, mode: "METER",
          ...(l.rollId ? { roll: l.rollId } : {}),
          ...(l.usedWidth ? { used_width: l.usedWidth } : {}),
          // Цену шлём, только если админ её правил (или её нет в каталоге и
          // взята из прошлого чека). Нетронутая — сервер берёт каталог сам, а
          // пустой каталог отклоняет: неявный ноль — ошибка ввода, явный
          // (вписанный) ноль — подарок.
          ...(isAdmin && l.priceEdited ? { material_price: l.price } : {}),
        };
      if (l.kind === "material-area")
        return {
          type: "MATERIAL", material: l.id, quantity: l.area, mode: "SQM",
          ...(l.lotId ? { roll: l.lotId } : {}),
          ...(isAdmin && l.priceEdited ? { material_price: l.price } : {}),
        };
      if (l.kind === "cutting")
        return {
          type: "SERVICE", service: l.serviceId, material: l.materialId,
          width: l.width, length: l.length, running_meters: l.runM,
          ...(l.lotId ? { roll: l.lotId } : {}),
          ...(isAdmin && l.materialPriceEdited ? { material_price: l.materialPrice } : {}),
          ...(isAdmin && l.rateEdited ? { cut_rate: l.rate } : {}),
        };
      if (l.kind === "cut-work")
        // Материал передаём (для ставки реза), но без размеров → площадь 0 →
        // бэкенд создаёт только строку работы, без материала по площади.
        return {
          type: "SERVICE", service: l.serviceId, material: l.materialId,
          running_meters: l.runM,
          ...(isAdmin && l.rateEdited ? { cut_rate: l.rate } : {}),
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
      // Зачесть сдачу решает касса, а СКОЛЬКО зачесть — сервер: сдача могла
      // измениться, пока чек собирали, и своё число касса бы не угадала.
      if (useChange && clientChange > 0) payload.use_change = true;
      // Сумму долга считает сервер: он же собирает список заказов под погашение
      // ДО продажи, чтобы новый заказ не попал сам под себя.
      if (payDebt && clientDebt > 0) payload.pay_debt = true;
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
      setClientChange(0);
      setUseChange(true);
      setClientDebt(0);
      setPayDebt(false);
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
  // Рулон продаётся ДЛИНОЙ, и решает это справочник, а не мастер: ширина у
  // рулона постоянная (её режут поперёк целиком), выбирать её нечего. Поэтому
  // для такого материала показываем ОДНО поле — длину, а ширину пишем надписью.
  const cutRollMat = isMatModal ? (cut.material || materials.find((m) => m.id === Number(cut.materialId))) : null;
  const cutRoll = !!(cutRollMat && cutRollMat.sells_by_metre);
  const cutRollLen = cutRoll ? Number(cut.length) || 0 : 0;
  const cutRollRate = cutRoll ? Number(cut.matPrice ?? cutRollMat.price_per_pm) || 0 : 0;
  const cutRollTotal = cutRoll ? ceilSom(cutRollLen * cutRollRate) : 0;
  // Контурная резка по рулону — ОТДЕЛЬНАЯ работа, а не отрез поперёк: тот в
  // цене метра уже сидит. Поэтому она за галочкой и со своей длиной реза,
  // которую вводит мастер: из длины отреза её не вывести (метр плёнки можно
  // изрезать на десять метров контура).
  const cutRollWorkOn = cutRoll && !!cut?.rollCut;
  const cutRollRunM = cutRollWorkOn ? Number(cut.running_meters) || 0 : 0;
  const cutRollWorkRate = cutRollWorkOn ? Number(cut.cutRate) || 0 : 0;
  const cutRollWork = ceilSom(cutRollRunM * cutRollWorkRate);
  // Списывается вся ширина полотна — это и есть остаток после отреза.
  // Рулоны этого материала — початые первыми, как их и режут.
  const cutRolls = cutRoll
    ? rolls
        // Форма партии — ROLL: у листовой ширина тоже задана, и она пролезла бы
        // в выбор «из какого рулона режем», где ей делать нечего.
        .filter((r) => r.material === cutRollMat.id && r.form === "ROLL" && Number(r.metres_remaining) > 0)
        .sort((a, b) => new Date(a.received_at) - new Date(b.received_at))
    : [];
  // Партии ЛИСТА того же материала: пачки с разной ценой закупки. Выбор нужен
  // по той же причине, что и у рулона, — мастер берёт лист из конкретной пачки,
  // а не из той, которую первой посчитал бы FIFO.
  const cutSheetLots = isMatModal && cutRollMat && !cutRoll && cutRollMat.is_roll_material
    ? rolls
        .filter((r) => r.material === cutRollMat.id && r.form === "SHEET" && Number(r.remaining_area) > 0)
        .sort((a, b) => new Date(a.received_at) - new Date(b.received_at))
    : [];
  const cutSheetLotPicked =
    cutSheetLots.find((r) => String(r.id) === String(cut?.lotId)) || cutSheetLots[0] || null;
  // По умолчанию — первый в очереди: початый дожигают раньше целого.
  const cutRollPicked =
    cutRolls.find((r) => String(r.id) === String(cut?.rollId)) || cutRolls[0] || null;
  // Обрезок: ширина рулона минус то, что реально уходит клиенту. Полосу 0.5 м
  // от рулона 0.9 отрезают на всю ширину, 0.4 остаётся в цехе.
  const cutUsedWidth = cutRoll ? Number(cut.usedWidth) || 0 : 0;
  // Ширина — ТОГО рулона, с которого режем: она заморожена в партии, а в
  // карточке лишь значение по умолчанию (под одной карточкой лежат рулоны
  // разной ширины). Обрезок и «шире рулона» считаются от неё же.
  const cutFullWidth = cutRoll
    ? Number(cutRollPicked?.width) || Number(cutRollMat.roll_width) || 0
    : 0;
  const cutOffcut =
    cutUsedWidth > 0 && cutUsedWidth < cutFullWidth && cutRollLen > 0
      ? +((cutFullWidth - cutUsedWidth) * cutRollLen).toFixed(3)
      : 0;
  const cutRollLeft = cutRollPicked
    ? Number(cutRollPicked.metres_remaining || 0) - cutRollLen
    : Number(cutRollMat?.metres_remaining || 0) - cutRollLen;
  const cutArea = cut && Number(cut.width) && Number(cut.length) ? areaOf(cut.width, cut.length) : 0;
  const cutMat = cut ? (cut.material || materials.find((m) => m.id === Number(cut.materialId))) : null;
  // Сколько площади просит эта строка и сколько её осталось на складе. В корзине
  // уже может лежать этот же материал — учитываем, иначе две строки по 8 кв.м
  // при остатке 14 обе выглядели бы допустимыми.
  const cutInCart = cutMat
    ? cart.reduce(
        (sum, l) =>
          (l.kind === "material-area" || l.kind === "cutting") && l.materialId === cutMat.id
            ? sum + Number(l.area || 0)
            : (l.kind === "material" && l.id === cutMat.id ? sum + Number(l.qty || 0) : sum),
        0
      )
    : 0;
  const cutNeed = Number((cutArea + cutInCart).toFixed(3));
  const cutLeft = cutMat ? Number(Number(cutMat.quantity).toFixed(3)) : 0;
  const cutShort = cutMat && !cutPiece && cutArea > 0 ? Number((cutNeed - cutLeft).toFixed(3)) : 0;
  // Editable (overridable) prices — default to the material's catalogue values.
  const cutMatSqm = cut ? Number(cut.matPrice || 0) : 0;
  // Работа реза считается у услуги «внутренний монтаж» и в режимах реза.
  // Услуги резки в каталоге нет вовсе (её ещё не завели) — работа в чек НЕ
  // попадёт: строку резки не из чего собрать. Раньше касса всё равно считала
  // её в окне, а в чек клала один материал: показывала 472, добавляла 427.
  // Считаем работу только тогда, когда она правда будет продана.
  const cutWorkOn = cut?.service
    ? true
    : isMatModal && CUT_MODES.includes(cut.mode) && !!cuttingService;
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
  const cutPieceWork =
    cutPiece && cut?.pieceCut && cuttingService ? cutPieceRate * cutPieceRunM : 0;
  // Резать просят, а услуги нет — единственный честный ответ «так продать
  // нельзя»: молча снять работу значит подарить её, а показать в окне и не
  // положить в чек — соврать. Оба варианта уже были.
  const cutNoService =
    isMatModal &&
    !cuttingService &&
    (CUT_MODES.includes(cut?.mode) || !!cut?.pieceCut || !!cut?.rollCut);
  // Длина реза, которую мастер должен ввести САМ и ещё не ввёл. У фигурного
  // реза длину кривой знает только он, и пустое поле раньше молча уезжало в чек
  // нулём: материал посчитан, а самая дорогая работа цеха — бесплатно. Пока
  // длины нет, позицию добавить нельзя; сервер её тоже не примет.
  const cutRunMMissing =
    !!cuttingService &&
    ((isMatModal && !cutRoll && !cutPiece && cut.mode === "CURVE" && !(cutRunM > 0)) ||
      (cutPiece && !!cut.pieceCut && !(cutPieceRunM > 0)));
  // Нулевая ставка/цена БЕЗ правки руками — это пустой каталог, а не скидка:
  // сервер такую строку отклонит, а здесь причина видна до кнопки. Вписанный
  // админом ноль — осознанный подарок, его пропускаем.
  const cutRateMissing =
    isMatModal && !cutRoll && !cutPiece && cutWorkOn && !(cutWorkRate > 0) && !cut.cutRateEdited;
  const cutPriceMissing =
    isMatModal && !cutRoll && !cutPiece && !(cutMatSqm > 0) && !cut.matPriceEdited;
  // Слово для целой единицы в этом окне: лист или рулон.
  const cutWholeUnit = wholeUnitOf(cutMat);
  // Итог = сумма округлённых вверх строк (как в чеке): лист/материал + работа.
  const cutTotal = cutRoll
    ? cutRollTotal + cutRollWork
    : cutPiece
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
              // Штучный материал без цены в каталоге продать нечем: раньше
              // плитка «0 сом» нажималась и строка уходила в чек бесплатно.
              // Лист/рулон и услуги свою причину покажут в окне.
              const noPrice = p.kind === "material" && !p.is_roll_material && !(p.price > 0);
              return (
              <button
                key={p.key}
                className={`pos-product${st ? (st.out ? " is-out" : " is-low") : ""}`}
                onClick={() => tapProduct(p)}
                disabled={!!st?.out || noPrice}
                title={st?.out ? t("checkout.outOfStockBlock") : noPrice ? t("checkout.priceMissing") : undefined}
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
                    p.sells_by_metre ? (
                      // У рулона цена одна — за погонный метр. Цены за квадрат
                      // и «за лист целиком» у него не бывает.
                      <>
                        {ceilSom(p.price_per_pm)}{" "}
                        {t("checkout.perPieceShort", { unit: t("unit.METER") })}
                      </>
                    ) : p.is_roll_material ? (
                      <>
                        {/* Обе цены сразу: за квадрат — по ней считается кусок,
                            за лист — по ней продают целиком. Раньше на плитке
                            стояла одна, и у материала без цены за лист касса
                            показывала «0 сом» (price_per_unit у листового
                            материала не заполняется). */}
                        {ceilSom(p.sqm_price)} {t("checkout.perPieceShort", { unit: t("unit.SQM") })}
                        {p.piece_price > 0 && (
                          <div className="muted" style={{ fontSize: 12 }}>
                            {ceilSom(p.piece_price)}{" "}
                            {t("checkout.perPieceShort", { unit: wholeUnitOf(p) })}
                          </div>
                        )}
                      </>
                    ) : noPrice ? (
                      <span style={{ color: "var(--danger)", fontSize: 12 }}>{t("checkout.priceMissingShort")}</span>
                    ) : (
                      `${ceilSom(p.price)} сом`
                    )
                  ) : p.uses_area ? (
                    `${ceilSom(p.rate_flat)} ${t("checkout.perPieceShort", { unit: t("unit.SQM") })}`
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
                      {l.width}×{l.length} = {l.area} {t("unit.SQM")} · {l.materialName} ·{" "}
                      {t("checkout.rateWork")} {l.rate} × {l.runM} {t("checkout.pmShort")}
                      {/* Строка без длины реза может прийти только из повтора
                          старого заказа, где работа ушла нулём. Сервер такой
                          заказ не примет — говорим об этом заранее. */}
                      {!(Number(l.runM) > 0) && (
                        <span className="badge warn" style={{ marginLeft: 6 }}>{t("checkout.runMetersMissingLine")}</span>
                      )}
                    </div>
                  ) : l.kind === "cut-work" ? (
                    <div className="cl-sub">
                      {/* Материал в подписи есть не всегда (режут чужой) —
                          тогда и точку-разделитель не рисуем. */}
                      {t("checkout.rateWork")} {l.rate} × {l.runM} {t("checkout.pmShort")}
                      {l.materialName ? ` · ${l.materialName}` : ""}
                      {!(Number(l.runM) > 0) && (
                        <span className="badge warn" style={{ marginLeft: 6 }}>{t("checkout.runMetersMissingLine")}</span>
                      )}
                    </div>
                  ) : l.kind === "material-metre" ? (
                    // Рулон: показываем длину и ставку за метр — ширины тут нет
                    // намеренно, она в расчёт цены не входит.
                    <div className="cl-sub">
                      {l.length} {t("unit.METER")} · {l.price} {t("checkout.perPieceShort", { unit: t("unit.METER") })}
                      {l.rollName ? ` · ${t("checkout.fromRoll", { roll: l.rollName })}` : ""}
                    </div>
                  ) : l.kind === "material-area" ? (
                    <div className="cl-sub">{l.width}×{l.length} = {l.area} {t("unit.SQM")} · {l.price} {t("checkout.perPieceShort", { unit: t("unit.SQM") })}</div>
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
                {l.kind !== "cutting" && l.kind !== "material-area" && l.kind !== "material-metre" && l.kind !== "cut-work" && (
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
            <div className="field" style={{ minWidth: 120, flex: "0 0 auto", margin: 0 }}>
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

          {/* Сдача клиента с прошлых заказов. Деньги уже у цеха — незачем
              выдавать их из ящика, чтобы тут же принять обратно: галочка
              закрывает ими новый заказ, и «к оплате» сразу становится меньше.
              Снял галочку — сдача остаётся лежать, её выдают в «Чеках». */}
          {paymentMethod !== "ONLINE" && clientChange > 0 && total > 0 && (
            <div
              className="card"
              style={{ background: "var(--primary-soft)", padding: 10, marginTop: 10 }}
            >
              <label style={{ display: "flex", gap: 8, alignItems: "flex-start", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={useChange}
                  onChange={(e) => setUseChange(e.target.checked)}
                  style={{ width: 18, height: 18, marginTop: 2 }}
                />
                <span style={{ fontSize: 13 }}>
                  {t("checkout.hasChange", { sum: clientChange.toLocaleString("ru-RU") })}
                  <div className="muted" style={{ fontSize: 12 }}>
                    {useChange
                      ? t("checkout.changeApplied", {
                          sum: appliedChange.toLocaleString("ru-RU"),
                          left: toPay.toLocaleString("ru-RU"),
                        })
                      : t("checkout.changeKept")}
                  </div>
                </span>
              </label>
            </div>
          )}

          {/* Старый долг клиента — тут же, а не в другом разделе: он приходит
              за новым заказом и заодно отдаёт прошлое. Галочка ВЫКЛЮЧЕНА по
              умолчанию: это его деньги, и решает он. Долг принимает только
              админ — у складовщика такой галочки нет, как и в «Клиентах». */}
          {paymentMethod !== "ONLINE" && isAdmin && clientDebt > 0 && (
            <div
              className="card"
              style={{ background: "var(--warn-bg)", padding: 10, marginTop: 10 }}
            >
              <label style={{ display: "flex", gap: 8, alignItems: "flex-start", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={payDebt}
                  onChange={(e) => setPayDebt(e.target.checked)}
                  style={{ width: 18, height: 18, marginTop: 2 }}
                />
                <span style={{ fontSize: 13 }}>
                  {t("checkout.hasDebt", { sum: clientDebt.toLocaleString("ru-RU") })}
                  <div className="muted" style={{ fontSize: 12 }}>
                    {payDebt
                      ? `${t("checkout.debtWithOrder", { sum: cashNow.toLocaleString("ru-RU") })} ${t("checkout.payDebtHowTo")}`
                      : t("checkout.debtKept")}
                  </div>
                </span>
              </label>
            </div>
          )}

          {paymentMethod !== "ONLINE" && (
            <div className="field" style={{ marginTop: 10 }}>
              <label>{t("checkout.prepay")}</label>
              <div className="row" style={{ gap: 8, margin: 0 }}>
                <input
                  type="number"
                  min="0"
                  // С погашением долга «вся сумма» — это заказ и долг вместе:
                  // столько кассир и берёт из рук.
                  value={payFull ? String(owedNow.toFixed(0)) : prepay}
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
                {debtNow > 0 ? t("checkout.prepayWithDebtHint") : t("checkout.prepayHint")}
              </p>
              {/* Долг и сдача считаются от суммы ПОСЛЕ зачёта: заказ на 3 000
                  с зачтённой тысячей и внесёнными 2 000 — это оплаченный
                  заказ, а не долг в тысячу. С погашением долга — от заказа и
                  долга вместе: сначала закрывается заказ, остаток гасит долг
                  от старых к новым, и «долг» ниже — то, что останется висеть. */}
              {!payFull && Number(prepay || 0) < owedNow && (
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {t("receipts.debt")}:{" "}
                  <strong style={{ color: "var(--danger)" }}>
                    {(owedNow - Number(prepay || 0)).toFixed(0)} сом
                  </strong>
                </div>
              )}
              {/* Переплата больше не пропадает: она запоминается сдачей за
                  клиентом. Отдали на месте — нажать «Выдать» в Чеках, и она
                  спишется; не отдали (в кассе не было мелочи) — останется
                  видна, пока не отдадут. */}
              {!payFull && Number(prepay || 0) > owedNow && total > 0 && (
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {t("checkout.change")}:{" "}
                  <strong style={{ color: "var(--accent-strong)" }}>
                    {(Number(prepay) - owedNow).toFixed(0)} сом
                  </strong>
                  <div style={{ fontSize: 12 }}>{t("checkout.changeHint")}</div>
                </div>
              )}
            </div>
          )}

          {error && <div className="error">{error}</div>}
          {/* На кнопке — сумма ЗАКАЗА. Когда гасим ещё и долг, под ней строкой
              стоит то, что кассир реально берёт из рук: заказ после зачёта
              сдачи плюс долг. Складывать это в одно число на кнопке нельзя —
              «Оформить · 3 500» по заказу на 1 000 читается как ошибка. */}
          {/* Дата НЕ сбрасывается после продажи — это сделано намеренно, заказы
              задним числом заносят пачкой. Но тогда она обязана быть на виду:
              иначе следующая обычная продажа молча уходит в прошлый месяц, и
              «Финансы» её «теряют» — искать будут в отчёте, а причина в кассе. */}
          {isAdmin && orderDate && orderDate !== todayStr() && (
            <div
              className="error"
              style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8, justifyContent: "space-between" }}
            >
              <span>{t("checkout.backdatedWarn", { date: orderDate.split("-").reverse().join(".") })}</span>
              <button
                type="button"
                className="secondary"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
                onClick={() => setOrderDate(todayStr())}
              >
                {t("checkout.backdatedReset")}
              </button>
            </div>
          )}
          <button style={{ marginTop: 14, width: "100%", height: 52 }} onClick={submit} disabled={busy || !cart.length}>
            {busy
              ? t("common.loading")
              : `${t("checkout.submit")} · ${total.toFixed(0)} сом` +
                (isAdmin && orderDate && orderDate !== todayStr()
                  ? ` · ${orderDate.split("-").reverse().join(".")}`
                  : "")}
          </button>
          {debtNow > 0 && (
            <p className="muted" style={{ fontSize: 13, margin: "6px 0 0", textAlign: "center" }}>
              {t("checkout.takeNow")}: <strong>{cashNow.toLocaleString("ru-RU")} сом</strong>
            </p>
          )}
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
                  // Резку просят, а услуги нет — добавлять нечего.
                  cutNoService ||
                  cutRoll
                    // Рулон: нужна длина, цена за метр, чтобы её хватило на
                    // складе и чтобы ширина изделия не превышала рулон.
                    ? !(cutRollLen > 0) ||
                      !(cutRollRate > 0) ||
                      cutRollLeft < 0 ||
                      cutUsedWidth > cutFullWidth ||
                      // Резку включили, а длину реза не назвали — работа ушла
                      // бы в чек нулём. Сервер такую строку и не примет.
                      (cutRollWorkOn && !(cutRollRunM > 0))
                    : cutPiece
                    ? !(Number(cut.qty) > 0) || !(cutPieceUnit > 0) || cutRunMMissing
                    // Фигурный рез без длины кривой не добавляется: иначе работа
                    // уходит в чек нулём.
                    : !cutArea || (cut.service && !cut.materialId) || cutRunMMissing || cutRateMissing || cutPriceMissing
                }
                title={cutRunMMissing ? t("checkout.runMetersRequired") : undefined}
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
          {/* Вкладок у рулона нет: способ расчёта определяет ТОВАР, а не выбор
              мастера. Рулон продаётся длиной, и «квадратный метр» или «штучный»
              для него — не выбор, а ошибка. */}
          {isMatModal && !cutRoll && (
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

          {/* Услуги резки в каталоге нет — говорим об этом здесь, а не после
              оформления: без неё работа мастера в заказ не попадёт вовсе. */}
          {cutNoService && (
            <p style={{ color: "var(--danger)", fontSize: 13, margin: "0 0 10px" }}>
              {isAdmin ? t("checkout.noCutServiceAdmin") : t("checkout.noCutService")}
            </p>
          )}

          {/* Из какой ПАЧКИ берём лист — та же развилка, что «из какого рулона
              режем». Пачки отличаются ценой закупки, и себестоимость строки
              считается по выбранной. Одна пачка — просто подпись: выбирать не
              из чего, а знать, чем сейчас торгуют, полезно. */}
          {cutSheetLots.length > 0 && (
            <div className="field">
              <label>{t("checkout.sheetLotPick")}</label>
              {cutSheetLots.length === 1 ? (
                <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                  {sheetLotLabel(cutSheetLots[0], cutRollMat)}
                </p>
              ) : (
                <select
                  value={cutSheetLotPicked ? cutSheetLotPicked.id : ""}
                  onChange={(e) => setCut({ ...cut, lotId: e.target.value })}
                >
                  {cutSheetLots.map((r, i) => (
                    <option key={r.id} value={r.id}>
                      {sheetLotLabel(r, cutRollMat)}{i === 0 ? ` — ${t("checkout.rollFirst")}` : ""}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          {/* Станок: ЧПУ или лазер. Показываем, только когда станков правда
              несколько — если он один, спрашивать не о чем, и лишний ряд кнопок
              в кассе только мешает. Смена станка подставляет ЕГО ставку. */}
          {/* Сам отрез поперёк рулона в цене метра уже сидит — станок для него
              не спрашиваем. А контурная резка по рулону это отдельная работа:
              её включают галочкой ниже, и вот тогда станок нужен. */}
          {isMatModal && cuttingServices.length > 1 && (cutRoll ? cutRollWorkOn : CUT_MODES.includes(cut.mode) || cut.pieceCut) && (
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
                    {m.name} ({matSqm(m)} {t("checkout.perPieceShort", { unit: t("unit.SQM") })}, {t("checkout.stockLeft")} {m.quantity} {t("unit.SQM")}
                    {m.sheets_remaining != null ? ` ≈${Math.round(Number(m.sheets_remaining))} ${t("warehouse.sheetsShort")}` : ""})
                    {Number(m.quantity) <= 0 ? ` — ${t("checkout.outOfStock")}` : ""}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Штучно: количество, цена за штуку и (по желанию) рез */}
          {cutRoll ? (
            <>
              {/* Ширина — надпись, а не поле. Мастеру там нечего менять: рулон
                  режут поперёк на всю ширину, и 40 см ширины купить нельзя.
                  Раньше ширина была свободным числом и лезла в расчёт — в чек
                  уезжало «1250 × 2.1», где 2.1 это 1.5 × 1.4. */}
              <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                {t("checkout.rollWidthFixed", { width: cutFullWidth })}
              </p>
              {/* Из КАКОГО рулона режем. Один — просто пишем какой; несколько —
                  даём выбрать, но первым ставим початый: его и надо дожечь. */}
              {cutRolls.length > 0 && (
                <div className="field" style={{ marginBottom: 10 }}>
                  <label>{t("checkout.rollPick")}</label>
                  {cutRolls.length === 1 ? (
                    <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                      {rollLabel(cutRolls[0])}
                    </p>
                  ) : (
                    <select
                      value={cutRollPicked ? cutRollPicked.id : ""}
                      onChange={(e) => setCut({ ...cut, rollId: e.target.value })}
                    >
                      {cutRolls.map((r, i) => (
                        <option key={r.id} value={r.id}>
                          {rollLabel(r)}{i === 0 ? ` — ${t("checkout.rollFirst")}` : ""}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              )}
              {/* У рулона без цены за метр кнопка «Добавить» молча гасла, и
                  складовщик не понимал почему: цену правит только админ. */}
              {!(cutRollRate > 0) && (
                <p style={{ color: "var(--danger)", fontSize: 13, margin: "0 0 8px" }}>
                  {isAdmin ? t("checkout.rollNoPriceAdmin") : t("checkout.rollNoPrice")}
                </p>
              )}
              <div className="row">
                <div className="field grow" style={{ margin: 0 }}>
                  <label>{t("checkout.rollLength")}</label>
                  <input
                    type="number"
                    step="any"
                    value={cut.length}
                    onChange={(e) => setCut({ ...cut, length: e.target.value })}
                    autoFocus
                  />
                </div>
                {isAdmin && (
                  <div className="field grow" style={{ margin: 0 }}>
                    <label>{t("checkout.rollRate")}</label>
                    <input
                      type="number"
                      step="any"
                      value={cut.matPrice ?? ""}
                      onChange={(e) => setCut({ ...cut, matPrice: e.target.value, priceEdited: true })}
                    />
                  </div>
                )}
              </div>
              {/* Ширина изделия — необязательная. Заполнили меньше рулона —
                  система посчитает, сколько ушло в обрезок. Не заполнили —
                  считаем, что ушло всё, и ничего не выдумываем. */}
              <div className="field" style={{ marginTop: 10 }}>
                <label>{t("checkout.usedWidth")}</label>
                <input
                  type="number"
                  step="any"
                  value={cut.usedWidth ?? ""}
                  onChange={(e) => setCut({ ...cut, usedWidth: e.target.value })}
                  placeholder={String(cutFullWidth)}
                />
                <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
                  {t("checkout.usedWidthHint", { width: cutFullWidth })}
                </p>
              </div>
              {cutUsedWidth > cutFullWidth && (
                <p style={{ color: "var(--danger)", fontSize: 13, margin: "-4px 0 8px" }}>
                  {t("checkout.usedWidthTooWide", { width: cutFullWidth })}
                </p>
              )}

              {/* Резка по рулону — как «резать лист» у листового материала:
                  галочка, длина реза и станок. Отдельной строкой заказа, потому
                  что это отдельная работа: сам отрез поперёк в цене метра. */}
              <div className="field" style={{ marginTop: 10 }}>
                <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={!!cut.rollCut}
                    onChange={(e) => setCut({ ...cut, rollCut: e.target.checked })}
                    style={{ width: 18, height: 18 }}
                  />
                  {t("checkout.rollCutAdd")}
                </label>
              </div>
              {cutRollWorkOn && (
                <>
                  <div className="row">
                    <div className="field grow" style={{ margin: 0 }}>
                      <label>{t("checkout.runningMeters")}</label>
                      <input
                        type="number"
                        step="any"
                        value={cut.running_meters}
                        onChange={(e) => setCut({ ...cut, running_meters: e.target.value })}
                      />
                    </div>
                    {isAdmin && (
                      <div className="field grow" style={{ margin: 0 }}>
                        <label>{t("checkout.cutRateLabel")}</label>
                        <input
                          type="number"
                          step="any"
                          value={cut.cutRate ?? ""}
                          onChange={(e) => setCut({ ...cut, cutRate: e.target.value, cutRateEdited: true })}
                        />
                      </div>
                    )}
                  </div>
                  {/* Ставка 0 — работа за бесплатно. У листа об этом уже
                      предупреждают, рулон не исключение. */}
                  {!(cutRollWorkRate > 0) && (
                    <p style={{ color: "var(--danger)", fontSize: 13, margin: "4px 0 0" }}>
                      {isAdmin ? t("checkout.rateMissingAdmin") : t("checkout.rateMissing")}
                    </p>
                  )}
                </>
              )}
              {cutRollLen > 0 && cutRollLeft < 0 && (
                <p style={{ color: "var(--danger)", fontSize: 13, margin: "4px 0 0" }}>
                  {t("checkout.notEnough", {
                    name: cutRollMat.name,
                    need: cutRollLen,
                    left: cutRollMat.metres_remaining,
                    unit: t("unit.METER"),
                  })}
                </p>
              )}
            </>
          ) : cutPiece ? (
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
                    {cutRunMMissing && (
                      <p style={{ color: "var(--danger)", fontSize: 12, margin: "4px 0 0" }}>
                        {t("checkout.runMetersRequired")}
                      </p>
                    )}
                  </div>
                  {isAdmin && (
                    <div className="field">
                      <label>{t("checkout.cutRateLabel")}</label>
                      <input type="number" step="any" value={cut.cutRate ?? ""} onChange={(e) => setCut({ ...cut, cutRate: e.target.value, cutRateEdited: true })} />
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
                  <label>{t("checkout.runningMeters")} *</label>
                  <input
                    type="number"
                    step="any"
                    value={cut.running_meters}
                    onChange={(e) => setCut({ ...cut, running_meters: e.target.value })}
                  />
                  <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("checkout.runMetersHint")}</p>
                  {/* Пустое поле — не «работа бесплатно», а незаполненная
                      обязательная величина: об этом говорим красным и держим
                      кнопку «Добавить» закрытой. */}
                  {cutRunMMissing && (
                    <p style={{ color: "var(--danger)", fontSize: 12, margin: "4px 0 0" }}>
                      {t("checkout.runMetersRequired")}
                    </p>
                  )}
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
                    <input type="number" step="any" value={cut.matPrice ?? ""} onChange={(e) => setCut({ ...cut, matPrice: e.target.value, matPriceEdited: true })} />
                  </div>
                  {cutWorkOn && (
                    <div className="field grow" style={{ margin: 0 }}>
                      <label>{t("checkout.cutRateLabel")}</label>
                      <input type="number" step="any" value={cut.cutRate ?? ""} onChange={(e) => setCut({ ...cut, cutRate: e.target.value, cutRateEdited: true })} />
                    </div>
                  )}
                </div>
              )}
            </>
          )}

          {/* Live total */}
          {cutRoll && cutRollLen > 0 && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
              <div className="crow">
                <span className="k">{t("checkout.rateMaterial")}</span>
                <span>{cutRollRate} × {cutRollLen} = {cutRollTotal}</span>
              </div>
              {/* Работа реза — отдельной строкой, как она уйдёт и в чек. */}
              {cutRollWorkOn && (
                <div className="crow">
                  <span className="k">{t("checkout.rateWork")}</span>
                  <span>{cutRollWorkRate} × {cutRollRunM} = {cutRollWork}</span>
                </div>
              )}
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6 }}>
                <strong>{t("common.total")}</strong>
                <strong style={{ fontSize: 18 }}>{cutRollTotal + cutRollWork} сом</strong>
              </div>
              {/* Сколько ушло в отход — на виду в момент продажи, а не потом
                  в отчёте: здесь ещё можно передумать и отрезать иначе. */}
              {cutOffcut > 0 && (
                <div className="crow" style={{ paddingTop: 6 }}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {t("checkout.offcut", {
                      w: +(cutFullWidth - cutUsedWidth).toFixed(2),
                      len: cutRollLen,
                      area: cutOffcut,
                    })}
                  </span>
                </div>
              )}
              {cutRollMat.metres_remaining != null && (
                <div className="crow" style={{ paddingTop: 6 }}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {cutRollPicked
                      ? t("checkout.rollLeftAfterOne", {
                          roll: cutRollPicked.code || `№${cutRollPicked.id}`,
                          n: cutRollLeft.toFixed(2),
                        })
                      : t("checkout.rollLeftAfter", { n: cutRollLeft.toFixed(2) })}
                  </span>
                </div>
              )}
            </div>
          )}
          {!cutRoll && ((cutPiece && Number(cut.qty) > 0) || (!cutPiece && cutArea > 0 && cutMat)) && (
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
                  <div className="crow"><span className="k">{t("supply.area")}</span><strong>{cutArea} {t("unit.SQM")}</strong></div>
                  {/* Строку «Работа» показываем и при нулевой ставке — красным:
                      раньше она просто исчезала, и складовщик не видел, что
                      самая дорогая работа цеха уходит бесплатно. */}
                  {cutWorkOn && (
                    <div className="crow"><span className="k">{t("checkout.rateWork")}</span><span>{cutWorkRate} × {cutRunM} = {ceilSom(cutWork)}</span></div>
                  )}
                  {cutRateMissing && (
                    <p style={{ color: "var(--danger)", fontSize: 12, margin: "0 0 6px" }}>
                      {t(isAdmin ? "checkout.rateMissingAdmin" : "checkout.rateMissing")}
                    </p>
                  )}
                  <div className="crow"><span className="k">{t("checkout.rateMaterial")}</span><span>{cutMatSqm} × {cutArea} = {ceilSom(cutMaterialSum)}</span></div>
                  {cutPriceMissing && (
                    <p style={{ color: "var(--danger)", fontSize: 12, margin: "0 0 6px" }}>
                      {t(isAdmin ? "checkout.priceMissingAdmin" : "checkout.priceMissing")}
                    </p>
                  )}
                </>
              )}
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6 }}>
                <strong>{t("common.total")}</strong>
                <strong style={{ fontSize: 18 }}>{cutTotal.toFixed(0)} сом</strong>
              </div>
              {/* Нехватка остатка была видна только при отправке заказа: кассир
                  собирал весь чек, выбирал клиента и способ оплаты — и лишь
                  тогда получал «Недостаточно «Акрил 3мм»». Говорим сразу, в той
                  же строке, где посчитали площадь. */}
              {cutShort > 0 && (
                <div className="crow" style={{ paddingTop: 6 }}>
                  <span style={{ color: "var(--danger)", fontSize: 13 }}>
                    {t("checkout.notEnough", {
                      name: cutMat?.name,
                      need: cutNeed,
                      left: cutLeft,
                      unit: t("unit.SQM"),
                    })}
                  </span>
                </div>
              )}
            </div>
          )}
        </Modal>
      )}

      {receipt && (
        <Modal title={`${t("checkout.receipt")} №${receipt.order_number}`} onClose={() => setReceipt(null)}>
          {/* Строки — с единицей и ценой, суммы — целыми сомами, как в печатной
              форме и в списке чеков; «Акрил 3мм × 0.554» и «831.00 сом» ничего
              не объясняли. Ниже — что приняли, сдача и долг: это то, что
              кассир должен увидеть сразу после оформления. */}
          {receipt.items.map((it) => (
            <div className="crow" key={it.id}>
              <span>
                {it.type === "SERVICE" ? it.service_name : it.material_name}
                <span className="muted">
                  {" "}× {trimQty(it.quantity)} {it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || ""}
                  {" "}· {trimQty(it.price_per_item)} {t("checkout.perPieceShort", { unit: it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || "" })}
                </span>
              </span>
              <span>{somFmt(it.line_total)}</span>
            </div>
          ))}
          <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 8 }}>
            <strong>{t("common.total")}</strong><strong>{somFmt(receipt.total_price)}</strong>
          </div>
          {Number(receipt.amount_paid) > 0 && (
            <div className="crow"><span className="k">{t("print.paid")}</span><span>{somFmt(receipt.amount_paid)}</span></div>
          )}
          {/* Отдельной строкой: иначе «оплачено 3 000» по заказу, за который
              принесли 2 000, выглядит как ошибка кассы. */}
          {Number(receipt.change_applied) > 0 && (
            <div className="crow">
              <span className="k">{t("checkout.changeUsed")}</span>
              <span>{somFmt(receipt.change_applied)}</span>
            </div>
          )}
          {/* Долг закрыт вместе с заказом — это отдельные деньги, и кассир
              должен видеть, что они приняты (или что принять не вышло). */}
          {Number(receipt.debt_paid) > 0 && (
            <div className="crow">
              <span className="k">{t("checkout.debtPaid")}</span>
              <strong>{somFmt(receipt.debt_paid)}</strong>
            </div>
          )}
          {receipt.debt_error && (
            <p className="muted" style={{ fontSize: 12, color: "var(--danger)" }}>
              {receipt.debt_error}
            </p>
          )}
          {Number(receipt.change_due) > 0 && (
            <div className="crow">
              <span className="k">{t("checkout.change")}</span>
              <strong style={{ color: "var(--accent-strong)" }}>{somFmt(receipt.change_due)}</strong>
            </div>
          )}
          {Number(receipt.debt) > 0 && (
            <div className="crow">
              <span className="k">{t("receipts.debt")}</span>
              <strong style={{ color: "var(--danger)" }}>{somFmt(receipt.debt)}</strong>
            </div>
          )}
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

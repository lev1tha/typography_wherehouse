// Черновик корзины кассы — в localStorage (волна 2, XL-12).
//
// Корзина жила только в useState: переход в «Чеки» посмотреть заказ клиента или
// случайный F5 стирали собранный чек. Потом — в sessionStorage, и она
// переживала уход с экрана и перезагрузку, но не закрытие вкладки и не
// пропавшую связь (браузер на телефоне выгружает вкладку). Теперь — в
// localStorage: собранный заказ дожидается связи и следующего открытия кассы.
// Явный выход его стирает (`AuthContext.logout`), чужой черновик касса не берёт
// (поле `user`). Оформлять заказ без связи нельзя — это только корзина.
//
// Любое обращение к хранилищу — в try/catch: в приватном окне, при заблокированных
// данных сайта и при переполнении оно бросает исключение, а касса обязана
// работать и без черновика. Ключ версионирован: смени форму данных — подними
// версию, и старые черновики будут просто проигнорированы, а не развалят корзину.
export const DRAFT_KEY = "chpu.checkoutDraft.v1";

function parse(raw) {
  if (!raw) return null;
  const data = JSON.parse(raw);
  if (!data || typeof data !== "object" || !Array.isArray(data.cart)) return null;
  return data;
}

export function saveCheckoutDraft(data) {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(data));
  } catch {
    /* нет хранилища — работаем без черновика */
  }
}

export function loadCheckoutDraft() {
  try {
    const stored = parse(localStorage.getItem(DRAFT_KEY));
    if (stored) return stored;
    // Черновик прежней версии кассы (sessionStorage, тот же ключ) — переносим.
    const legacy = parse(sessionStorage.getItem(DRAFT_KEY));
    if (legacy) {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(legacy));
      sessionStorage.removeItem(DRAFT_KEY);
    }
    return legacy;
  } catch {
    return null; // битый JSON и всё прочее — как будто черновика нет
  }
}

export function clearCheckoutDraft() {
  try {
    localStorage.removeItem(DRAFT_KEY);
  } catch {
    /* см. выше */
  }
  try {
    sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    /* см. выше */
  }
}

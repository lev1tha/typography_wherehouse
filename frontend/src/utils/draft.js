// Черновик корзины кассы в sessionStorage.
//
// Корзина жила только в useState: переход в «Чеки» посмотреть заказ клиента или
// случайный F5 стирали собранный чек. Теперь она переживает уход с экрана,
// перезагрузку и принудительный выход по истёкшей сессии (вкладка та же).
//
// Любое обращение к хранилищу — в try/catch: в приватном окне, при заблокированных
// данных сайта и при переполнении оно бросает исключение, а касса обязана
// работать и без черновика. Ключ версионирован: смени форму данных — подними
// версию, и старые черновики будут просто проигнорированы, а не развалят корзину.
export const DRAFT_KEY = "chpu.checkoutDraft.v1";

export function saveCheckoutDraft(data) {
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(data));
  } catch {
    /* нет хранилища — работаем без черновика */
  }
}

export function loadCheckoutDraft() {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object" || !Array.isArray(data.cart)) return null;
    return data;
  } catch {
    return null; // битый JSON и всё прочее — как будто черновика нет
  }
}

export function clearCheckoutDraft() {
  try {
    sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    /* см. выше */
  }
}

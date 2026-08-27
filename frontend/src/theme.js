// Тема интерфейса: «как в системе» (по умолчанию), светлая, тёмная.
//
// Выбор хранится в localStorage и применяется атрибутом на <html>. Три
// состояния, а не галочка: у большинства система уже переключает тему по
// расписанию, и навязывать ей своё значение по умолчанию неправильно. При этом
// ручной выбор всегда сильнее системного — цех работает при своём свете.
const KEY = "cloude-theme";
export const THEMES = ["system", "light", "dark"];

export function readTheme() {
  try {
    const saved = localStorage.getItem(KEY);
    return THEMES.includes(saved) ? saved : "system";
  } catch {
    // Приватный режим и заблокированные куки: тема не должна ронять вход.
    return "system";
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* не сохранилось — тема останется до перезагрузки, это не повод падать */
  }
}

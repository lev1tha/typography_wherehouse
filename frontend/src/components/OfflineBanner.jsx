import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

// Баннер «нет связи» (волна 2, XL-12). Касса на телефоне в цеху теряет сеть —
// раньше это выглядело как «кнопка не нажимается». Теперь сверху честно
// написано, что связи нет: собранная корзина сохранится (localStorage), а
// оформить заказ можно будет, когда связь вернётся. Офлайн-оформления нет.
export default function OfflineBanner() {
  const { t } = useTranslation();
  const [offline, setOffline] = useState(() => typeof navigator !== "undefined" && navigator.onLine === false);

  useEffect(() => {
    const on = () => setOffline(false);
    const off = () => setOffline(true);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);

  if (!offline) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "sticky", top: 0, zIndex: 1000, padding: "8px 16px", textAlign: "center",
        background: "var(--surface)", color: "var(--warn-ink)", fontSize: 14, fontWeight: 600,
        borderBottom: "2px solid var(--warn-ink)",
      }}
    >
      {t("offline.banner")}
    </div>
  );
}

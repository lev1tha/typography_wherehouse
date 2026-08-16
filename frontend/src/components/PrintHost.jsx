import { createPortal } from "react-dom";

// Окно печатной формы живёт НЕ внутри приложения, а порталом прямо в <body>.
//
// В `@media print` (index.css) всё приложение — `.shell` с меню и шапкой —
// прячется целиком, на бумагу должен уходить только `.print-sheet`. Пока окно
// печати рендерилось внутри `.shell` (внутри страницы, из которой его
// открыли), оно пряталось вместе с ним: предпросмотр на экране был, а из
// принтера выходил пустой лист. Портал выносит окно из `.shell`, и правило
// «спрятать приложение, оставить лист» начинает делать ровно то, что обещает.
export default function PrintHost({ children }) {
  return createPortal(<div className="modal-backdrop print-host">{children}</div>, document.body);
}

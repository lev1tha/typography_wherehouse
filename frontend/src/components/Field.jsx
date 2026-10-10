import { cloneElement, isValidElement, useId } from "react";

// Поле формы: подпись, подсказка, ошибка — и всё это связано с самим полем.
//
// Раньше в формах стояло `<label>…</label><input/>` без связи: подпись не
// кликалась, экранный диктор читал поле без имени, а ошибка валидации падала
// тостом на 3 секунды и пропадала, не показав, КАКОЕ поле неверно.
//
// Что делает компонент:
//   * `<label htmlFor>` ↔ `id` поля (через useId — id не пересекаются);
//   * `aria-invalid` и `aria-describedby` на поле, когда есть ошибка/подсказка;
//   * `aria-required` и звёздочка у обязательных (нативный `required` не ставим:
//     он включил бы браузерную проверку там, где формы сохраняются по кнопке);
//   * ошибка — под полем, `role="alert"`, не гаснет, пока её не поправят.
//
// Использование:
//   <Field label="Телефон" required error={errors.phone}>
//     <input value={…} onChange={…} />
//   </Field>
// Если внутри не один элемент-поле (поле + кнопка в одну строку), дети — функция:
//   <Field label="Сумма">{(p) => <><input {...p} /><button>…</button></>}</Field>
export default function Field({
  label,
  error,
  hint,
  required = false,
  optional = false,
  optionalLabel,
  className = "",
  style,
  children,
}) {
  const uid = useId();
  const id = `f${uid.replace(/:/g, "")}`;
  const errId = `${id}-err`;
  const hintId = `${id}-hint`;
  const describedBy = [error ? errId : null, hint ? hintId : null].filter(Boolean).join(" ") || undefined;

  const aria = {
    id,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": describedBy,
    "aria-required": required ? true : undefined,
  };

  let control;
  if (typeof children === "function") control = children(aria);
  else if (isValidElement(children)) {
    // Свои aria-/id у ребёнка не затираем.
    control = cloneElement(children, {
      id: children.props.id || id,
      "aria-invalid": children.props["aria-invalid"] ?? aria["aria-invalid"],
      "aria-describedby": children.props["aria-describedby"] ?? aria["aria-describedby"],
      "aria-required": children.props["aria-required"] ?? aria["aria-required"],
    });
  } else control = children;

  const forId = isValidElement(children) ? children.props.id || id : id;

  return (
    <div className={`field${error ? " has-error" : ""}${className ? ` ${className}` : ""}`} style={style}>
      {label != null && (
        <label htmlFor={forId}>
          {label}
          {required && (
            <span className="req" aria-hidden="true">
              {" "}
              *
            </span>
          )}
          {optional && <span className="muted"> — {optionalLabel}</span>}
        </label>
      )}
      {control}
      {hint && (
        <p className="field-hint" id={hintId}>
          {hint}
        </p>
      )}
      {error && (
        <p className="field-error" id={errId} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

// Фокус на первое неверное поле — после того как React отрисовал ошибки.
// Ищем в верхнем открытом окне, а если окон нет — на странице.
export function focusFirstInvalid() {
  const run = () => {
    const dialogs = document.querySelectorAll('[role="dialog"]');
    const root = dialogs.length ? dialogs[dialogs.length - 1] : document;
    const el = root.querySelector('[aria-invalid="true"]');
    if (el && typeof el.focus === "function") {
      el.focus();
      el.scrollIntoView?.({ block: "center", behavior: "smooth" });
    }
  };
  // Два кадра: первый — React применит состояние, второй — браузер разметит.
  requestAnimationFrame(() => requestAnimationFrame(run));
}

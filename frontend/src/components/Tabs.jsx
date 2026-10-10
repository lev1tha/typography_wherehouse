// Вкладки с нормальной семантикой: role=tablist/tab, aria-selected, стрелки
// влево/вправо и Home/End, фокус только на активной вкладке (roving tabindex).
// Раньше вкладки были просто рядом стоящими кнопками с классом «active», и
// экранный диктор не знал ни что это вкладки, ни какая из них выбрана.
//
//   <Tabs id="receipts" label="…" tabs={[{ key, label, disabled? }]} value={tab} onChange={setTab} />
//   <div {...tabPanel("receipts", tab)}> …содержимое… </div>
//
// `className` дописывается к «tabs» (например, «tabs-grid»).
export const tabId = (id, key) => `${id}-tab-${key}`;
export const panelId = (id) => `${id}-panel`;
export const tabPanel = (id, key) => ({
  role: "tabpanel",
  id: panelId(id),
  "aria-labelledby": tabId(id, key),
});

export default function Tabs({ id, label, tabs, value, onChange, className = "", style, panel = true }) {
  const enabled = tabs.filter((x) => !x.disabled);

  function onKeyDown(e) {
    const i = enabled.findIndex((x) => x.key === value);
    let next = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = enabled[(i + 1) % enabled.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = enabled[(i - 1 + enabled.length) % enabled.length];
    else if (e.key === "Home") next = enabled[0];
    else if (e.key === "End") next = enabled[enabled.length - 1];
    if (!next) return;
    e.preventDefault();
    onChange(next.key);
    // Фокус следует за выбранной вкладкой после перерисовки.
    requestAnimationFrame(() => document.getElementById(tabId(id, next.key))?.focus());
  }

  return (
    <div className={`tabs ${className}`.trim()} style={style} role="tablist" aria-label={label} onKeyDown={onKeyDown}>
      {tabs.map((x) => (
        <button
          key={x.key}
          type="button"
          role="tab"
          id={tabId(id, x.key)}
          aria-selected={value === x.key}
          aria-controls={panel ? panelId(id) : undefined}
          tabIndex={value === x.key ? 0 : -1}
          className={value === x.key ? "active" : ""}
          disabled={x.disabled}
          onClick={() => onChange(x.key)}
        >
          {x.label}
        </button>
      ))}
    </div>
  );
}

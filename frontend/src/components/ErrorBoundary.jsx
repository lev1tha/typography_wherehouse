import { Component } from "react";

import i18n from "../i18n";

// Предохранитель: исключение при отрисовке одного экрана больше не белит всё
// приложение. Раньше любая опечатка в данных (null там, где ждали объект)
// роняла React-дерево целиком — белая страница без единого слова.
//
// Текст берём прямо из i18n (а не хуком): класс не может пользоваться хуками, а
// корневой предохранитель стоит снаружи всех провайдеров. Страничный
// предохранитель получает `key={pathname}` — переход на другой экран его
// сбрасывает, и одна сломанная страница не запирает остальные.
export default class ErrorBoundary extends Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary:", error, info?.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const t = (k) => i18n.t(k);
    return (
      <div className="boundary" role="alert">
        <h1>{t("boundary.title")}</h1>
        <p className="muted">{t("boundary.text")}</p>
        <div className="row">
          <button type="button" onClick={() => window.location.reload()}>
            {t("boundary.reload")}
          </button>
          {this.props.onHome && (
            <button type="button" className="secondary" onClick={this.props.onHome}>
              {t("boundary.home")}
            </button>
          )}
        </div>
      </div>
    );
  }
}

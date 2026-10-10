// «Последний запрос побеждает».
//
// Поиск и фильтры шлют запрос на каждое нажатие; ответы приходят не по
// порядку, и медленный ответ на «ас» мог перетереть быстрый ответ на «асан».
// `useLatest()` отдаёт функцию, которая при каждом вызове отменяет предыдущий
// запрос и возвращает сигнал для нового; на размонтировании отменяется всё.
//
//   const next = useLatest();
//   api.get(url, { params, signal: next() }).then(...).catch((e) => { if (isCanceled(e)) return; ... });
import axios from "axios";
import { useCallback, useEffect, useRef } from "react";

export const isCanceled = (e) => axios.isCancel(e) || e?.code === "ERR_CANCELED";

export function useLatest() {
  const ref = useRef(null);
  useEffect(
    () => () => {
      ref.current?.abort();
    },
    []
  );
  return useCallback(() => {
    ref.current?.abort();
    const c = new AbortController();
    ref.current = c;
    return c.signal;
  }, []);
}

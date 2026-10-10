import { useTranslation } from "react-i18next";

const PAYMENT_VARIANT = {
  PAID: "ok",
  PENDING: "amber",
  REFUNDED: "red",
  PARTIALLY_REFUNDED: "amber",
};

const FULFILLMENT_VARIANT = {
  PROCESSING: "amber",
  READY: "blue",
  // Часть позиций отдали, остальное ещё дорезают — заказ открыт, как и «Готовится».
  PARTIALLY_ISSUED: "amber",
  ISSUED: "ok",
};

export function PaymentBadge({ status }) {
  const { t } = useTranslation();
  return (
    <span className={`badge dot ${PAYMENT_VARIANT[status] || ""}`}>
      {t(`payment.${status}`)}
    </span>
  );
}

export function FulfillmentBadge({ status }) {
  const { t } = useTranslation();
  return (
    <span className={`badge dot ${FULFILLMENT_VARIANT[status] || ""}`}>
      {t(`fulfillment.${status}`)}
    </span>
  );
}

// Гарантийная переделка: заказ за счёт цеха, цена строк в нём 0. Метка стоит
// рядом с номером, чтобы его не принимали за обычный заказ с нулевой суммой.
export function WarrantyBadge() {
  const { t } = useTranslation();
  return <span className="badge red">{t("warranty.badge")}</span>;
}

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

import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";

/**
 * Material photo gallery. Swipe-friendly horizontal strip (touch scroll-snap).
 * When `manage` is true (admin), allows upload / delete / set-primary.
 */
export default function GalleryModal({ material, onClose, onChanged, manage = false }) {
  const { t } = useTranslation();
  const [images, setImages] = useState(material.images || []);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef();

  async function refresh() {
    const { data } = await api.get(`/warehouse/materials/${material.id}/`);
    setImages(data.images || []);
    onChanged?.();
  }

  async function upload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    const form = new FormData();
    form.append("material", material.id);
    form.append("image", file);
    form.append("is_primary", images.length === 0 ? "true" : "false");
    try {
      await api.post("/warehouse/material-images/", form, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      await refresh();
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function setPrimary(id) {
    await api.patch(`/warehouse/material-images/${id}/`, { is_primary: true });
    await refresh();
  }

  async function remove(id) {
    await api.delete(`/warehouse/material-images/${id}/`);
    await refresh();
  }

  return (
    <Modal title={`${material.name} — ${t("warehouse.gallery")}`} onClose={onClose}>
      {images.length ? (
        <div className="gallery">
          {images.map((img) => (
            <img key={img.id} src={img.image} alt={material.name} />
          ))}
        </div>
      ) : (
        <p className="muted">{t("common.empty")}</p>
      )}

      {manage && (
        <div style={{ marginTop: 14 }}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {images.map((img) => (
              <div key={img.id} style={{ textAlign: "center" }}>
                <img className="thumb" src={img.image} alt="" />
                <div className="row" style={{ gap: 4, marginTop: 4 }}>
                  {!img.is_primary && (
                    <button className="ghost" onClick={() => setPrimary(img.id)} aria-label={t("warehouse.primary")}>
                      <Icon name="star" size={17} />
                    </button>
                  )}
                  {img.is_primary && <span className="badge ok">{t("warehouse.primary")}</span>}
                  <button className="ghost" onClick={() => remove(img.id)} aria-label={t("common.delete")}>
                    <Icon name="trash" size={17} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            onChange={upload}
            disabled={busy}
            style={{ marginTop: 12, width: "100%" }}
          />
        </div>
      )}
    </Modal>
  );
}

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import DataTable from "../../components/DataTable.jsx";
import Field, { focusFirstInvalid } from "../../components/Field.jsx";
import Icon from "../../components/Icon.jsx";
import LoadError from "../../components/LoadError.jsx";
import Modal from "../../components/Modal.jsx";
import Tabs, { tabPanel } from "../../components/Tabs.jsx";
import { useUI } from "../../components/UIProvider.jsx";

// Экран «Сотрудники» (аудит STAFF-11): учётные записи и справочник сотрудников
// цеха. Раньше заводить людей можно было только в django-admin.
//
// Два разных понятия, и экран их не смешивает:
//   · учётная запись — ВХОД в систему (логин, пароль, роль). Не удаляется,
//     а отключается: чеки и журнал хранят автора;
//   · сотрудник — человек, которому считают зарплату (ФИО, должность, станок).
//     Учётки у мастера может не быть вовсе (общие логины «Чпу», «Лазер»).

const ROLES = ["ADMIN", "STOREKEEPER", "ACCOUNTANT"];
const MACHINES = ["CNC", "LASER"];

function UsersTab() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState(null);   // {id?, username, ...} — форма учётки
  const [passwordFor, setPasswordFor] = useState(null);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);

  function load() {
    api.get("/staff/users/")
      .then((r) => { setRows(r.data.results || r.data); setFailed(false); })
      .catch((e) => { setFailed(true); toast(apiError(e, t("common.loadFailed")), "error"); });
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, []);

  function startNew() {
    setErrors({});
    setEditing({ username: "", password: "", role: "STOREKEEPER", first_name: "", last_name: "" });
  }

  async function save() {
    const next = {};
    if (!editing.username.trim()) next.username = t("staff.needLogin");
    if (!editing.id && !editing.password) next.password = t("staff.needPassword");
    setErrors(next);
    if (Object.keys(next).length) return focusFirstInvalid();
    setBusy(true);
    try {
      const body = {
        username: editing.username.trim(), role: editing.role,
        first_name: editing.first_name, last_name: editing.last_name,
      };
      if (editing.id) await api.patch(`/staff/users/${editing.id}/`, body);
      else await api.post("/staff/users/", { ...body, password: editing.password });
      setEditing(null);
      load();
      toast(t("common.saved"));
    } catch (e) {
      const data = e.response?.data;
      if (data && typeof data === "object" && (data.username || data.password)) {
        setErrors({ username: [].concat(data.username || []).join(" "), password: [].concat(data.password || []).join(" ") });
        focusFirstInvalid();
      } else toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive(row) {
    if (row.is_active && !(await confirm(t("staff.confirmDisable", { name: row.username })))) return;
    try {
      await api.patch(`/staff/users/${row.id}/`, { is_active: !row.is_active });
      load();
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function savePassword() {
    if (!passwordFor.password) {
      setErrors({ password: t("staff.needPassword") });
      return focusFirstInvalid();
    }
    if (passwordFor.password !== passwordFor.again) {
      setErrors({ again: t("staff.passwordsDiffer") });
      return focusFirstInvalid();
    }
    setErrors({});
    setBusy(true);
    try {
      await api.post(`/staff/users/${passwordFor.id}/set-password/`, { password: passwordFor.password });
      setPasswordFor(null);
      toast(t("staff.passwordChanged"));
    } catch (e) {
      const msg = e.response?.data?.password;
      if (msg) {
        setErrors({ password: [].concat(msg).join(" ") });
        focusFirstInvalid();
      } else toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  const columns = [
    { key: "username", label: t("common.username"), render: (r) => <strong>{r.username}</strong> },
    {
      key: "name", label: t("staff.fullName"),
      render: (r) => [r.first_name, r.last_name].filter(Boolean).join(" ") || <span className="muted">—</span>,
    },
    { key: "role", label: t("staff.role"), render: (r) => t(`roles.${r.role.toLowerCase()}`) },
    {
      key: "status", label: t("staff.status"),
      render: (r) => (r.is_active
        ? <span className="badge ok">{t("staff.active")}</span>
        : <span className="badge red">{t("staff.disabled")}</span>),
    },
    {
      key: "actions", label: "",
      render: (r) => (
        <span className="row" style={{ gap: 4, margin: 0, flexWrap: "wrap" }}>
          <button className="ghost row-btn" onClick={() => { setErrors({}); setEditing({ ...r, password: "" }); }}>
            <Icon name="pencil" size={14} /> {t("common.edit")}
          </button>
          <button className="ghost row-btn" onClick={() => { setErrors({}); setPasswordFor({ id: r.id, username: r.username, password: "", again: "" }); }}>
            <Icon name="key" size={14} /> {t("staff.setPassword")}
          </button>
          <button className={`ghost row-btn${r.is_active ? " row-danger" : ""}`} onClick={() => toggleActive(r)}>
            {r.is_active ? t("staff.disable") : t("staff.enable")}
          </button>
        </span>
      ),
    },
  ];

  if (failed && !rows.length) return <LoadError onRetry={load} />;

  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0, maxWidth: "70ch" }}>{t("staff.usersHint")}</p>
      <div className="row" style={{ margin: "8px 0 12px" }}>
        <button onClick={startNew}>+ {t("staff.addUser")}</button>
      </div>
      <DataTable columns={columns} rows={rows} rowClass={(r) => (r.is_active ? "" : "row-muted")} />

      {editing && (
        <Modal
          title={editing.id ? t("staff.editUser") : t("staff.addUser")}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setEditing(null)}>{t("common.cancel")}</button>
              <button onClick={save} disabled={busy}>{t("common.save")}</button>
            </>
          }
        >
          <div className="row">
            <Field className="grow" label={t("common.username")} required error={errors.username}>
              <input value={editing.username} autoFocus autoComplete="off"
                onChange={(e) => setEditing({ ...editing, username: e.target.value })} />
            </Field>
            <Field className="grow" label={t("staff.role")}>
              <select value={editing.role} onChange={(e) => setEditing({ ...editing, role: e.target.value })}>
                {ROLES.map((r) => <option key={r} value={r}>{t(`roles.${r.toLowerCase()}`)}</option>)}
              </select>
            </Field>
          </div>
          <div className="row">
            <Field className="grow" label={t("staff.firstName")}>
              <input value={editing.first_name || ""} onChange={(e) => setEditing({ ...editing, first_name: e.target.value })} />
            </Field>
            <Field className="grow" label={t("staff.lastName")}>
              <input value={editing.last_name || ""} onChange={(e) => setEditing({ ...editing, last_name: e.target.value })} />
            </Field>
          </div>
          {!editing.id && (
            <Field label={t("common.password")} required error={errors.password} hint={t("staff.passwordHint")}>
              <input type="password" autoComplete="new-password" value={editing.password}
                onChange={(e) => setEditing({ ...editing, password: e.target.value })} />
            </Field>
          )}
          <p className="muted" style={{ fontSize: 12 }}>{t(`staff.roleHint_${editing.role}`)}</p>
        </Modal>
      )}

      {passwordFor && (
        <Modal
          title={t("staff.passwordFor", { name: passwordFor.username })}
          onClose={() => setPasswordFor(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setPasswordFor(null)}>{t("common.cancel")}</button>
              <button onClick={savePassword} disabled={busy}>{t("common.save")}</button>
            </>
          }
        >
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("staff.passwordNote")}</p>
          <Field label={t("staff.newPassword")} required error={errors.password} hint={t("staff.passwordHint")}>
            <input type="password" autoFocus autoComplete="new-password" value={passwordFor.password}
              onChange={(e) => setPasswordFor({ ...passwordFor, password: e.target.value })} />
          </Field>
          <Field label={t("staff.repeatPassword")} required error={errors.again}>
            <input type="password" autoComplete="new-password" value={passwordFor.again}
              onChange={(e) => setPasswordFor({ ...passwordFor, again: e.target.value })} />
          </Field>
        </Modal>
      )}
    </>
  );
}

function EmployeesTab() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [users, setUsers] = useState([]);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState(null);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);

  function load() {
    api.get("/staff/employees/")
      .then((r) => { setRows(r.data.results || r.data); setFailed(false); })
      .catch((e) => { setFailed(true); toast(apiError(e, t("common.loadFailed")), "error"); });
    api.get("/staff/users/").then((r) => setUsers(r.data.results || r.data)).catch(() => {});
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, []);

  const taken = new Set(rows.filter((r) => r.user && r.id !== editing?.id).map((r) => r.user));
  const freeUsers = users.filter((u) => !taken.has(u.id));

  function startNew() {
    setErrors({});
    setEditing({ full_name: "", position: "", default_machine: "", user: "", is_active: true, note: "" });
  }

  async function save() {
    if (!editing.full_name.trim()) {
      setErrors({ full_name: t("staff.needFullName") });
      return focusFirstInvalid();
    }
    setErrors({});
    setBusy(true);
    const body = {
      full_name: editing.full_name.trim(), position: editing.position,
      default_machine: editing.default_machine, user: editing.user ? Number(editing.user) : null,
      is_active: editing.is_active, note: editing.note || "",
    };
    try {
      if (editing.id) await api.patch(`/staff/employees/${editing.id}/`, body);
      else await api.post("/staff/employees/", body);
      setEditing(null);
      load();
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive(row) {
    if (row.is_active && !(await confirm(t("staff.confirmFire", { name: row.full_name })))) return;
    try {
      await api.patch(`/staff/employees/${row.id}/`, { is_active: !row.is_active });
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const columns = [
    { key: "full_name", label: t("staff.fullName"), render: (r) => <strong>{r.full_name}</strong> },
    { key: "position", label: t("staff.position"), render: (r) => r.position || <span className="muted">—</span> },
    {
      key: "machine", label: t("staff.machine"),
      render: (r) => (r.default_machine ? <span className="chip">{t(`staff.machine_${r.default_machine}`)}</span> : <span className="muted">—</span>),
    },
    { key: "user", label: t("staff.account"), render: (r) => r.user_username || <span className="muted">{t("staff.noAccount")}</span> },
    {
      key: "status", label: t("staff.status"),
      render: (r) => (r.is_active
        ? <span className="badge ok">{t("staff.works")}</span>
        : <span className="badge">{t("staff.fired")}</span>),
    },
    {
      key: "actions", label: "",
      render: (r) => (
        <span className="row" style={{ gap: 4, margin: 0, flexWrap: "wrap" }}>
          <button className="ghost row-btn" onClick={() => { setErrors({}); setEditing({ ...r, user: r.user || "" }); }}>
            <Icon name="pencil" size={14} /> {t("common.edit")}
          </button>
          <button className={`ghost row-btn${r.is_active ? " row-danger" : ""}`} onClick={() => toggleActive(r)}>
            {r.is_active ? t("staff.fire") : t("staff.rehire")}
          </button>
        </span>
      ),
    },
  ];

  if (failed && !rows.length) return <LoadError onRetry={load} />;

  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0, maxWidth: "70ch" }}>{t("staff.employeesHint")}</p>
      <div className="row" style={{ margin: "8px 0 12px", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button onClick={startNew}>+ {t("staff.addEmployee")}</button>
        <Link to="/admin/payroll" className="btn-link">{t("staff.toPayroll")} <Icon name="arrow-right" size={16} /></Link>
      </div>
      <DataTable columns={columns} rows={rows} rowClass={(r) => (r.is_active ? "" : "row-muted")} />

      {editing && (
        <Modal
          title={editing.id ? t("staff.editEmployee") : t("staff.addEmployee")}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setEditing(null)}>{t("common.cancel")}</button>
              <button onClick={save} disabled={busy}>{t("common.save")}</button>
            </>
          }
        >
          <Field label={t("staff.fullName")} required error={errors.full_name}>
            <input value={editing.full_name} autoFocus
              onChange={(e) => setEditing({ ...editing, full_name: e.target.value })} />
          </Field>
          <div className="row">
            <Field className="grow" label={t("staff.position")}>
              <input value={editing.position || ""} placeholder={t("staff.positionPh")}
                onChange={(e) => setEditing({ ...editing, position: e.target.value })} />
            </Field>
            <Field className="grow" label={t("staff.machine")} hint={t("staff.machineHint")}>
              <select value={editing.default_machine || ""}
                onChange={(e) => setEditing({ ...editing, default_machine: e.target.value })}>
                <option value="">{t("staff.noMachine")}</option>
                {MACHINES.map((m) => <option key={m} value={m}>{t(`staff.machine_${m}`)}</option>)}
              </select>
            </Field>
          </div>
          <Field label={t("staff.account")} hint={t("staff.accountHint")}>
            <select value={editing.user || ""} onChange={(e) => setEditing({ ...editing, user: e.target.value })}>
              <option value="">{t("staff.noAccount")}</option>
              {freeUsers.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
            </select>
          </Field>
          <Field label={t("expenses.note")}>
            <input value={editing.note || ""} onChange={(e) => setEditing({ ...editing, note: e.target.value })} />
          </Field>
          <label className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input type="checkbox" style={{ width: 20, height: 20, minHeight: 0 }} checked={!!editing.is_active}
              onChange={(e) => setEditing({ ...editing, is_active: e.target.checked })} />
            {t("staff.works")}
          </label>
        </Modal>
      )}
    </>
  );
}

export default function Staff() {
  const { t } = useTranslation();
  const [tab, setTab] = useState("users");
  return (
    <>
      <h1>{t("staff.title")}</h1>
      <Tabs
        id="staff"
        label={t("staff.title")}
        value={tab}
        onChange={setTab}
        tabs={[
          { key: "users", label: t("staff.tabUsers") },
          { key: "employees", label: t("staff.tabEmployees") },
        ]}
      />
      <div {...tabPanel("staff", tab)}>
        {tab === "users" ? <UsersTab /> : <EmployeesTab />}
      </div>
    </>
  );
}

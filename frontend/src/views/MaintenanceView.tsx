// Maintenance journal view.
//
// Layout:
//   - "Service status" panel: each scheduled item with ok/soon/overdue chip,
//     hours-to-next, days-to-next, "Mark done" button.
//   - "Recent log" table: every completed entry with run-hours stamp.
//   - "Schedule" editor (admin): edit intervals + add custom items.

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { Card, EmptyState, Icon, Pill, Skeleton } from "../components/primitives";
import type {
  MaintenanceDue,
  MaintenanceLogEntry,
  MaintenanceSchedule,
  MaintenanceStatus,
} from "../types";
import { relTime } from "./LiveView";

export function MaintenanceView() {
  const [due, setDue] = useState<{
    currentRunHours: number | null;
    items: MaintenanceDue[];
  } | null>(null);
  const [log, setLog] = useState<MaintenanceLogEntry[]>([]);
  const [schedule, setSchedule] = useState<MaintenanceSchedule[]>([]);
  const [loading, setLoading] = useState(true);
  const [marking, setMarking] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      const [d, l, s] = await Promise.all([
        api.maintenance.due(),
        api.maintenance.log(undefined, 100),
        api.maintenance.schedule(),
      ]);
      setDue(d);
      setLog(l.log);
      setSchedule(s.schedule);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const markDone = async (kind: string) => {
    const reason = window.prompt(`Notes for ${kind} (filter brand, oil weight, etc.) — optional:`, "");
    if (reason === null) return;
    setMarking(kind);
    try {
      await api.maintenance.addLog({
        kind,
        run_hours_at: null,
        notes: reason,
      });
      await refresh();
    } catch (e: any) {
      window.alert(
        e instanceof ApiError ? `Failed to record: HTTP ${e.status}` : `Failed: ${e?.message}`,
      );
    } finally {
      setMarking(null);
    }
  };

  const overdueCount = due?.items.filter((d) => d.status === "overdue").length ?? 0;
  const soonCount = due?.items.filter((d) => d.status === "soon").length ?? 0;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-eyebrow">Service log · {schedule.length} scheduled items</div>
          <h1 className="page-title">Maintenance</h1>
          <div className="page-sub">
            {due?.currentRunHours != null
              ? <>Current run hours: <span className="mono">{due.currentRunHours.toFixed(1)} h</span></>
              : "Run hours not yet polled"}
            {overdueCount > 0 && (
              <> · <span style={{ color: "var(--red)" }}>{overdueCount} overdue</span></>
            )}
            {soonCount > 0 && (
              <> · <span style={{ color: "var(--amber)" }}>{soonCount} due soon</span></>
            )}
          </div>
        </div>
        <div className="flex ai-c gap-8">
          <button className="btn btn-ghost" onClick={refresh}>
            <Icon name="refresh" size={14} /> Refresh
          </button>
        </div>
      </div>

      <Card title="Service status" sub="From maintenance_schedule + most-recent log entry" flush>
        {loading && !due ? (
          <MaintLoadingSkeleton />
        ) : !due || due.items.length === 0 ? (
          <EmptyState
            icon="bookmark"
            title="No scheduled items"
            desc="Default schedule will be seeded on first boot. Reload the service or add items below."
          />
        ) : (
          <table className="reg-table">
            <thead>
              <tr>
                <th>Item</th>
                <th>Status</th>
                <th>Interval</th>
                <th>Last performed</th>
                <th>Until next</th>
                <th style={{ textAlign: "right" }}>Action</th>
              </tr>
            </thead>
            <tbody>
              {due.items.map((d) => (
                <tr key={d.kind}>
                  <td className="mono">{d.kind}</td>
                  <td>
                    <Pill tone={statusTone(d.status)}>{statusLabel(d.status)}</Pill>
                  </td>
                  <td className="mono" style={{ color: "var(--text-3)" }}>
                    {intervalSummary(d.intervalHours, d.intervalDays)}
                  </td>
                  <td className="mono">
                    {d.lastTs == null
                      ? "—"
                      : (
                        <>
                          {relTime(d.lastTs)}
                          {d.lastRunHours != null && (
                            <span style={{ color: "var(--text-3)" }}> · {d.lastRunHours.toFixed(0)} h</span>
                          )}
                        </>
                      )}
                  </td>
                  <td className="mono">{untilNextSummary(d)}</td>
                  <td style={{ textAlign: "right" }}>
                    <button
                      className="btn btn-ghost"
                      disabled={marking === d.kind}
                      onClick={() => markDone(d.kind)}
                    >
                      <Icon name="check" size={14} />
                      {marking === d.kind ? " Saving…" : " Mark done"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <div style={{ marginTop: "var(--gap)" }}>
        <Card title="Recent log entries" sub="Append-only — service history forever" flush>
          {log.length === 0 ? (
            <EmptyState
              icon="bookmark"
              title="No log entries yet"
              desc="Mark a scheduled item as done above and it'll appear here."
            />
          ) : (
            <table className="reg-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Item</th>
                  <th>Run hours</th>
                  <th>Performed by</th>
                  <th>Notes</th>
                  <th style={{ textAlign: "right" }}>Cost</th>
                </tr>
              </thead>
              <tbody>
                {log.map((e) => (
                  <tr key={e.id}>
                    <td className="mono">{relTime(e.ts)}</td>
                    <td className="mono">{e.kind}</td>
                    <td className="mono">{e.runHoursAt != null ? `${e.runHoursAt.toFixed(0)} h` : "—"}</td>
                    <td>{e.performedBy || "—"}</td>
                    <td style={{ color: "var(--text-2)" }}>{e.notes || "—"}</td>
                    <td className="mono" style={{ textAlign: "right" }}>
                      {e.costUsd != null ? `$${e.costUsd.toFixed(2)}` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <div style={{ marginTop: "var(--gap)" }}>
        <ScheduleEditor schedule={schedule} onChanged={refresh} />
      </div>
    </>
  );
}

function statusLabel(s: MaintenanceStatus): string {
  return s === "ok" ? "OK"
    : s === "soon" ? "Due soon"
    : s === "overdue" ? "Overdue"
    : "Never recorded";
}

function statusTone(s: MaintenanceStatus): "ok" | "warn" | "alarm" | "info" {
  return s === "ok" ? "ok" : s === "soon" ? "warn" : "alarm";
}

function intervalSummary(h: number, d: number): string {
  const parts: string[] = [];
  if (h > 0) parts.push(`${h} h`);
  if (d > 0) parts.push(`${d} d`);
  return parts.join(" / ") || "—";
}

function untilNextSummary(d: MaintenanceDue): string {
  if (d.status === "never") return "—";
  const parts: string[] = [];
  if (d.hoursToNext != null) parts.push(`${d.hoursToNext.toFixed(0)} h`);
  if (d.daysToNext != null) parts.push(`${d.daysToNext.toFixed(0)} d`);
  return parts.join(" · ") || "—";
}

function ScheduleEditor({
  schedule,
  onChanged,
}: {
  schedule: MaintenanceSchedule[];
  onChanged: () => Promise<void>;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ kind: string; hours: number; days: number; notes: string; enabled: boolean }>({
    kind: "", hours: 0, days: 0, notes: "", enabled: true,
  });

  const begin = (s?: MaintenanceSchedule) => {
    setEditing(s?.kind ?? "_new");
    setDraft({
      kind: s?.kind ?? "",
      hours: s?.intervalHours ?? 0,
      days: s?.intervalDays ?? 0,
      notes: s?.notes ?? "",
      enabled: s?.enabled ?? true,
    });
  };

  const save = async () => {
    if (!draft.kind) return;
    try {
      await api.maintenance.upsertSchedule({
        kind: draft.kind,
        interval_hours: draft.hours,
        interval_days: draft.days,
        notes: draft.notes,
        enabled: draft.enabled,
      });
      setEditing(null);
      await onChanged();
    } catch (e: any) {
      window.alert(
        e instanceof ApiError ? `Save failed: HTTP ${e.status}` : `Save failed: ${e?.message}`,
      );
    }
  };

  const remove = async (kind: string) => {
    if (!window.confirm(`Remove scheduled item "${kind}"?`)) return;
    try {
      await api.maintenance.deleteSchedule(kind);
      await onChanged();
    } catch (e: any) {
      window.alert(
        e instanceof ApiError ? `Delete failed: HTTP ${e.status}` : `Delete failed: ${e?.message}`,
      );
    }
  };

  return (
    <Card
      title="Schedule"
      sub="Hours or days — whichever trips first. Admin only."
      actions={
        editing == null && (
          <button className="btn btn-primary" onClick={() => begin()}>
            <Icon name="plus" size={14} /> New item
          </button>
        )
      }
      flush
    >
      <table className="reg-table">
        <thead>
          <tr>
            <th>Kind</th>
            <th>Hours</th>
            <th>Days</th>
            <th>Enabled</th>
            <th>Notes</th>
            <th style={{ textAlign: "right" }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {schedule.map((s) => (
            <tr key={s.kind}>
              <td className="mono">{s.kind}</td>
              <td className="mono">{s.intervalHours || "—"}</td>
              <td className="mono">{s.intervalDays || "—"}</td>
              <td>
                <Pill tone={s.enabled ? "ok" : "info"}>{s.enabled ? "on" : "off"}</Pill>
              </td>
              <td style={{ color: "var(--text-2)" }}>{s.notes || "—"}</td>
              <td style={{ textAlign: "right" }}>
                <button className="btn btn-ghost" onClick={() => begin(s)}>
                  Edit
                </button>{" "}
                <button className="btn btn-danger" onClick={() => remove(s.kind)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {editing != null && (
            <tr>
              <td>
                <input
                  className="input"
                  value={draft.kind}
                  placeholder="e.g. air_filter"
                  onChange={(e) => setDraft((d) => ({ ...d, kind: e.target.value }))}
                  disabled={editing !== "_new"}
                />
              </td>
              <td>
                <input
                  className="input"
                  type="number"
                  value={draft.hours}
                  onChange={(e) => setDraft((d) => ({ ...d, hours: Number(e.target.value) }))}
                />
              </td>
              <td>
                <input
                  className="input"
                  type="number"
                  value={draft.days}
                  onChange={(e) => setDraft((d) => ({ ...d, days: Number(e.target.value) }))}
                />
              </td>
              <td>
                <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={draft.enabled}
                    onChange={(e) => setDraft((d) => ({ ...d, enabled: e.target.checked }))}
                  />
                  enabled
                </label>
              </td>
              <td>
                <input
                  className="input"
                  value={draft.notes}
                  onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))}
                />
              </td>
              <td style={{ textAlign: "right" }}>
                <button className="btn btn-ghost" onClick={() => setEditing(null)}>
                  Cancel
                </button>{" "}
                <button className="btn btn-primary" onClick={save}>
                  Save
                </button>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </Card>
  );
}

function MaintLoadingSkeleton() {
  return (
    <div style={{ padding: 14 }}>
      {Array.from({ length: 6 }).map((_, i) => (
        <div
          key={i}
          style={{ display: "grid", gridTemplateColumns: "1fr 80px 100px 1fr 100px 100px", gap: 12, padding: "10px 0" }}
        >
          <Skeleton width="70%" height={14} />
          <Skeleton width={60} height={14} />
          <Skeleton width={80} height={14} />
          <Skeleton width="60%" height={14} />
          <Skeleton width={80} height={14} />
          <Skeleton width={80} height={14} />
        </div>
      ))}
    </div>
  );
}

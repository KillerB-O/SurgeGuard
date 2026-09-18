import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { addAlertRecipient, removeAlertRecipient, ApiError } from "@/lib/api";
import { useAlertHistory, useAlertRecipients } from "../lib/queries";
import { formatClock } from "../lib/format";
import { Empty } from "../components/States";
import type { AlertStatus, SurgeRiskLevel } from "@/lib/types";

/**
 * Severity floors an operator can assign, most to least selective.
 *
 * The point of the choice is that one firing should not mail everybody: the
 * operational head can take CRITICAL only while a floor lead takes HIGH and
 * up. LOW is offered but labelled honestly -- it means every rule, including
 * the routine ones.
 */
const LEVELS: { value: SurgeRiskLevel; label: string }[] = [
  { value: "CRITICAL", label: "Critical only" },
  { value: "HIGH", label: "High and above" },
  { value: "MEDIUM", label: "Medium and above" },
  { value: "LOW", label: "Everything" },
];

const STATUS_CLASS: Record<AlertStatus, string> = {
  SENT: "badge safe",
  PENDING: "badge watch",
  FAILED: "badge breached",
};

export function Alerts() {
  const recipients = useAlertRecipients();
  const history = useAlertHistory();
  const queryClient = useQueryClient();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [minLevel, setMinLevel] = useState<SurgeRiskLevel>("HIGH");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["alert-recipients"] });
    queryClient.invalidateQueries({ queryKey: ["alert-history"] });
  };

  const add = useMutation({
    mutationFn: () => addAlertRecipient(name.trim(), email.trim(), minLevel),
    onSuccess: () => {
      setName("");
      setEmail("");
      setError(null);
      refresh();
    },
    onError: (err) => {
      // A duplicate address is the one failure an operator can actually fix,
      // so it is named rather than collapsed into "something went wrong".
      setError(
        err instanceof ApiError && err.status === 409
          ? "That address is already on the list."
          : "Could not add that recipient.",
      );
    },
  });

  const remove = useMutation({
    mutationFn: removeAlertRecipient,
    onSuccess: refresh,
  });

  const canSubmit =
    name.trim().length > 0 && email.trim().includes("@") && !add.isPending;

  return (
    <div className="page">
      <header className="page-head">
        <h1>Alerts</h1>
        <p style={{ color: "var(--ink-muted)", maxWidth: "60ch" }}>
          Who gets told when a rule fires, and whether the message actually
          reached them. Alerts are raised against the same schedule this
          dashboard shows, so an alert never claims something these screens
          disagree with.
        </p>
      </header>

      <section className="panel">
        <div className="panel-title">
          <h2>Recipients</h2>
        </div>

        <form
          className="alert-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (canSubmit) add.mutate();
          }}
        >
          <label>
            <span>Name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Priya Raghavan"
              required
            />
          </label>
          <label>
            <span>Email</span>
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="ops.head@example.com"
              required
            />
          </label>
          <label>
            <span>Tell them about</span>
            <select
              value={minLevel}
              onChange={(event) =>
                setMinLevel(event.target.value as SurgeRiskLevel)
              }
            >
              {LEVELS.map((level) => (
                <option key={level.value} value={level.value}>
                  {level.label}
                </option>
              ))}
            </select>
          </label>
          <button className="btn" type="submit" disabled={!canSubmit}>
            {add.isPending ? "Adding…" : "Add recipient"}
          </button>
        </form>

        {error && (
          <p role="alert" style={{ color: "var(--breached)" }}>
            {error}
          </p>
        )}

        {recipients.data && recipients.data.recipients.length === 0 ? (
          <Empty title="Nobody is on the alert list yet" />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Tell them about</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {recipients.data?.recipients.map((recipient) => (
                <tr key={recipient.id}>
                  <td>{recipient.name}</td>
                  <td>{recipient.email}</td>
                  <td>
                    {LEVELS.find((l) => l.value === recipient.min_level)
                      ?.label ?? recipient.min_level}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <button
                      className="btn secondary"
                      disabled={remove.isPending}
                      onClick={() => remove.mutate(recipient.id)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <div className="panel-title">
          <h2>Recent alerts</h2>
        </div>
        <p style={{ color: "var(--ink-muted)", marginTop: 0, maxWidth: "60ch" }}>
          Every alert is written down before it is sent, so a message that
          never arrived shows up here with the reason rather than only in a
          log. A failed alert is not retried again.
        </p>

        {history.data && history.data.alerts.length === 0 ? (
          <Empty title="No alerts have been raised yet" />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Raised</th>
                <th>Alert</th>
                <th>To</th>
                <th>Delivery</th>
              </tr>
            </thead>
            <tbody>
              {history.data?.alerts.map((alert) => (
                <tr key={alert.id}>
                  <td>{formatClock(alert.decided_at)}</td>
                  <td>{alert.subject}</td>
                  <td>
                    {alert.recipient_name ?? (
                      <span style={{ color: "var(--ink-muted)" }}>removed</span>
                    )}
                  </td>
                  <td>
                    <span className={STATUS_CLASS[alert.status]}>
                      {alert.status}
                    </span>
                    {alert.last_error && (
                      <div
                        style={{ color: "var(--ink-muted)", fontSize: "0.8125rem" }}
                      >
                        {alert.last_error} (after {alert.attempts} attempts)
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

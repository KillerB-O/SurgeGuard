import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

/** Empty and failure screens explain what happened and what to do next. */

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="empty" role="status">
      {label}…
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function Failed({ error }: { error: unknown }) {
  const apiError = error instanceof ApiError ? error : null;

  if (apiError?.status === 0) {
    return (
      <div className="notice error">
        Cannot reach the backend. Start it with{" "}
        <code>uvicorn app.main:app --reload</code>, or set
        <code> VITE_DATA_SOURCE=mock</code> to work against fixtures.
      </div>
    );
  }

  if (apiError?.status === 503) {
    return (
      <div className="notice warn">
        The database is unavailable. Polling continues, so this clears on its own
        once PostgreSQL is back.
      </div>
    );
  }

  if (apiError?.status === 404) {
    return <div className="notice warn">{apiError.message}</div>;
  }

  return (
    <div className="notice error">
      {apiError?.message ?? "Something went wrong. Retrying on the next poll."}
    </div>
  );
}

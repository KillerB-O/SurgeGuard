import type {
  ActionStatus,
  OrderStatus,
  SLAStatus,
  SurgeRiskLevel,
  VerificationStatus,
} from "@/lib/types";
import { titleCase } from "../lib/format";

/**
 * Badges only colour a value the backend already decided. The frontend never
 * classifies SLA or risk itself (doc 06 section 9).
 */

export function SlaBadge({ status }: { status: SLAStatus | null }) {
  if (!status) return <span style={{ color: "var(--muted)" }}>not queued</span>;
  return <span className={`badge ${status.toLowerCase()}`}>{titleCase(status)}</span>;
}

export function RiskBadge({ level }: { level: SurgeRiskLevel }) {
  return <span className={`badge ${level.toLowerCase()}`}>{titleCase(level)}</span>;
}

export function ActionBadge({ status }: { status: ActionStatus }) {
  return <span className={`badge ${status.toLowerCase()}`}>{titleCase(status)}</span>;
}

/**
 * Whether a capacity claim actually showed up in measured throughput (P6).
 * UNVERIFIED and NOT_OBSERVED are the ones that matter most -- see
 * index.css, they are deliberately not styled as quiet neutral grey.
 */
export function VerificationBadge({ status }: { status: VerificationStatus }) {
  return <span className={`badge ${status.toLowerCase()}`}>{titleCase(status)}</span>;
}

export function StatusBadge({ status }: { status: OrderStatus }) {
  return <span className="badge neutral">{titleCase(status)}</span>;
}

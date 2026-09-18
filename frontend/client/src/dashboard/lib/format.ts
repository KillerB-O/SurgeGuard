/** Display helpers. No operational maths lives here — that is the backend's job. */

/**
 * The backend emits UTC (`datetime.now(UTC)`), while the team and the demo run
 * in IST. Render in one explicit zone everywhere so promised and predicted
 * timestamps are never compared across different clocks.
 */
const TIME_ZONE = "Asia/Kolkata";

const dateTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: TIME_ZONE,
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const clock = new Intl.DateTimeFormat("en-IN", {
  timeZone: TIME_ZONE,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export const TIME_ZONE_LABEL = "IST";

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  return dateTime.format(new Date(iso));
}

export function formatClock(iso: string | null): string {
  if (!iso) return "—";
  return clock.format(new Date(iso));
}

/** Signed hours between two instants, for slack against a promise. */
export function hoursBetween(from: string, to: string): number {
  return (new Date(to).getTime() - new Date(from).getTime()) / 3_600_000;
}

export function formatHours(hours: number | null): string {
  if (hours === null || Number.isNaN(hours)) return "—";
  const sign = hours < 0 ? "-" : "";
  const abs = Math.abs(hours);
  if (abs < 1) return `${sign}${Math.round(abs * 60)}m`;
  return `${sign}${abs.toFixed(1)}h`;
}

export function formatNumber(value: number, digits = 0): string {
  return value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** `order_value` arrives as a JSON string because it is a Decimal server-side. */
export function formatCurrency(value: string): string {
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return value;
  return `₹${parsed.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function titleCase(value: string): string {
  return value
    .toLowerCase()
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function signed(value: number, digits = 0): string {
  if (value === 0) return "no change";
  const formatted = formatNumber(Math.abs(value), digits);
  return `${value > 0 ? "+" : "-"}${formatted}`;
}

/**
 * A span of minutes as prose: "45m", "3h 20m", "1d 4h".
 *
 * Takes a magnitude -- whether the span is before or after now is the caller's
 * to say in words, because "-122 minutes to first breach" reads as a countdown
 * when it means the deadline is already two hours gone.
 */
export function formatDuration(minutes: number): string {
  const total = Math.round(Math.abs(minutes));
  if (total < 60) return `${total}m`;

  const hours = Math.floor(total / 60);
  const remainingMinutes = total % 60;
  if (hours < 24) {
    return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
  }

  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  return remainingHours ? `${days}d ${remainingHours}h` : `${days}d`;
}

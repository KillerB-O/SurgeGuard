import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "@/contexts/AuthContext";

import { FACILITY_ID, USING_MOCKS, getMockScenario, setMockScenario } from "@/lib/api";
import { POLL_MS, useDashboard } from "../lib/queries";
import { TIME_ZONE_LABEL, formatClock, formatNumber } from "../lib/format";
import { RiskBadge } from "./Badges";
import { LimelightNav, NavItem } from "./LimelightNav";

/* Inline SVG icons so we don't depend on lucide-react loading. */
const IconCommand = (p: React.SVGProps<SVGSVGElement>) => (
  <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <rect x="14" y="14" width="7" height="7" rx="1" />
  </svg>
);
const IconOrders = (p: React.SVGProps<SVGSVGElement>) => (
  <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="M16 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8Z" />
    <path d="M15 3v4a2 2 0 0 0 2 2h4" />
  </svg>
);
const IconAlert = (p: React.SVGProps<SVGSVGElement>) => (
  <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
    <path d="M12 9v4" /><path d="M12 17h.01" />
  </svg>
);
const IconWhatIf = (p: React.SVGProps<SVGSVGElement>) => (
  <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5" />
    <path d="M9 18h6" /><path d="M10 22h4" />
  </svg>
);
const IconRecovery = (p: React.SVGProps<SVGSVGElement>) => (
  <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" />
    <path d="M21 3v5h-5" />
  </svg>
);
const IconActions = (p: React.SVGProps<SVGSVGElement>) => (
  <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
    <polyline points="22 4 12 14.01 9 11.01" />
  </svg>
);

const IconBell = (p: React.SVGProps<SVGSVGElement>) => (
  <svg {...p} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
    <path d="M13.73 21a2 2 0 0 1-3.46 0" />
  </svg>
);

const NAV_ROUTES = [
  { path: "/dashboard",          label: "Command centre", icon: <IconCommand /> },
  { path: "/dashboard/orders",   label: "Orders",          icon: <IconOrders /> },
  { path: "/dashboard/at-risk",  label: "At risk",         icon: <IconAlert /> },
  { path: "/dashboard/what-if",  label: "What-if",         icon: <IconWhatIf /> },
  { path: "/dashboard/recovery", label: "Recovery",        icon: <IconRecovery /> },
  { path: "/dashboard/actions",  label: "Actions",         icon: <IconActions /> },
  { path: "/dashboard/alerts",   label: "Alerts",          icon: <IconBell /> },
];

export function AppShell() {
  const { data, isError } = useDashboard();
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [signingOut, setSigningOut] = useState(false);
  const queryClient = useQueryClient();
  // Mirrors the module-level scenario so the button label re-renders on
  // toggle without relying on an unrelated query refetch to do it.
  const [mockScenario, setMockScenarioState] = useState(getMockScenario);

  const displayName = user?.full_name || user?.email || "Signed in";
  const initials = (user?.full_name || user?.email || "?")
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join("");

  const signOut = async () => {
    setSigningOut(true);
    await logout("/");
  };

  const gap = data
    ? data.demand_work_units_per_hour - data.fulfillment_work_units_per_hour
    : null;

  const atRiskTotal = data
    ? data.sla_counts.at_risk + data.sla_counts.breached
    : 0;

  // Exact match for the command centre: every other tab also starts with
  // /dashboard, so a prefix match would light it up on every page.
  const pathname = location.pathname.replace(/\/+$/, "");
  const currentIndex = NAV_ROUTES.findIndex((r) =>
    r.path === "/dashboard"
      ? pathname === "/dashboard"
      : pathname === r.path || pathname.startsWith(`${r.path}/`),
  );

  const navItems: NavItem[] = NAV_ROUTES.map((route) => ({
    id: route.path,
    icon: route.icon,
    label: route.label,
    badge:
      route.path === "/dashboard/at-risk" && atRiskTotal > 0
        ? atRiskTotal
        : null,
  }));

  return (
    <div className="sg-dashboard">
    <div className="shell">
      {/* ── Sidebar ──────────────────────────────────────────── */}
      <nav className="rail" aria-label="Sections">
        <div className="rail-logo">
          <div className="logo-title">SurgeGuard</div>
          <div className="logo-subtitle">Fulfillment control</div>
        </div>

        <LimelightNav
          items={navItems}
          activeIndex={currentIndex >= 0 ? currentIndex : 0}
          onTabChange={(index) => navigate(NAV_ROUTES[index].path)}
        />

        <div className="rail-foot">
          {USING_MOCKS ? (
            <>
              <div className="foot-label">Fixture data</div>
              <button
                className="rail-foot-btn"
                title={`Switch to ${mockScenario === "surge" ? "normal" : "surge"}`}
                onClick={() => {
                  const next = mockScenario === "surge" ? "healthy" : "surge";
                  setMockScenario(next);
                  setMockScenarioState(next);
                  // The mock reads happen live from the module-level scenario,
                  // so a refetch is enough; a full reload used to also wipe
                  // that scenario back to "surge" before it could be read.
                  queryClient.invalidateQueries();
                }}
              >
                {mockScenario === "surge" ? "Show normal day" : "Show surge day"}
              </button>
            </>
          ) : (
            <>
              <div className="rail-account">
                <span className="rail-avatar" aria-hidden="true">{initials}</span>
                <span className="rail-account-text">
                  <span className="rail-account-name">{displayName}</span>
                  {user?.full_name && <span className="rail-account-email">{user.email}</span>}
                </span>
              </div>
              <button className="rail-foot-btn" type="button" onClick={signOut} disabled={signingOut}>
                {signingOut ? "Signing out..." : "Sign out"}
              </button>
            </>
          )}
        </div>
      </nav>

      {/* ── Main ─────────────────────────────────────────────── */}
      <div className="main">
        {isError && (
          <div className="banner">
            Backend unreachable. Showing the last successful read.
          </div>
        )}

        <header className="strip">
          <div className="strip-item">
            <span className="label">Facility</span>
            <span className="value">{data?.facility_id ?? FACILITY_ID}</span>
          </div>

          <div className="strip-item">
            <span className="label">Demand</span>
            <span className="value num">
              {data ? `${formatNumber(data.demand_work_units_per_hour)} wu/hr` : "—"}
            </span>
          </div>

          <div className="strip-item">
            <span className="label">Throughput</span>
            <span className="value num">
              {data
                ? `${formatNumber(data.fulfillment_work_units_per_hour)} wu/hr`
                : "—"}
              {data?.throughput_stalled ? (
                <span
                  style={{ color: "var(--breached)", marginLeft: 6 }}
                  title="Orders are waiting and the floor has not moved work in the last hour"
                >
                  stalled
                </span>
              ) : data?.throughput_source === "configured" ? (
                <span
                  style={{ color: "var(--ink-muted)", marginLeft: 6 }}
                  title="No live telemetry: this is the facility's configured capacity, not a measurement"
                >
                  est.
                </span>
              ) : null}
            </span>
          </div>

          <div className="strip-item">
            <span className="label">Gap</span>
            <span
              key={gap === null ? "none" : Math.round(gap)}
              className="value num ticked"
              style={{
                color:
                  gap !== null && gap > 0 ? "var(--at-risk)" : "var(--safe)",
              }}
            >
              {gap === null
                ? "—"
                : `${gap > 0 ? "+" : ""}${formatNumber(gap)}`}
            </span>
          </div>

          <div className="strip-item strip-spacer">
            <span className="label">Risk</span>
            <span key={data?.risk_level ?? "none"} className="value ticked">
              {data ? <RiskBadge level={data.risk_level} /> : "—"}
            </span>
          </div>

          <div className="strip-item">
            <span className="label">Updated</span>
            <span className="value num" style={{ color: "var(--ink-muted)", display: "flex", alignItems: "center", gap: 7 }}>
              <span className={`live-dot${data ? ` ${data.risk_level.toLowerCase()}` : ""}`} aria-hidden="true" />
              {data ? `${formatClock(data.generated_at)} ${TIME_ZONE_LABEL}` : "—"}
            </span>
          </div>
        </header>

        <main className="content">
          <Outlet />
        </main>

        <footer className="dash-foot">
          <span className="dash-foot-status">
            <span className={`live-dot${data ? ` ${data.risk_level.toLowerCase()}` : ""}`} aria-hidden="true" />
            {data ? `Live · refreshing every ${Math.round(POLL_MS / 1000)}s` : "Reconnecting…"}
          </span>
          <span>SurgeGuard · {data?.facility_id ?? FACILITY_ID}</span>
        </footer>
      </div>
    </div>
    </div>
  );
}

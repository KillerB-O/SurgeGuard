import { Navigate, Outlet, useLocation } from "react-router-dom";

import { USING_MOCKS } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { BrandMark } from "@/components/brand";

/**
 * Keeps every /dashboard route behind a live session.
 *
 * Two things this deliberately does NOT do:
 *
 * 1. It never checks `verified`. Email verification is informational -- an
 *    account that has never clicked its link is a normal, usable account, and
 *    gating on it would strand people behind a mail server.
 * 2. It does nothing at all in explicit mock mode. That opt-in exists for
 *    isolated UI work with fixtures and has no backend session to validate.
 */
export function ProtectedDashboard() {
  const location = useLocation();
  const { user, loading, unreachable } = useAuth();

  if (USING_MOCKS) return <Outlet />;

  // Deliberately blank: this resolves against the local backend in ~50ms, and
  // a spinner flashing on every load is more distracting than nothing.
  if (loading) return null;

  if (unreachable && !user) {
    // A different problem from being signed out; bouncing to the sign-in form
    // would misdescribe it.
    return (
      <div className="not-found">
        <BrandMark />
        <h1>Cannot reach SurgeGuard.</h1>
        <p className="auth-intro">The backend is not answering. Check it is running, then reload.</p>
        <button type="button" className="button button-green" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/auth" replace state={{ from: location.pathname + location.search }} />;
  }

  return <Outlet />;
}

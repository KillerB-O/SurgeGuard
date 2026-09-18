import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ArrowRight, Eye, EyeOff } from "lucide-react";

import { AuthField, BrandMark, MetricCard, useAmbientCanvas } from "@/components/brand";
import { PasswordChecklist, useAllRulesMet } from "@/components/PasswordChecklist";
import { useAuth } from "@/contexts/AuthContext";
import { ApiError, login, resendVerification, signup as createAccount } from "@/lib/api";

/** Where to go after signing in: the page that bounced here, else the dashboard. */
function destinationFrom(state: unknown): string {
  const from = (state as { from?: unknown } | null)?.from;
  // Only same-app dashboard paths; never follow an arbitrary value off history state.
  return typeof from === "string" && from.startsWith("/dashboard") ? from : "/dashboard";
}

export function AuthPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, loading, refreshUser } = useAuth();
  const isSignup = location.pathname === "/signup";

  const [showPassword, setShowPassword] = useState(false);
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [notice, setNotice] = useState<{ tone: "info" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // Login refuses unverified accounts with a 403; offer the fix right there.
  const [needsVerification, setNeedsVerification] = useState(false);
  const [resending, setResending] = useState(false);
  const rulesMet = useAllRulesMet(password);
  useAmbientCanvas("auth-canvas");

  // Already signed in: this page has nothing to offer.
  if (!loading && user) return <Navigate to={destinationFrom(location.state)} replace />;

  const switchMode = () => {
    setNotice(null);
    setNeedsVerification(false);
    setPassword("");
    // Keep the bounce-back target when flipping between the two forms.
    navigate(isSignup ? "/auth" : "/signup", { replace: true, state: location.state });
  };

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const data = new FormData(event.currentTarget);
    setNotice(null);
    setNeedsVerification(false);

    if (isSignup) {
      if (!rulesMet) {
        setNotice({ tone: "error", text: "Your password does not meet every requirement yet." });
        return;
      }
      if (password !== String(data.get("confirmPassword") ?? "")) {
        setNotice({ tone: "error", text: "Passwords do not match. Please check both fields." });
        return;
      }
    }

    setBusy(true);
    try {
      if (isSignup) {
        await createAccount(email, password, {
          full_name: String(data.get("fullName") ?? "").trim(),
          company: String(data.get("company") ?? "").trim(),
          role: String(data.get("role") ?? "").trim(),
        });
        setPassword("");
        navigate("/auth", { replace: true, state: location.state });
        setNotice({
          tone: "info",
          text: "Account created. Open the verification link we just emailed you, then sign in here.",
        });
      } else {
        await login(email, password, data.get("rememberMe") !== null);
        await refreshUser();
        navigate(destinationFrom(location.state), { replace: true });
      }
    } catch (error) {
      if (!isSignup && error instanceof ApiError && error.status === 403) setNeedsVerification(true);
      setNotice({
        tone: "error",
        text: error instanceof ApiError ? error.message : "Something went wrong. Please try again.",
      });
    } finally {
      setBusy(false);
    }
  };

  const resend = async () => {
    if (!email || resending) return;
    setResending(true);
    try {
      await resendVerification(email);
      // The backend answers the same for any address, so say no more than that.
      setNotice({ tone: "info", text: "If this account still needs verifying, a fresh link is on its way to your inbox." });
      setNeedsVerification(false);
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof ApiError ? error.message : "Something went wrong. Please try again." });
    } finally {
      setResending(false);
    }
  };

  const passwordToggle = (
    <button
      type="button"
      className="icon-button"
      aria-label={showPassword ? "Hide password" : "Show password"}
      onClick={() => setShowPassword((value) => !value)}
    >
      {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
    </button>
  );

  return (
    <div className="auth-shell">
      <canvas id="auth-canvas" className="ambient-canvas" />
      <div className="grain-layer" />
      <Link className="auth-brand" to="/"><BrandMark /></Link>
      <main className="auth-layout">
        <section className="auth-form-column">
          <div className="auth-form-wrap">
            <div className="eyebrow"><span className="eyebrow-dot" /> {isSignup ? "Create secure access" : "Secure operator access"}</div>
            <h1>
              {isSignup
                ? <>Create your operator account.<br /><span>See the surge sooner.</span></>
                : <>Welcome back.<br /><span>Stay ahead of the surge.</span></>}
            </h1>
            <p className="auth-intro">
              {isSignup
                ? "Set up your access to monitor capacity, demand, and fulfillment health in real time."
                : "Access your fulfillment command centre and keep every promise on track."}
            </p>

            <form onSubmit={submit} className="auth-form" key={isSignup ? "signup" : "signin"}>
              {isSignup && (
                <div className="signup-grid">
                  <AuthField label="Full name" name="fullName" placeholder="Alex Morgan" autoComplete="name" />
                  <AuthField label="Company" name="company" placeholder="Acme Logistics" autoComplete="organization" />
                </div>
              )}
              <AuthField
                label="Work email address"
                name="email"
                type="email"
                placeholder="operator@company.com"
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
              <AuthField
                label="Password"
                name="password"
                type={showPassword ? "text" : "password"}
                placeholder={isSignup ? "Choose a strong password" : "Enter your password"}
                autoComplete={isSignup ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                trailing={passwordToggle}
              />
              {isSignup && (
                <>
                  <div className="password-checklist"><PasswordChecklist value={password} /></div>
                  <AuthField label="Your role" name="role" placeholder="Operations leader" autoComplete="organization-title" />
                  <AuthField
                    label="Confirm password"
                    name="confirmPassword"
                    type={showPassword ? "text" : "password"}
                    placeholder="Re-enter your password"
                    autoComplete="new-password"
                  />
                </>
              )}
              {!isSignup && (
                <div className="auth-options">
                  <label><input type="checkbox" name="rememberMe" /> <span>Keep me signed in</span></label>
                  <Link className="inline-link" to="/reset-password">Reset password</Link>
                </div>
              )}
              <button className="button button-green auth-submit" type="submit" disabled={busy} aria-busy={busy}>
                {busy
                  ? (isSignup ? "Creating account..." : "Signing in...")
                  : (isSignup ? "Create SurgeGuard account" : "Sign in to command centre")}
                {!busy && <ArrowRight size={16} />}
              </button>
              {notice && (
                <div className={`form-notice${notice.tone === "error" ? " is-error" : ""}`} role={notice.tone === "error" ? "alert" : "status"}>
                  {notice.text}
                </div>
              )}
              {needsVerification && (
                <button type="button" className="secondary-button" onClick={() => void resend()} disabled={resending}>
                  {resending ? "Sending..." : "Resend verification email"}
                </button>
              )}
            </form>

            <div className="auth-divider"><span /> Or continue with <span /></div>
            <button
              type="button"
              className="google-button"
              onClick={() => setNotice({ tone: "info", text: "Google authentication is coming soon." })}
            >
              <span className="google-glyph">G</span> Coming Soon
            </button>
            <p className="switch-copy">
              {isSignup ? "Already have an account?" : "New to SurgeGuard?"}{" "}
              <button type="button" className="inline-link" onClick={switchMode}>
                {isSignup ? "Sign in" : "Create an account"}
              </button>
            </p>
            <Link className="return-link" to="/">← Return to main site</Link>
          </div>
        </section>

        <section className="auth-visual-column">
          <div className="auth-visual">
            <div className="visual-status"><span /> Systems online <em>SG // AUTH-01</em></div>
            <div className="auth-radar"><div /><div /><div /><span /></div>
            <div className="auth-wave">
              <svg viewBox="0 0 800 200" preserveAspectRatio="none">
                <path d="M0 125 C80 123 96 122 136 116 S177 80 218 118 S270 155 309 108 S349 74 390 115 S433 143 474 107 S516 86 554 111 S595 137 638 105 S684 77 723 101 S765 112 800 98" />
              </svg>
            </div>
            <div className="visual-copy">
              <h2>Know the gap<br /><span>before it breaks.</span></h2>
              <p>One intelligent view of capacity, demand, and every promise your operation makes.</p>
            </div>
            <div className="visual-metrics">
              <MetricCard value="98.7%" label="SLA health" tone="safe" />
              <MetricCard value="12.4k" label="Orders tracked" />
              <MetricCard value="24/7" label="Live detection" />
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

import { useState } from "react";
import { ArrowRight, Radar, ShieldCheck } from "lucide-react";

import { AuthField } from "@/components/brand";
import { RecoveryShell } from "@/components/RecoveryShell";
import { ApiError, requestPasswordReset } from "@/lib/api";

type Notice = { tone: "info" | "error"; text: string } | null;

export function ResetPasswordPage() {
  const [sent, setSent] = useState(false);
  const [email, setEmail] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  // The backend answers identically whether or not the address is registered,
  // so nothing here may claim "no account with that email".
  const send = async () => {
    if (!email || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      await requestPasswordReset(email);
      if (sent) setNotice({ tone: "info", text: "A new reset email has been requested." });
      setSent(true);
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof ApiError ? error.message : "Something went wrong. Please try again." });
    } finally {
      setBusy(false);
    }
  };

  const noticeBox = notice && (
    <div className={`form-notice${notice.tone === "error" ? " is-error" : ""}`} role={notice.tone === "error" ? "alert" : "status"}>
      {notice.text}
    </div>
  );

  return (
    <RecoveryShell
      canvasId="reset-canvas"
      steps={["active", "", ""]}
      eyebrow="Password recovery"
      title={<>Reset your password.<br /><span>Regain command.</span></>}
      aside={{
        status: "Secure recovery",
        code: "SG // RESET-01",
        icon: <Radar size={31} />,
        title: <>One email.<br /><span>One secure next step.</span></>,
        copy: "The reset email keeps password changes separate, verified, and intentional.",
        footerLabel: "LINK STATUS",
        footerValue: "ENCRYPTED",
      }}
    >
      {!sent ? (
        <>
          <p className="auth-intro">
            Enter your account email and we will send a secure reset email. Its button opens the page for setting your new password.
          </p>
          <form className="auth-form" onSubmit={(event) => { event.preventDefault(); void send(); }}>
            <AuthField
              label="Email address"
              name="email"
              type="email"
              placeholder="operator@company.com"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
            <button className="button button-green auth-submit" type="submit" disabled={busy} aria-busy={busy}>
              {busy ? "Sending..." : <>Send reset email <ArrowRight size={16} /></>}
            </button>
            {noticeBox}
          </form>
        </>
      ) : (
        <div className="reset-state">
          <div className="reset-state-icon"><ShieldCheck size={22} /></div>
          <strong>Reset email sent</strong>
          <p>
            If <b>{email}</b> belongs to an account, a secure reset email is on its way. Use its reset button to set a new
            password. If you do not see it, check your spam folder.
          </p>
          <button className="secondary-button" type="button" disabled={busy} onClick={() => void send()}>
            {busy ? "Sending..." : "Resend email"}
          </button>
          {noticeBox}
        </div>
      )}
    </RecoveryShell>
  );
}

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Check, ShieldAlert, ShieldCheck } from "lucide-react";

import { AuthField } from "@/components/brand";
import {
  PasswordChecklist,
  useAllRulesMet,
} from "@/components/PasswordChecklist";
import { RecoveryShell } from "@/components/RecoveryShell";
import { useAuth } from "@/contexts/AuthContext";
import {
  ApiError,
  confirmPasswordReset,
  validatePasswordResetToken,
} from "@/lib/api";

export function SetNewPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const { refreshUser } = useAuth();
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [complete, setComplete] = useState(false);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const rulesMet = useAllRulesMet(newPassword);

  // A peek, not a consume: email scanners prefetch links, so only the POST
  // carrying the new password ever uses the token up.
  const check = useQuery({
    queryKey: ["reset-token", token],
    queryFn: () => validatePasswordResetToken(token),
    enabled: token.length > 0 && !complete,
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    placeholderData: undefined,
  });
  const tokenInvalid = !complete && (token.length === 0 || check.isError);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setNotice("");
    if (!rulesMet) {
      setNotice("Your new password does not meet every requirement yet.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setNotice("Passwords do not match. Please check both fields.");
      return;
    }
    setBusy(true);
    try {
      await confirmPasswordReset(token, newPassword);
      setComplete(true);
      // The backend just revoked every session for this account, possibly
      // including one open in this browser.
      void refreshUser().catch(() => undefined);
    } catch (error) {
      setNotice(
        error instanceof ApiError
          ? error.message
          : "Something went wrong. Please try again."
      );
    } finally {
      setBusy(false);
    }
  };

  let body;
  if (complete) {
    body = (
      <div className="reset-state">
        <div className="reset-state-icon">
          <Check size={22} />
        </div>
        <strong>Password reset complete</strong>
        <p>
          Your new password is active. Every session on this account was signed
          out, so sign in again to continue.
        </p>
        <Link className="button button-green auth-submit" to="/auth">
          Return to sign in <ArrowRight size={16} />
        </Link>
      </div>
    );
  } else if (tokenInvalid) {
    body = (
      <div className="reset-state is-error">
        <div className="reset-state-icon">
          <ShieldAlert size={22} />
        </div>
        <strong>This link has expired or was already used</strong>
        <p>
          Reset links work once and only for a limited time. Request a fresh one
          to continue.
        </p>
        <Link className="button button-green auth-submit" to="/reset-password">
          Request a new link <ArrowRight size={16} />
        </Link>
      </div>
    );
  } else if (check.isPending) {
    body = <p className="auth-intro">Checking your reset link...</p>;
  } else {
    body = (
      <>
        <p className="auth-intro">
          You arrived here from a verified reset email. Choose a new password
          for your SurgeGuard account.
        </p>
        <form className="auth-form" onSubmit={submit}>
          <AuthField
            label="New password"
            name="newPassword"
            type="password"
            placeholder="Enter a new password"
            autoComplete="new-password"
            value={newPassword}
            onChange={event => setNewPassword(event.target.value)}
          />
          <div className="password-checklist">
            <PasswordChecklist value={newPassword} />
          </div>
          <AuthField
            label="Confirm new password"
            name="confirmPassword"
            type="password"
            placeholder="Re-enter your new password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={event => setConfirmPassword(event.target.value)}
          />
          <button
            className="button button-green auth-submit"
            type="submit"
            disabled={busy}
            aria-busy={busy}
          >
            {busy ? (
              "Updating..."
            ) : (
              <>
                Update password <ArrowRight size={16} />
              </>
            )}
          </button>
          {notice && (
            <div className="form-notice is-error" role="alert">
              {notice}
            </div>
          )}
        </form>
      </>
    );
  }

  return (
    <RecoveryShell
      canvasId="set-password-canvas"
      steps={["complete", "complete", complete ? "complete" : "active"]}
      eyebrow="Secure password update"
      title={
        complete ? (
          <>
            Password updated.
            <br />
            <span>You are back in control.</span>
          </>
        ) : (
          <>
            Set a new password.
            <br />
            <span>Secure the next shift.</span>
          </>
        )
      }
      aside={{
        status: "Secure recovery",
        code: "SG // SET-NEW-01",
        icon: <ShieldCheck size={31} />,
        title: (
          <>
            Verified link.
            <br />
            <span>Fresh credentials.</span>
          </>
        ),
        copy: "This is the dedicated password destination opened from the reset email.",
        footerLabel: "LINK",
        footerValue: tokenInvalid ? "EXPIRED" : "VERIFIED",
      }}
    >
      {body}
    </RecoveryShell>
  );
}

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, MailCheck, ShieldAlert } from "lucide-react";

import { AuthField } from "@/components/brand";
import { RecoveryShell } from "@/components/RecoveryShell";
import { useAuth } from "@/contexts/AuthContext";
import { ApiError, resendVerification, verifyEmail } from "@/lib/api";

export function VerifyEmailPage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const { user } = useAuth();
  const [email, setEmail] = useState("");
  const [resent, setResent] = useState("");
  const [busy, setBusy] = useState(false);

  // Verification CONSUMES the token. A bare effect would fire twice under
  // StrictMode and report a valid link as already used; the query dedupes it
  // to one request per token and keeps the answer for the life of the page.
  const verification = useQuery({
    queryKey: ["verify-email", token],
    queryFn: () => verifyEmail(token),
    enabled: token.length > 0,
    retry: false,
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
    placeholderData: undefined,
  });
  const failed = token.length === 0 || verification.isError;

  const resend = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!email || busy) return;
    setBusy(true);
    try {
      await resendVerification(email);
      // Same answer whether or not the address exists; do not imply otherwise.
      setResent(
        "If that address has an unverified account, a fresh link is on its way."
      );
    } catch (error) {
      setResent(
        error instanceof ApiError
          ? error.message
          : "Something went wrong. Please try again."
      );
    } finally {
      setBusy(false);
    }
  };

  let body;
  if (failed) {
    body = (
      <>
        <div className="reset-state is-error">
          <div className="reset-state-icon">
            <ShieldAlert size={22} />
          </div>
          <strong>This link has expired or was already used</strong>
          <p>
            Verification links work once. If your address is already verified
            there is nothing more to do. Otherwise, request a fresh link.
          </p>
        </div>
        <form className="auth-form verify-resend" onSubmit={resend}>
          <AuthField
            label="Account email"
            name="email"
            type="email"
            placeholder="operator@company.com"
            autoComplete="email"
            value={email}
            onChange={event => setEmail(event.target.value)}
          />
          <button className="secondary-button" type="submit" disabled={busy}>
            {busy ? "Sending..." : "Send a new verification link"}
          </button>
          {resent && (
            <div className="form-notice" role="status">
              {resent}
            </div>
          )}
        </form>
      </>
    );
  } else if (verification.isPending) {
    body = <p className="auth-intro">Confirming your email address...</p>;
  } else {
    body = (
      <div className="reset-state">
        <div className="reset-state-icon">
          <MailCheck size={22} />
        </div>
        <strong>Email verified</strong>
        <p>
          Your address is confirmed. Alerts and account emails will reach you
          here.
        </p>
        <Link
          className="button button-green auth-submit"
          to={user ? "/dashboard" : "/auth"}
        >
          {user ? "Open the command centre" : "Sign in"}{" "}
          <ArrowRight size={16} />
        </Link>
      </div>
    );
  }

  const footerValue = failed
    ? "UNCONFIRMED"
    : verification.isPending
      ? "CHECKING"
      : "CONFIRMED";

  return (
    <RecoveryShell
      canvasId="verify-canvas"
      steps={[
        "complete",
        failed ? "active" : "complete",
        failed || verification.isPending ? "" : "complete",
      ]}
      eyebrow="Email verification"
      title={
        failed ? (
          <>
            Link not accepted.
            <br />
            <span>One step to fix it.</span>
          </>
        ) : (
          <>
            Confirm your address.
            <br />
            <span>Stay in the loop.</span>
          </>
        )
      }
      aside={{
        status: "Identity check",
        code: "SG // VERIFY-01",
        icon: <MailCheck size={31} />,
        title: (
          <>
            One click.
            <br />
            <span>A trusted inbox.</span>
          </>
        ),
        copy: "Verifying your email makes sure surge alerts reach a real, reachable operator.",
        footerLabel: "ADDRESS",
        footerValue,
      }}
    >
      {body}
    </RecoveryShell>
  );
}

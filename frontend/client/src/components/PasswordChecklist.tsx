import { useQuery } from "@tanstack/react-query";
import { getPasswordPolicy } from "@/lib/api";
import type { PasswordPolicy } from "@/lib/types";
import "./PasswordChecklist.css";

/**
 * Replicates the backend's PasswordRules evaluation so the live checklist
 * reflects the same rules served by GET /auth/password-policy.
 */
// Weak to strong, tuned for the dark auth surface.
const SEGMENT_COLOURS = ["#f87171", "#fb923c", "#fbbf24", "#a3e635", "#34d399"];
const STRENGTH_NOTE = [
  "Enter a password to begin.",
  "Too weak to accept.",
  "Still missing requirements.",
  "Almost there.",
  "One requirement left.",
  "Meets every requirement.",
];

function isMet(rule: { id: string }, value: string, policy: PasswordPolicy): boolean {
  switch (rule.id) {
    case "length":
      return value.length >= policy.min_length;
    case "uppercase":
      return /[A-Z]/.test(value);
    case "lowercase":
      return /[a-z]/.test(value);
    case "digit":
      return /[0-9]/.test(value);
    case "symbol":
      return /[^A-Za-z0-9]/.test(value);
    default:
      return false;
  }
}

export function PasswordChecklist({ value }: { value: string }) {
  const { data: policy, isError } = useQuery({
    queryKey: ["password-policy"],
    queryFn: getPasswordPolicy,
    staleTime: Infinity,
    retry: 1,
  });

  if (isError) {
    return (
      <p className="auth-meter-note">
        Password requirements are unavailable right now. You can still submit;
        the server will say if anything is missing.
      </p>
    );
  }

  if (!policy) return null;

  const met = policy.rules.filter((rule) => isMet(rule, value, policy)).length;
  const total = policy.rules.length;
  const note = value.length === 0 ? STRENGTH_NOTE[0] : STRENGTH_NOTE[met + 1] ?? STRENGTH_NOTE[5];
  const colour = value.length > 0 ? SEGMENT_COLOURS[Math.min(met - 1, SEGMENT_COLOURS.length - 1)] : undefined;

  return (
    <>
      <div
        className="auth-meter"
        role="progressbar"
        aria-valuenow={met}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label="Password requirements met"
      >
        {policy.rules.map((rule, index) => (
          <div
            key={rule.id}
            className="auth-meter-seg"
            style={value.length > 0 && index < met ? { background: colour } : undefined}
          />
        ))}
      </div>
      <p className="auth-meter-note">{note}</p>
      <ul className="auth-rules">
        {policy.rules.map((rule) => {
          const ok = isMet(rule, value, policy);
          return (
            <li className="auth-rule" data-met={ok} key={rule.id}>
              <span className="auth-tick" aria-hidden="true" />
              <span>{rule.label}</span>
              <span className="sr-only">{ok ? " — met" : " — not met"}</span>
            </li>
          );
        })}
      </ul>
    </>
  );
}

export function useAllRulesMet(value: string): boolean {
  const { data: policy } = useQuery({
    queryKey: ["password-policy"],
    queryFn: getPasswordPolicy,
    staleTime: Infinity,
    retry: 1,
  });
  if (!policy) return true;
  return policy.rules.every((rule) => isMet(rule, value, policy));
}

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { BrandMark, useAmbientCanvas } from "@/components/brand";

type Step = "active" | "complete" | "";

type Props = {
  canvasId: string;
  steps: [Step, Step, Step];
  eyebrow: string;
  title: ReactNode;
  aside: {
    status: string;
    code: string;
    icon: ReactNode;
    title: ReactNode;
    copy: string;
    footerLabel: string;
    footerValue: string;
  };
  children: ReactNode;
};

/**
 * The two-panel card used by every out-of-band account page: requesting a
 * reset, setting the new password, and confirming an email address.
 */
export function RecoveryShell({ canvasId, steps, eyebrow, title, aside, children }: Props) {
  useAmbientCanvas(canvasId);
  return (
    <div className="auth-shell reset-shell">
      <canvas id={canvasId} className="ambient-canvas" />
      <div className="grain-layer" />
      <Link className="auth-brand" to="/"><BrandMark /></Link>
      <main className="reset-layout">
        <section className="reset-card glass-panel">
          <div className="reset-progress">
            <span className={steps[0]}>01</span><i /><span className={steps[1]}>02</span><i /><span className={steps[2]}>03</span>
          </div>
          <div className="eyebrow"><span className="eyebrow-dot" /> {eyebrow}</div>
          <h1>{title}</h1>
          {children}
          <p className="switch-copy"><Link className="inline-link" to="/auth">← Back to sign in</Link></p>
          <Link className="return-link" to="/">Return to main site</Link>
        </section>
        <aside className="reset-aside">
          <div className="visual-status"><span /> {aside.status} <em>{aside.code}</em></div>
          <div className="reset-aside-orbit">{aside.icon}</div>
          <div className="visual-copy">
            <h2>{aside.title}</h2>
            <p>{aside.copy}</p>
          </div>
          <div className="reset-aside-footer"><span>{aside.footerLabel}</span><b>{aside.footerValue}</b></div>
        </aside>
      </main>
    </div>
  );
}

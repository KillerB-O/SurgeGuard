import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, ArrowRight, BarChart3, Check, ChevronDown, ChevronRight, CircleDot, Gauge, Layers3, Menu, Radar, ShieldCheck, Sparkles, X, Zap } from "lucide-react";

import { BrandMark, MetricCard, SpinningBorderButton, useAmbientCanvas } from "@/components/brand";
import { useAuth } from "@/contexts/AuthContext";

const navItems = [
  { label: "The Gap", href: "#problem" },
  { label: "How it works", href: "#how-it-works" },
  { label: "Recovery", href: "#solutions" },
  { label: "Command Centre", href: "#dashboard" },
];

function FloatingNav() {
  const { user } = useAuth();
  const [scrolled, setScrolled] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 80);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  return (
    <header className={`floating-nav ${scrolled ? "is-scrolled" : ""}`}>
      <a href="#top" className="nav-brand"><BrandMark /></a>
      <nav className={`nav-links ${mobileOpen ? "open" : ""}`}>
        {navItems.map((item) => <a key={item.href} href={item.href} onClick={() => setMobileOpen(false)}>{item.label}</a>)}
      </nav>
      {user
        ? <SpinningBorderButton className="nav-auth" href="/dashboard">Open dashboard</SpinningBorderButton>
        : <SpinningBorderButton className="nav-auth">Log in / Sign up</SpinningBorderButton>}
      <button className="mobile-nav-toggle" aria-label={mobileOpen ? "Close navigation" : "Open navigation"} onClick={() => setMobileOpen((open) => !open)}>
        {mobileOpen ? <X size={19} /> : <Menu size={19} />}
      </button>
    </header>
  );
}

function HeroSignal() {
  return (
    <div className="hero-signal" aria-hidden="true">
      <div className="signal-orbit orbit-one" /><div className="signal-orbit orbit-two" /><div className="signal-core" />
      <div className="signal-label label-left"><span /> DEMAND VECTOR <b>+24.8%</b></div>
      <div className="signal-label label-right"><span /> CAPACITY INDEX <b>67.2</b></div>
      <svg viewBox="0 0 900 210" preserveAspectRatio="none" className="signal-wave">
        <path d="M0 140 C72 138 105 142 142 132 S184 115 216 134 S256 170 294 128 S338 87 371 137 S416 164 452 127 S497 99 528 131 S565 166 612 130 S650 97 688 128 S730 158 770 133 S823 113 900 112" />
      </svg>
    </div>
  );
}

export function LandingPage() {
  const { user } = useAuth();
  useAmbientCanvas("landing-canvas");
  return (
    <div className="site-shell" id="top">
      <canvas id="landing-canvas" className="ambient-canvas" />
      <div className="grain-layer" />
      <FloatingNav />
      <main>
        <section className="landing-hero section-shell">
          <div className="hero-glow" />
          <div className="hero-trail" />
          <div className="eyebrow"><span className="eyebrow-dot" /> Intelligent fulfillment operations</div>
          <h1 className="display-title">Your warehouse is slow.<br />Demand just got faster.<br /><span>SurgeGuard detects it.</span></h1>
          <p className="hero-copy">An intelligent fulfillment platform that detects capacity crises in real time, predicts order failures, and lets operators simulate recovery strategies before demand breaks promises.</p>
          <div className="hero-actions"><a href="#dashboard" className="button button-light">Enter command centre <ArrowRight size={17} /></a><a href="#how-it-works" className="button button-ghost">How it works <ChevronDown size={16} /></a></div>
          <HeroSignal />
          <a href="#problem" className="scroll-cue"><ChevronDown size={18} /><span>Scroll to inspect</span></a>
        </section>

        <section id="problem" className="section-shell content-section">
          <div className="section-heading"><div className="section-kicker"><i /> The gap</div><h2>The moment a warehouse <span>breaks.</span></h2><p>When incoming order workload outpaces fulfillment throughput, promises start failing. SurgeGuard shows the gap before it becomes an incident.</p></div>
          <div className="gap-panel glass-panel"><div className="gap-summary"><strong>34.67%</strong><span>of orders are exposed when demand crosses capacity.</span><div className="mini-status"><CircleDot size={13} /> Live capacity signal</div></div><div className="capacity-chart"><div className="chart-meta"><span>Order workload</span><b>86.4k / hr</b></div><div className="chart-track"><div className="chart-fill demand-fill" /><div className="breach-hatch" /></div><div className="chart-meta"><span>Fulfillment throughput</span><b>57.2k / hr</b></div><div className="chart-track"><div className="chart-fill throughput-fill" /></div><div className="capacity-line"><span>safe capacity</span></div></div></div>
        </section>

        <section id="how-it-works" className="section-shell content-section">
          <div className="section-heading split-heading"><div><div className="section-kicker"><i /> How it works</div><h2>Turn operational noise into a <span>clear next move.</span></h2></div><p>One intelligence layer across demand, labor, capacity, and customer promise.</p></div>
          <div className="feature-grid"><FeatureCard icon={<Radar />} index="01" title="Detect the gap" copy="Continuously compare incoming demand with live fulfillment capacity, by node, zone, and shift." /><FeatureCard icon={<Activity />} index="02" title="Predict the break" copy="Surface the orders, SLAs, and operational constraints most likely to fail next." /><FeatureCard icon={<Zap />} index="03" title="Simulate recovery" copy="Test labor moves, routing changes, and overflow plans before making the call." /></div>
        </section>

        <section id="solutions" className="section-shell content-section solutions-section"><div className="section-heading"><div className="section-kicker"><i /> Recovery paths</div><h2>Choose the move that <span>buys time.</span></h2><p>SurgeGuard turns pressure into a shortlist of actions operators can trust.</p></div><div className="solution-grid"><SolutionCard label="Stabilize" title="Protect the promise" copy="Prioritize high-risk orders and preserve your most valuable customer commitments." icon={<ShieldCheck />} /><SolutionCard label="Rebalance" title="Move capacity" copy="See where labor, inventory, and fulfillment load can shift to close the gap." icon={<BarChart3 />} featured /><SolutionCard label="Simulate" title="Test the recovery" copy="Compare recovery scenarios side by side before the next wave hits." icon={<Gauge />} /></div></section>

        <section id="dashboard" className="section-shell content-section dashboard-section"><div className="section-heading split-heading"><div><div className="section-kicker"><i /> Command centre</div><h2>The operational picture, <span>without the fog.</span></h2></div><p>Built for the moment when a good operator needs one decisive view.</p></div><div className="dashboard-window glass-panel"><div className="window-top"><div className="window-brand"><span className="window-dots"><i /><i /><i /></span><span>surgeguard / command-centre</span></div><span className="live-pill"><span /> LIVE</span></div><div className="dashboard-body"><aside className="dashboard-sidebar"><span className="sidebar-active"><Activity size={15} /> Overview</span><span><Radar size={15} /> Risk map</span><span><BarChart3 size={15} /> Recovery</span><span><Layers3 size={15} /> Network</span></aside><div className="dashboard-main"><div className="dash-header"><div><span className="mono-label">Tuesday / 14:42 UTC</span><h3>Fulfillment health</h3></div><span className="health-badge"><Check size={13} /> Monitoring</span></div><div className="metric-row"><MetricCard value="98.7%" label="SLA health" tone="safe" /><MetricCard value="12.4k" label="Orders tracked" /><MetricCard value="03" label="Active alerts" tone="warning" /></div><div className="dashboard-chart"><div className="chart-header"><span>Demand vs throughput</span><span className="chart-legend"><i /> Demand <i /> Throughput</span></div><svg viewBox="0 0 700 150" preserveAspectRatio="none"><path className="chart-gridline" d="M0 30H700M0 75H700M0 120H700" /><path className="demand-line" d="M0 112 C80 109 110 102 156 98 S216 80 260 88 S318 50 365 62 S424 22 474 46 S536 18 584 31 S650 15 700 17" /><path className="throughput-line" d="M0 124 C90 122 120 120 170 118 S230 107 276 110 S330 102 385 105 S444 97 500 102 S580 92 625 95 S670 88 700 90" /></svg></div></div></div></div></section>

        <section className="section-shell cta-section"><div className="cta-panel"><div className="section-kicker"><i /> Next shift, clearer</div><h2>Make the gap visible<br /><span>before it makes the decision.</span></h2><p>Bring your operation into focus with SurgeGuard.</p><SpinningBorderButton className="cta-button" href={user ? "/dashboard" : "/auth"}>{user ? "Open the command centre" : "Explore the system"}</SpinningBorderButton></div></section>
      </main>
      <footer className="site-footer section-shell"><a href="#top"><BrandMark /></a><div className="footer-links"><a href="#problem">The Gap</a><a href="#solutions">Recovery</a><Link to={user ? "/dashboard" : "/auth"}>{user ? "Dashboard" : "Access"}</Link></div><span className="footer-copy">© 2026 SurgeGuard / Intelligent fulfillment operations</span></footer>
    </div>
  );
}

function FeatureCard({ icon, index, title, copy }: { icon: React.ReactNode; index: string; title: string; copy: string }) { return <article className="feature-card glass-panel"><div className="feature-top"><span className="feature-icon">{icon}</span><span className="feature-index">{index}</span></div><h3>{title}</h3><p>{copy}</p><a href="#dashboard" className="card-link">Inspect signal <ArrowRight size={15} /></a></article>; }
function SolutionCard({ icon, label, title, copy, featured = false }: { icon: React.ReactNode; label: string; title: string; copy: string; featured?: boolean }) { return <article className={`solution-card glass-panel ${featured ? "is-featured" : ""}`}>{featured && <span className="recommended-badge">Recommended</span>}<span className="solution-cost">{label}</span><div className="solution-icon">{icon}</div><h3>{title}</h3><p>{copy}</p><a href="#dashboard" className="card-link">See the move <ArrowRight size={15} /></a></article>; }

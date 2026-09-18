import { useEffect, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Layers3 } from "lucide-react";

/** Drifting particle field behind the marketing and auth pages. */
export function useAmbientCanvas(canvasId: string) {
  useEffect(() => {
    const canvas = document.getElementById(canvasId) as HTMLCanvasElement | null;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let frame = 0;
    let animationId = 0;
    const particles = Array.from({ length: 76 }, () => ({
      x: Math.random(),
      y: Math.random(),
      radius: Math.random() * 1.2 + 0.25,
      speed: Math.random() * 0.00023 + 0.00005,
      opacity: Math.random() * 0.35 + 0.06,
    }));

    const resize = () => {
      const scale = window.devicePixelRatio || 1;
      canvas.width = window.innerWidth * scale;
      canvas.height = window.innerHeight * scale;
      canvas.style.width = `${window.innerWidth}px`;
      canvas.style.height = `${window.innerHeight}px`;
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
    };

    const render = () => {
      const width = window.innerWidth;
      const height = window.innerHeight;
      ctx.clearRect(0, 0, width, height);
      const glow = ctx.createRadialGradient(width * 0.46, height * 0.65, 0, width * 0.46, height * 0.65, width * 0.65);
      glow.addColorStop(0, "rgba(52,211,153,.05)");
      glow.addColorStop(0.5, "rgba(6,78,59,.025)");
      glow.addColorStop(1, "transparent");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, width, height);
      particles.forEach((particle) => {
        particle.y -= particle.speed;
        if (particle.y < -0.02) particle.y = 1.02;
        ctx.beginPath();
        ctx.arc(particle.x * width, particle.y * height, particle.radius, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(52,211,153,${particle.opacity})`;
        ctx.fill();
      });
      frame += 1;
      animationId = requestAnimationFrame(render);
    };

    resize();
    render();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      cancelAnimationFrame(animationId);
      void frame;
    };
  }, [canvasId]);
}

export function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <span className="brand-mark">
      <span className="brand-mark-icon"><Layers3 size={19} strokeWidth={2.4} /></span>
      {!compact && <span>SurgeGuard</span>}
    </span>
  );
}

export function SpinningBorderButton({ children, href = "/auth", className = "" }: { children: ReactNode; href?: string; className?: string }) {
  return (
    <Link className={`spinning-button ${className}`} to={href}>
      <span className="spinning-button-surface">{children}<ArrowRight size={14} /></span>
    </Link>
  );
}

export function MetricCard({ value, label, tone = "" }: { value: string; label: string; tone?: string }) {
  return <div className={`metric-card ${tone}`}><strong>{value}</strong><span>{label}</span></div>;
}

type AuthFieldProps = {
  label: string;
  name: string;
  placeholder: string;
  type?: string;
  trailing?: ReactNode;
  value?: string;
  defaultValue?: string;
  autoComplete?: string;
  required?: boolean;
  onChange?: (event: React.ChangeEvent<HTMLInputElement>) => void;
};

export function AuthField({ label, name, placeholder, type = "text", trailing, value, defaultValue, autoComplete, required = true, onChange }: AuthFieldProps) {
  return (
    <label className="auth-field">
      <span>{label}</span>
      <span className="input-wrap">
        <input name={name} type={type} placeholder={placeholder} value={value} defaultValue={defaultValue} autoComplete={autoComplete} onChange={onChange} required={required} />
        {trailing}
      </span>
    </label>
  );
}

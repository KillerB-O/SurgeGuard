import React, { useRef, useLayoutEffect, useState, cloneElement } from "react";

export type NavItem = {
  id: string | number;
  icon: React.ReactElement<React.SVGProps<SVGSVGElement>>;
  label?: string;
  badge?: number | null;
  onClick?: () => void;
};

type Props = {
  items: NavItem[];
  activeIndex: number;
  onTabChange: (index: number) => void;
};

/**
 * Vertical navigation with a sliding spotlight beam on the right edge.
 * The beam tracks the active item with a smooth cubic-bezier transition,
 * and a radial glow cone bleeds leftward from the bar.
 */
export function LimelightNav({ items, activeIndex, onTabChange }: Props) {
  const [isReady, setIsReady] = useState(false);
  const itemRefs = useRef<(HTMLAnchorElement | null)[]>([]);
  const beamRef = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    if (items.length === 0) return;

    const beam = beamRef.current;
    const activeItem = itemRefs.current[activeIndex];

    if (beam && activeItem) {
      const newTop =
        activeItem.offsetTop +
        activeItem.offsetHeight / 2 -
        beam.offsetHeight / 2;
      beam.style.top = `${newTop}px`;

      if (!isReady) {
        setTimeout(() => setIsReady(true), 50);
      }
    }
  }, [activeIndex, isReady, items]);

  if (items.length === 0) return null;

  return (
    <div className="limelight-rail">
      {items.map(({ id, icon, label, badge, onClick }, index) => (
        <a
          key={id}
          ref={(el) => { itemRefs.current[index] = el; }}
          className={`limelight-item${activeIndex === index ? " active" : ""}`}
          onClick={() => {
            onTabChange(index);
            onClick?.();
          }}
          aria-label={label}
        >
          {cloneElement(icon, {
            className: "limelight-icon",
            style: {
              width: 18,
              height: 18,
              opacity: activeIndex === index ? 1 : 0.6,
              color: activeIndex === index ? "var(--sidebar-ink)" : "var(--sidebar-ink-muted)",
              transition: "opacity 0.15s, color 0.15s",
            },
          })}
          <span className="limelight-label">{label}</span>
          {badge != null && badge > 0 && (
            <span className="nav-badge">{badge}</span>
          )}
        </a>
      ))}

      {/* The sliding beam + glow cone */}
      <div
        ref={beamRef}
        className={`limelight-beam${isReady ? " ready" : ""}`}
        style={{ top: -999 }}
      >
        <div className="limelight-cone" />
      </div>
    </div>
  );
}

import React, { useEffect, useRef, useState } from 'react';
import { useChat } from '../context/ChatContext';
import { fetchStats } from '../api/client';
import Header from '../components/Header';
import PresetGrid from '../components/PresetGrid';
import HeroLeaf from '../components/hero/HeroLeaf';

/* ── Animated count-up ─────────────────────────────────────────────────── */
function useCountUp(target: number, durationMs = 1200): number {
  const [value, setValue] = useState(0);
  const startedRef = useRef(false);

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    const start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      // easeOutCubic
      const eased = 1 - Math.pow(1 - t, 3);
      setValue(Math.round(eased * target));
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, durationMs]);

  return value;
}

interface Stat {
  value: number;
  label: string;
}
// Fallback shown before /api/stats resolves (or if it fails). Real counts come
// from the live catalog via fetchStats() in HomePage.
const STATS_FALLBACK: Stat[] = [
  { value: 25, label: 'Products' },
  { value: 6, label: 'Categories' },
  { value: 7, label: 'On Sale' },
];

function StatCounter({ stat }: { stat: Stat }) {
  const n = useCountUp(stat.value);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: '2rem',
          fontWeight: 600,
          color: 'var(--spring-green)',
          textShadow: '0 0 30px rgba(0,237,100,0.3)',
          lineHeight: 1,
        }}
      >
        {n}
      </span>
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: '0.7rem',
          textTransform: 'uppercase',
          letterSpacing: '2px',
          color: 'var(--text-secondary)',
        }}
      >
        {stat.label}
      </span>
    </div>
  );
}

/* ── Backgrounds ───────────────────────────────────────────────────────── */
const auroraStyle: React.CSSProperties = {
  position: 'absolute',
  inset: '-30%',
  zIndex: 0,
  pointerEvents: 'none',
  filter: 'blur(50px)',
  opacity: 0.9,
  background: [
    'radial-gradient(35% 35% at 20% 25%, rgba(0,237,100,0.12), transparent 70%)',
    'radial-gradient(40% 40% at 75% 30%, rgba(64,120,255,0.08), transparent 70%)',
    'radial-gradient(45% 45% at 55% 80%, rgba(150,90,255,0.06), transparent 70%)',
  ].join(','),
  animation: 'aurora-drift 60s linear infinite',
};

const gridDotStyle: React.CSSProperties = {
  position: 'absolute',
  inset: 0,
  zIndex: 0,
  pointerEvents: 'none',
  backgroundImage:
    'radial-gradient(rgba(255,255,255,0.07) 1px, transparent 1px)',
  backgroundSize: '40px 40px',
  WebkitMaskImage:
    'radial-gradient(ellipse 80% 70% at 40% 40%, #000 30%, transparent 80%)',
  maskImage:
    'radial-gradient(ellipse 80% 70% at 40% 40%, #000 30%, transparent 80%)',
};

const heroStyle: React.CSSProperties = {
  position: 'relative',
  minHeight: '70vh',
  display: 'flex',
  alignItems: 'center',
  padding: '64px 24px',
  background: 'var(--bg)',
  overflow: 'hidden',
};

const consoleBoxStyle: React.CSSProperties = {
  position: 'relative',
  zIndex: 1,
  maxWidth: 640,
  width: '100%',
  padding: 40,
  borderRadius: 'var(--radius-xl)',
  background: 'rgba(12,17,23,0.72)',
  backdropFilter: 'blur(14px)',
  WebkitBackdropFilter: 'blur(14px)',
  border: '1px solid transparent',
  backgroundImage:
    'linear-gradient(rgba(12,17,23,0.72), rgba(12,17,23,0.72)), linear-gradient(135deg, rgba(0,237,100,0.5), rgba(255,255,255,0.06))',
  backgroundOrigin: 'border-box',
  backgroundClip: 'padding-box, border-box',
  animation: 'fade-up 0.6s ease both',
};

const titleStyle: React.CSSProperties = {
  fontFamily: 'var(--font-display)',
  fontSize: 'clamp(2.2rem, 5vw, 3.2rem)',
  fontWeight: 700,
  lineHeight: 1.05,
  color: 'var(--text)',
  margin: 0,
};

const subtitleStyle: React.CSSProperties = {
  fontFamily: 'var(--font-ui)',
  fontSize: '1.1rem',
  lineHeight: 1.7,
  color: 'var(--text-secondary)',
  margin: '18px 0 28px',
  maxWidth: 540,
};

const statsRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 40,
  marginBottom: 30,
  flexWrap: 'wrap',
};

const footerStyle: React.CSSProperties = {
  textAlign: 'center',
  padding: '40px 24px 56px',
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  letterSpacing: '0.5px',
  color: 'var(--text-secondary)',
};

function CtaButton({ onClick }: { onClick: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 10,
        padding: '12px 22px',
        fontFamily: 'var(--font-mono)',
        fontSize: 13,
        fontWeight: 500,
        letterSpacing: '0.5px',
        color: 'var(--spring-green)',
        background: hover ? 'rgba(0,237,100,0.1)' : 'var(--green-tint)',
        border: '1px solid var(--green-border)',
        borderRadius: 'var(--radius-md)',
        cursor: 'pointer',
        transition: 'transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease',
        transform: hover ? 'translateY(-2px)' : 'none',
        boxShadow: hover ? '0 0 28px rgba(0,237,100,0.25)' : 'none',
      }}
    >
      <span aria-hidden="true">✦</span>
      Ask the assistant
    </button>
  );
}

export default function HomePage() {
  const { setOpen } = useChat();
  const [stats, setStats] = useState<Stat[]>(STATS_FALLBACK);

  // Pull live catalog counts on mount; keep the static fallback per-field if a
  // value is null (DB unreachable) or the request fails.
  useEffect(() => {
    let cancelled = false;
    fetchStats()
      .then((s) => {
        if (cancelled) return;
        setStats([
          { value: s.products ?? STATS_FALLBACK[0].value, label: 'Products' },
          { value: s.categories ?? STATS_FALLBACK[1].value, label: 'Categories' },
          { value: s.on_sale ?? STATS_FALLBACK[2].value, label: 'On Sale' },
        ]);
      })
      .catch(() => {
        /* keep STATS_FALLBACK */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      <Header />
      <main>
        <section style={heroStyle}>
          <div style={auroraStyle} aria-hidden="true" />
          <div style={gridDotStyle} aria-hidden="true" />
          {/* Decorative GPU particle leaf — assembles into the MongoDB
              logomark on the right half of the hero, behind the console. */}
          <HeroLeaf isDark />
          <div style={consoleBoxStyle}>
            <h1 style={titleStyle}>
              Shop smarter
              <br />
              with{' '}
              <span
                style={{
                  color: 'var(--spring-green)',
                  textShadow: '0 0 40px rgba(0,237,100,0.35)',
                }}
              >
                Agent Cartsmith
              </span>
            </h1>
            <p style={subtitleStyle}>
              Your AI grocery concierge — recipes, deals, loyalty, and shopping
              lists, powered by MongoDB Atlas + LangChain Deep Agents.
            </p>
            <div style={statsRowStyle}>
              {stats.map((s) => (
                // Key includes the value so the counter remounts and
                // re-animates when live data replaces the fallback.
                <StatCounter key={`${s.label}-${s.value}`} stat={s} />
              ))}
            </div>
            <CtaButton onClick={() => setOpen(true)} />
          </div>
        </section>

        <PresetGrid />

        <footer style={footerStyle}>
          MongoDB Atlas + LangChain Deep Agents · retail demo
        </footer>
      </main>
    </>
  );
}

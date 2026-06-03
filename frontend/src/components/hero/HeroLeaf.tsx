/**
 * Capability gate + lazy boundary for the decorative hero leaf.
 *
 * The leaf is DECORATIVE only — an ambient brand signature with no
 * interactivity. The layer is `pointer-events:none` + `aria-hidden`, three
 * is lazy-loaded so it never enters the entry chunk, and the animation is
 * skipped entirely when the user prefers reduced motion. The render loop
 * pauses while the hero is offscreen or the tab is hidden.
 *
 * Ported from the MongoDB Partner Library hero leaf.
 */
import React, {
  Component,
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react';

const HeroLeafScene = lazy(() => import('./HeroLeafScene'));

const layerStyle: CSSProperties = {
  position: 'absolute',
  inset: 0,
  zIndex: 0,
  pointerEvents: 'none',
};

/** Swallow any WebGL/runtime error in the scene so the page never crashes. */
class SceneErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }
  componentDidCatch(): void {
    /* decorative — silently degrade to no canvas */
  }
  render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

export interface HeroLeafProps {
  isDark?: boolean;
}

export default function HeroLeaf({ isDark = true }: HeroLeafProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [enabled] = useState(() => !prefersReducedMotion());
  const [active, setActive] = useState(true);

  useEffect(() => {
    if (!enabled || typeof document === 'undefined') return;
    const recompute = (visible: boolean) =>
      setActive(document.visibilityState !== 'hidden' && visible);
    const onVis = () => recompute(true);
    document.addEventListener('visibilitychange', onVis);
    let io: IntersectionObserver | null = null;
    const node = containerRef.current;
    if (node && typeof IntersectionObserver !== 'undefined') {
      io = new IntersectionObserver(
        (entries) => {
          const e = entries[0];
          if (e) recompute(e.isIntersecting);
        },
        { threshold: 0.01 },
      );
      io.observe(node);
    }
    return () => {
      document.removeEventListener('visibilitychange', onVis);
      io?.disconnect();
    };
  }, [enabled]);

  if (!enabled) return null;

  return (
    <div ref={containerRef} style={layerStyle} aria-hidden="true">
      <SceneErrorBoundary>
        <Suspense fallback={null}>
          <HeroLeafScene isDark={isDark} active={active} />
        </Suspense>
      </SceneErrorBoundary>
    </div>
  );
}

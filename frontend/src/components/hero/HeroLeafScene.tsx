/**
 * Decorative hero leaf scene.
 *
 * A GPU particle system that assembles into the MongoDB leaf and HOLDS it
 * as the brand signature — not interactive. Lazy-loaded by `HeroLeaf` so
 * three never enters the storefront entry chunk.
 *
 * Ported from the MongoDB Partner Library hero leaf.
 */
import * as THREE from 'three';
import { useMemo, useRef } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { rasterizeLeaf } from './leafSilhouette';
import { sampleSilhouette } from './leafSampler';
import { buildParticleField } from './particleField';
import { ParticleConstellation } from './ParticleConstellation';

export interface HeroLeafSceneProps {
  isDark?: boolean;
  active?: boolean;
}

const PARTICLE_BUDGET = 4500;

function Leaf({ isDark = true }: { isDark?: boolean }) {
  const outer = useRef<THREE.Group>(null);
  const camElapsed = useRef(0);

  // Leaf target sampled from the official MongoDB silhouette (browser-only;
  // empty under jsdom where the scene never renders).
  const leafPoints = useMemo(() => {
    const mask = rasterizeLeaf(140);
    return mask
      ? sampleSilhouette(mask, PARTICLE_BUDGET, { scale: 1.6, seed: 11 })
      : new Float32Array(0);
  }, []);

  const field = useMemo(
    () => buildParticleField(leafPoints, PARTICLE_BUDGET, { seed: 21 }),
    [leafPoints],
  );

  useFrame((state, delta) => {
    // One-shot cinematic pull-in, monotonic so it survives offscreen
    // pause/resume without re-triggering.
    const dt = Math.min(Math.max(delta, 0), 0.05);
    camElapsed.current = Math.min(camElapsed.current + dt, 2);
    const ease = 1 - Math.pow(1 - camElapsed.current / 2, 3);
    state.camera.position.z = 14 - 6 * ease; // 14 → 8

    // The leaf RESTS facing the camera (a flat leaf spun on Y goes
    // edge-on). Subtle pointer-parallax only.
    if (outer.current) {
      const targetX = state.pointer.y * 0.12;
      const targetY = state.pointer.x * 0.18;
      outer.current.rotation.x += (targetX - outer.current.rotation.x) * 0.05;
      outer.current.rotation.y += (targetY - outer.current.rotation.y) * 0.05;
    }
  });

  return (
    // Offset to the right half of the hero so it sits clear of the
    // console panel on the left.
    <group position={[3.4, 0.2, 0]}>
      <group ref={outer}>
        {/* Bloom-like halo glow pass (dark hero only). */}
        {isDark && <ParticleConstellation field={field} isDark={isDark} halo />}
        {/* Visible layer: GPU particles resting in the MongoDB leaf. */}
        <ParticleConstellation field={field} isDark={isDark} />
      </group>
    </group>
  );
}

export default function HeroLeafScene({
  active = true,
  isDark = true,
}: HeroLeafSceneProps) {
  return (
    <Canvas
      dpr={[1, 2]}
      frameloop={active ? 'always' : 'never'}
      gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
      camera={{ position: [0, 0, 14], fov: 55 }}
      style={{ width: '100%', height: '100%', display: 'block' }}
    >
      <Leaf isDark={isDark} />
    </Canvas>
  );
}

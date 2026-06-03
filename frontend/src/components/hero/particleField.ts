/**
 * Pure particle-field builder. Given the sampled leaf target + a budget,
 * produce the static per-particle attribute buffers the GPU shader morphs
 * between: scatter (initial) → leaf (assembly). Deterministic for a fixed
 * seed; no WebGL/DOM.
 *
 * Ported from the MongoDB Partner Library hero leaf. The upstream version
 * also seeded a partner "constellation" home layout; this storefront has
 * no partner graph, and the Option-A shader RESTS on the leaf (it never
 * reads `home`), so every particle is ambient and the partner machinery
 * is dropped.
 */
import { mulberry32 } from './leafSampler';

export interface ParticleField {
  count: number;
  scatter: Float32Array; // count*3 — initial scattered shell
  leaf: Float32Array; // count*3 — MongoDB-leaf target (centred at origin)
  home: Float32Array; // count*3 — ambient drift cloud (unused by Option-A shader)
  seed: Float32Array; // count   — per-particle random
  bucket: Float32Array; // count — colour ramp 0..1 (green core → neon edge)
}

export interface FieldOptions {
  seed?: number;
  scatterRadius?: number;
  ambientRadius?: number;
}

function spherePoint(rand: () => number, radius: number, shell: boolean): [number, number, number] {
  const theta = rand() * Math.PI * 2;
  const phi = Math.acos(2 * rand() - 1);
  const r = shell ? radius : radius * Math.cbrt(rand());
  return [r * Math.sin(phi) * Math.cos(theta), r * Math.sin(phi) * Math.sin(theta), r * Math.cos(phi)];
}

export function buildParticleField(
  leafPoints: Float32Array,
  budget: number,
  opts: FieldOptions = {},
): ParticleField {
  const count = Math.max(0, Math.floor(budget));
  const scatterRadius = opts.scatterRadius ?? 9;
  const ambientRadius = opts.ambientRadius ?? 3.4;
  const rand = mulberry32(opts.seed ?? 1);

  const scatter = new Float32Array(count * 3);
  const leaf = new Float32Array(count * 3);
  const home = new Float32Array(count * 3);
  const seed = new Float32Array(count);
  const bucket = new Float32Array(count);

  const leafLen = leafPoints.length / 3;

  for (let i = 0; i < count; i++) {
    // scatter shell
    const s = spherePoint(rand, scatterRadius, true);
    scatter[i * 3] = s[0];
    scatter[i * 3 + 1] = s[1];
    scatter[i * 3 + 2] = s[2];

    // leaf target (cycle the sampled points + tiny jitter)
    if (leafLen > 0) {
      const li = i % leafLen;
      leaf[i * 3] = leafPoints[li * 3] + (rand() - 0.5) * 0.04;
      leaf[i * 3 + 1] = leafPoints[li * 3 + 1] + (rand() - 0.5) * 0.04;
      leaf[i * 3 + 2] = leafPoints[li * 3 + 2] + (rand() - 0.5) * 0.04;
    }

    seed[i] = rand();

    // ambient drift cloud (kept for parity; the Option-A shader rests on
    // the leaf and does not read `home`).
    const a = spherePoint(rand, ambientRadius, false);
    home[i * 3] = a[0];
    home[i * 3 + 1] = a[1];
    home[i * 3 + 2] = a[2];
    bucket[i] = rand(); // full ramp: green core → neon edge
  }

  return { count, scatter, leaf, home, seed, bucket };
}

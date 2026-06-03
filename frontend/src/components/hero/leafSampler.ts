/**
 * Pure silhouette sampler. No WebGL, no DOM: given an alpha mask + a seed
 * it returns deterministic particle target positions, aspect-preserved and
 * centred at the origin (y-up).
 *
 * Ported from the MongoDB Partner Library hero leaf.
 */

export interface SilhouetteMask {
  /** RGBA bytes, row-major (length = width*height*4). */
  data: Uint8ClampedArray;
  width: number;
  height: number;
}

/** Seeded PRNG (mulberry32) — deterministic for a given seed. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return function next(): number {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface SampleOptions {
  /** longest-axis half-extent in world units (default 1). */
  scale?: number;
  /** alpha (0–255) above which a pixel is "inside" (default 128). */
  alphaThreshold?: number;
  seed?: number;
  /** ± depth jitter as a fraction of scale (default 0.04). */
  depthJitter?: number;
}

/**
 * Sample `count` points (flat xyz Float32Array, length count*3) from the
 * mask's opaque region. Coordinates are centred at the origin and scaled
 * by the LONGER axis so aspect is preserved; y is up.
 */
export function sampleSilhouette(
  mask: SilhouetteMask,
  count: number,
  opts: SampleOptions = {},
): Float32Array {
  const scale = opts.scale ?? 1;
  const thr = opts.alphaThreshold ?? 128;
  const depthJitter = opts.depthJitter ?? 0.04;
  const rand = mulberry32(opts.seed ?? 1);
  const { data, width, height } = mask;

  const candidates: number[] = [];
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (data[(y * width + x) * 4 + 3] > thr) candidates.push(y * width + x);
    }
  }

  const out = new Float32Array(Math.max(0, count) * 3);
  const longest = Math.max(width, height);
  for (let i = 0; i < count; i++) {
    let px: number;
    let py: number;
    if (candidates.length === 0) {
      px = width / 2;
      py = height / 2;
    } else {
      const idx = candidates[Math.floor(rand() * candidates.length)];
      px = idx % width;
      py = Math.floor(idx / width);
    }
    out[i * 3] = ((px - width / 2) / longest) * 2 * scale;
    out[i * 3 + 1] = ((height / 2 - py) / longest) * 2 * scale;
    out[i * 3 + 2] = (rand() - 0.5) * 2 * depthJitter * scale;
  }
  return out;
}

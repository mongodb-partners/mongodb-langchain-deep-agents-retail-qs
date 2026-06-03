/**
 * Official MongoDB leaf silhouette.
 *
 * Path + viewBox are inlined verbatim from the official brand asset
 * (spring-green logomark). `rasterizeLeaf` draws it to an offscreen canvas
 * and returns the alpha mask; the pure sampler (`leafSampler.ts`) turns
 * that mask into particle target positions. Browser-only — returns `null`
 * under SSR/jsdom.
 *
 * Ported from the MongoDB Partner Library hero leaf.
 */
import type { SilhouetteMask } from './leafSampler';

export const MONGODB_LEAF_PATH =
  'M82.3229 28.5501C71.5367 15.7947 62.2485 2.84006 60.351 0.149477C60.1512 -0.0498257 ' +
  '59.8515 -0.0498257 59.6518 0.149477C57.7542 2.84006 48.4661 15.7947 37.6798 28.5501C' +
  '-54.9019 146.238 52.2613 225.661 52.2613 225.661L53.1601 226.258C53.959 238.516 55.9565 ' +
  '256.154 55.9565 256.154H59.9514H63.9463C63.9463 256.154 65.9438 238.615 66.7428 226.258L' +
  '67.6416 225.561C67.7414 225.561 174.905 146.238 82.3229 28.5501ZM59.9514 223.867C59.9514 ' +
  '223.867 55.1576 219.781 53.8592 217.688V217.489L59.6518 89.3375C59.6518 88.9389 60.2511 ' +
  '88.9389 60.2511 89.3375L66.0436 217.489V217.688C64.7453 219.781 59.9514 223.867 59.9514 223.867Z';

/** [minX, minY, width, height] of the leaf path. */
export const LEAF_VIEWBOX: readonly [number, number, number, number] = [0, 0, 120, 257];

/**
 * Rasterize the leaf to an alpha mask of roughly `size` px tall
 * (aspect-preserved). Returns `null` when no DOM/2D-canvas is available.
 */
export function rasterizeLeaf(size = 120): SilhouetteMask | null {
  if (typeof document === 'undefined') return null;
  const [, , vbW, vbH] = LEAF_VIEWBOX;
  const h = Math.max(8, Math.round(size));
  const w = Math.max(4, Math.round((size * vbW) / vbH));
  let canvas: HTMLCanvasElement;
  let ctx: CanvasRenderingContext2D | null;
  try {
    canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    ctx = canvas.getContext('2d');
  } catch {
    return null;
  }
  if (!ctx || typeof Path2D === 'undefined') return null;

  const path = new Path2D(MONGODB_LEAF_PATH);
  ctx.save();
  ctx.scale(w / vbW, h / vbH);
  ctx.fillStyle = '#ffffff';
  ctx.fill(path);
  ctx.restore();

  try {
    const img = ctx.getImageData(0, 0, w, h);
    return { data: img.data, width: w, height: h };
  } catch {
    return null;
  }
}

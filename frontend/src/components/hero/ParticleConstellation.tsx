/**
 * GPU particle constellation.
 *
 * A single `THREE.Points` whose motion is computed entirely in the vertex
 * shader (no FBO, no CPU per-frame writes): each particle morphs
 * scatter → leaf along a one-shot `uMorph` timeline, then drifts. Colour
 * ramps over the official data-viz palette; additive soft sprites give the
 * glow without postprocessing.
 *
 * Ported from the MongoDB Partner Library hero leaf. The interactive
 * partner-hover path is dropped (this storefront's leaf is decorative).
 */
import * as THREE from 'three';
import { useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import type { ParticleField } from './particleField';

/** Official MongoDB brand greens + slate. */
const BRAND = {
  springGreen: '#00ED64',
  forestGreen: '#00684A',
  slateNavy: '#001E2B',
  blue: '#0078FF',
} as const;

/** Official MongoDB data-visualization palette (charts/graphs only). */
const DATAVIZ = {
  sky: '#00D2FF',
  clearBlue: '#006EFF',
  lime: '#E9FF99',
} as const;

const VERT = /* glsl */ `
  attribute vec3 aScatter;
  attribute vec3 aLeaf;
  attribute float aSeed;
  attribute float aBucket;
  uniform float uTime;
  uniform float uMorph;
  uniform float uSize;
  uniform float uPixelRatio;
  varying float vBucket;
  void main() {
    // REST ON THE LEAF: particles assemble scatter -> leaf and HOLD the
    // MongoDB leaf as the brand signature.
    float a = smoothstep(0.0, 0.7, uMorph);
    vec3 pos = mix(aScatter, aLeaf, a);

    // Gentle ambient shimmer that scales in as the leaf forms.
    float t = uTime * 0.4 + aSeed * 6.2831853;
    pos += vec3(sin(t), cos(t * 1.3), sin(t * 0.7)) * 0.03 * a;

    vBucket = aBucket;

    vec4 mv = modelViewMatrix * vec4(pos, 1.0);
    gl_PointSize = uSize * uPixelRatio * (0.55 + aSeed * 0.9) / max(0.1, -mv.z);
    gl_Position = projectionMatrix * mv;
  }
`;

const FRAG = /* glsl */ `
  precision mediump float;
  uniform vec3 uColorCore;
  uniform vec3 uColorEdge1;
  uniform vec3 uColorEdge2;
  uniform vec3 uColorAccent;
  uniform vec3 uColorWhite;
  uniform float uAlpha;
  varying float vBucket;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float alpha = smoothstep(0.5, 0.0, d);
    if (alpha <= 0.0) discard;
    vec3 col = uColorCore;
    col = mix(col, uColorEdge1, smoothstep(0.3, 0.6, vBucket));
    col = mix(col, uColorEdge2, smoothstep(0.6, 0.85, vBucket));
    col = mix(col, uColorAccent, smoothstep(0.85, 1.0, vBucket));
    gl_FragColor = vec4(col, alpha * uAlpha);
  }
`;

export interface ParticleConstellationProps {
  field: ParticleField;
  size?: number;
  morphDuration?: number;
  /** Dark hero → additive glow + neon ramp. Light hero → normal blend +
   *  darker on-brand colours so the leaf reads on a pale background. */
  isDark?: boolean;
  /** Render a larger, fainter "halo" pass for bloom-like glow. */
  halo?: boolean;
}

export function ParticleConstellation({
  field,
  size = 26,
  morphDuration = 3.6,
  isDark = true,
  halo = false,
}: ParticleConstellationProps) {
  const matRef = useRef<THREE.ShaderMaterial>(null);
  // Monotonic timeline accumulators — see useFrame.
  const elapsedRef = useRef(0);
  const morphRef = useRef(0);

  const uniforms = useMemo(() => {
    // Dark: luminous data-viz neons on the deep hero, additive glow.
    // Light: darker, saturated brand colours that stay visible on a pale bg.
    const core = isDark ? BRAND.springGreen : BRAND.forestGreen;
    const edge1 = isDark ? DATAVIZ.sky : DATAVIZ.clearBlue;
    const edge2 = isDark ? DATAVIZ.clearBlue : BRAND.blue;
    const accent = isDark ? DATAVIZ.lime : BRAND.slateNavy;
    const pop = isDark ? '#ffffff' : BRAND.springGreen;
    const baseSize = isDark ? size : size * 0.92;
    return {
      uTime: { value: 0 },
      uMorph: { value: 0 },
      // Halo pass = larger + much fainter → soft bloom-like glow.
      uSize: { value: halo ? baseSize * 2.4 : baseSize },
      uPixelRatio: {
        value: Math.min(typeof window !== 'undefined' ? window.devicePixelRatio : 1, 2),
      },
      uAlpha: { value: halo ? 0.1 : isDark ? 0.55 : 0.85 },
      uColorCore: { value: new THREE.Color(core) },
      uColorEdge1: { value: new THREE.Color(edge1) },
      uColorEdge2: { value: new THREE.Color(edge2) },
      uColorAccent: { value: new THREE.Color(accent) },
      uColorWhite: { value: new THREE.Color(pop) },
    };
  }, [size, isDark, halo]);

  useFrame((_, delta) => {
    const m = matRef.current;
    if (!m) return;
    // Drive the one-shot assembly from OUR OWN monotonic accumulator, not
    // the R3F clock (which resets to 0 on every offscreen pause/resume).
    // Clamp delta so the first resumed frame can't jump the timeline.
    const dt = Math.min(Math.max(delta, 0), 0.05);
    elapsedRef.current += dt;
    morphRef.current = Math.min(morphRef.current + dt / morphDuration, 1);
    m.uniforms.uTime.value = elapsedRef.current;
    m.uniforms.uMorph.value = morphRef.current;
  });

  return (
    <points frustumCulled={false}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[field.home, 3]} />
        <bufferAttribute attach="attributes-aScatter" args={[field.scatter, 3]} />
        <bufferAttribute attach="attributes-aLeaf" args={[field.leaf, 3]} />
        <bufferAttribute attach="attributes-aSeed" args={[field.seed, 1]} />
        <bufferAttribute attach="attributes-aBucket" args={[field.bucket, 1]} />
      </bufferGeometry>
      <shaderMaterial
        ref={matRef}
        uniforms={uniforms}
        vertexShader={VERT}
        fragmentShader={FRAG}
        transparent
        depthWrite={false}
        blending={isDark ? THREE.AdditiveBlending : THREE.NormalBlending}
      />
    </points>
  );
}

import type { ReactNode } from 'react';

/**
 * One icon set, drawn to one spec: a 20×20 box, a 1.6px stroke, round caps and joins, no fills.
 *
 * The set it replaces was assembled from whatever Unicode had to hand (◷ ⊜ ◆ ▤ ☷). Those glyphs come
 * from different type designers at different weights and optical sizes, so a row of them never looks
 * like a set however carefully it is spaced — which is exactly the "assembled from defaults" tell
 * this lane was asked to remove. Drawing five paths is cheaper than trying to make five fonts agree.
 */
function Glyph({ children }: { children: ReactNode }) {
  return <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{children}</svg>;
}

export const icons = {
  /** A clock: today is a moment, and the feed is ordered by it. */
  today: <Glyph><circle cx="10" cy="10" r="7" /><path d="M10 6v4.2l2.6 1.6" /></Glyph>,
  /** Three people on one ring: the circle, and the fact that it has edges. Drawn as three nodes
   *  joined by an arc rather than as a heart — the screen is about who may be told what, which is
   *  a shape question, not a sentiment. */
  care: <Glyph><circle cx="10" cy="10" r="6.6" /><circle cx="10" cy="3.4" r="1.6" /><circle cx="4.3" cy="13.3" r="1.6" /><circle cx="15.7" cy="13.3" r="1.6" /></Glyph>,
  /** A handshake reduced to two halves meeting on a line — and the line is what can drift. */
  agreements: <Glyph><path d="M3 10h14" /><path d="M6.5 10 9 7.2M6.5 10 9 12.8" /><path d="M13.5 10 11 7.2M13.5 10 11 12.8" /></Glyph>,
  /** A funnel: the one thing a taught rule does is narrow what comes through. */
  skills: <Glyph><path d="M3.5 4.5h13l-5 5.6v5.4l-3 1.5v-6.9z" /></Glyph>,
  /** A bracket closing on a line: the boundary the change has to fit inside. */
  limits: <Glyph><path d="M6 3.5H4.2A1.2 1.2 0 0 0 3 4.7v10.6a1.2 1.2 0 0 0 1.2 1.2H6" /><path d="M14 3.5h1.8A1.2 1.2 0 0 1 17 4.7v10.6a1.2 1.2 0 0 1-1.2 1.2H14" /><path d="M7 10h6" /></Glyph>,
  /** Sliders. The only place in the product where something is adjusted rather than decided. */
  settings: <Glyph><path d="M3 6.5h9M15 6.5h2M3 13.5h2M8 13.5h9" /><circle cx="13.5" cy="6.5" r="1.8" /><circle cx="6.5" cy="13.5" r="1.8" /></Glyph>,
};

export type IconName = keyof typeof icons;

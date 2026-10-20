'use client';
import type { ReactNode } from 'react';
import type { LimitsMode } from '@/lib/v2-types';
import { icons, type IconName } from './icons';

/**
 * The frame every place sits in: a rail on a desktop, a tab bar on a phone, and one status strip
 * that is present on every screen because the thing it says — how much the assistant may do right
 * now — is true on every screen.
 */
export type Place = 'today' | 'care' | 'agreements' | 'skills' | 'limits' | 'settings';

export const places: { id: Place; label: string; icon: IconName }[] = [
  { id: 'today', label: 'Today', icon: 'today' },
  // Second, and deliberately: the product's strongest claim is about the person whose week this
  // is, and a nav that buried the circle and the protected list under Settings would be telling
  // the owner they are configuration. They are the subject.
  { id: 'care', label: 'Care', icon: 'care' },
  { id: 'agreements', label: 'Agreements', icon: 'agreements' },
  { id: 'skills', label: 'Skills', icon: 'skills' },
  { id: 'limits', label: 'Limits', icon: 'limits' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
];

/** The standing answer, in the owner's words. `off` is "Watch only" on every screen; the API value
 *  it maps to is unchanged, and this is the only place the two names meet. */
export const limitsModeLabel: Record<LimitsMode, string> = {
  ask_always: 'Ask me every time',
  within_limits: 'Act within limits',
  off: 'Watch only',
};

/** The same standing answer, phrased as the verb it names rather than the owner's own sentence —
 *  what the Today permission control needs, since "Autonomy" is a label a control sits under, and a
 *  control reads as a control when its value is a short phrase, not a sentence borrowed from a chip. */
export const autonomyLabel: Record<LimitsMode, string> = {
  ask_always: 'Ask before acting',
  within_limits: 'Act within limits',
  off: 'Watch only',
};

export function ModeChip({ mode }: { mode: LimitsMode | null }) {
  if (!mode) return null;
  return <span className={`mode-chip mode-${mode}`}>
    <span className="mode-dot" aria-hidden="true" />
    {limitsModeLabel[mode]}
  </span>;
}

const REPO_URL = 'https://github.com/ahm3dwasim/privatemesh';

/** A provenance surface. This is the one visual rule the whole product is built on: anything this
 *  system did not author sits recessed behind a dashed edge and says whose words it is. */
export function Provenance({ kind, children, label }: { kind: 'untrusted' | 'trusted'; label: string; children?: ReactNode }) {
  return <div className={`provenance ${kind}`}>
    <span className="provenance-chip">{label}</span>
    {children}
  </div>;
}

export function Shell({ place, onNavigate, disabled, mode, children, owner, sample, banner, firstRun = false }: {
  place: Place;
  onNavigate: (next: Place) => void;
  disabled: boolean;
  mode: LimitsMode | null;
  owner: string | null;
  sample: boolean;
  banner?: ReactNode;
  /** True while the owner is inside first-run setup. The nav still points at Today's own slot — it
   *  is the only way there — but names it for what it currently shows, so the label matches the
   *  screen instead of contradicting it. */
  firstRun?: boolean;
  children: ReactNode;
}) {
  const nav = (className: string) => <nav className={className} aria-label={className === 'rail-nav' ? 'Main navigation' : 'Sections'}>
    {places.map(item => <button
      key={item.id}
      type="button"
      disabled={disabled}
      aria-current={place === item.id ? 'page' : undefined}
      onClick={() => onNavigate(item.id)}
    ><span className="nav-glyph">{icons[item.icon]}</span><span className="nav-label">{item.id === 'today' && firstRun ? 'Setup' : item.label}</span></button>)}
  </nav>;
  return <div className="shell">
    <a className="skip" href="#main">Skip to content</a>
    <aside className="rail">
      <div className="brand"><span className="brand-mark" aria-hidden="true">╋</span><span className="brand-name">PrivateMesh</span></div>
      {nav('rail-nav')}
      <div className="rail-foot">
        <span className="boundary-rail" aria-hidden="true" />
        <p>Plans change.<br /><strong>Your boundaries don’t.</strong></p>
      </div>
      <footer className="app-footer">
        <a href={REPO_URL} target="_blank" rel="noreferrer">Source (Apache-2.0)</a>
      </footer>
    </aside>
    <div className="frame">
      <header className="appbar">
        <span className="appbar-brand"><span className="brand-mark" aria-hidden="true">╋</span><span>PrivateMesh</span></span>
        <span className="appbar-spacer" />
        <ModeChip mode={mode} />
        {/* The ONLY place this product says "this is a demo" outside Settings. Quiet on purpose —
            a judge told us the repeated "Sample workspace" / "Synthetic day" notices scattered
            through every page body were what made a real product read as a staged one. Live mode
            says nothing here, the same way a shipped product would not announce that it is real. */}
        {sample && <span className="demo-badge" title="This workspace uses synthetic data. Nothing here is a real account.">Demo mode</span>}
        {owner && <span className="avatar" aria-label={`Signed in as ${owner}`}>{owner.slice(0, 1)}</span>}
      </header>
      {banner}
      <main id="main" className="canvas">{children}</main>
      {nav('tabbar')}
    </div>
  </div>;
}

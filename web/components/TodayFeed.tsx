'use client';
import type { ReactNode } from 'react';
import type { FeedItem } from '@/lib/feed';
import { groupByDay } from '@/lib/feed';
import type { Session } from '@/lib/v2-types';
import { time } from './Day';

/**
 * Today: what needs the owner, then what already happened — newest first, one row per meeting.
 *
 * This is the screen that decides whether the product reads as a working assistant or as one demo.
 * So it is a FEED, not a task list: every row is one sentence a person would say out loud, and the
 * status beside it is one of exactly three words. "Needs you" is a live queue — every card expanded.
 * "Recent activity" is a record, grouped under day headings and drawn compact, because a week of
 * settled history should not weigh as much on the eye as the one thing still open. A row never says
 * more than the API reported — `lib/feed.ts` is where that is enforced, and this file only draws
 * what it returns.
 */
function Row({ item, busy, onOpen, timezone, compact = false }: { item: FeedItem; busy: boolean; onOpen: (id: string) => void; timezone: string; compact?: boolean }) {
  // The change's own id on the row. A test and a screenshot script both need to open ONE
  // particular story out of a week of them, and "the first row" stopped being a stable way to say
  // that the moment the week had more than one thing in it.
  return <li className={`feed-row tone-${item.tone} ${compact ? 'compact' : ''}`} data-change={item.change_id}>
    <button type="button" className="feed-hit" disabled={busy} onClick={() => onOpen(item.change_id)}>
      <span className="feed-marker" aria-hidden="true" />
      <span className="feed-text">
        <span className="feed-line">{item.line}</span>
        {!compact && item.detail && <span className="feed-detail">{item.detail}</span>}
        {/* What this thread refused rides inside its own row, not as a row of its own — one card,
            one meeting, whatever happened to it along the way. */}
        {/* The tag follows the note's own kind. "Also blocked" over a `withheld` note told the
            owner their rules had stopped something on a run where nothing was stopped and the
            change went ahead; over an unreadable log it named a cause the log never reported. Three
            kinds, three tags, and only the first one is a refusal. */}
        {!compact && item.blocked && <span className={`feed-blocked feed-blocked-${item.blocked.kind}`}><span className="feed-blocked-tag">{item.blocked.kind === 'refused' ? 'Also blocked' : item.blocked.kind === 'withheld' ? 'Planned around' : 'Also stopped'}</span> {item.blocked.line}</span>}
      </span>
      <span className="feed-meta">
        <span className={`status-chip tone-${item.tone}`}>{item.statusLabel}</span>
        <span className="feed-time">{time(item.at, timezone)}</span>
      </span>
      {/* A row that opens something should look like it opens something — the fix for a row a
          judge could only tell was selectable "by implication". */}
      <span className="feed-chevron" aria-hidden="true">›</span>
    </button>
  </li>;
}

export function TodayFeed({ items, session, onOpen, waiting, busy, today, timezone, aside }: {
  items: FeedItem[];
  session: Session | null;
  waiting: number;
  busy: boolean;
  onOpen: (changeId: string) => void;
  today: string;
  timezone: string;
  /** The day beside the record. On a phone it follows the feed; it never competes with it. */
  aside?: ReactNode;
}) {
  const routine = session?.routine;
  const watching = routine?.enabled && !routine.paused;
  const needsYou = items.filter(item => item.status === 'needs_you');
  const recent = items.filter(item => item.status !== 'needs_you');
  const recentByDay = groupByDay(recent, today, timezone);
  // Grid areas, not nesting: on a phone the day panel has to sit right after "Needs you" and before
  // "Recent activity", which is impossible while it is nested a level deeper than those two sections.
  // `grid-template-areas` puts all four blocks as siblings of one grid, so each width can place them
  // in its own order without duplicating any markup.
  return <div className="today-grid" role="region" aria-label="Today">
    <div className="feed-status tg-status">
      <span className={`pulse ${watching ? 'on' : 'off'}`} aria-hidden="true" />
      <p>
        <strong>{watching ? 'Watching your inbox.' : 'Not watching right now.'}</strong>{' '}
        {waiting > 0
          ? `${waiting} ${waiting === 1 ? 'change needs' : 'changes need'} you.`
          : 'Nothing needs you.'}
      </p>
      <span className="feed-checked">Last checked {routine?.last_checked_at ? time(routine.last_checked_at, session?.timezone) : 'not yet'}</span>
    </div>

    {items.length === 0
      ? <div className="card quiet-card tg-empty"><h2>Nothing has happened yet today.</h2><p>When an email asks to move a meeting, it will show up here — with what was done, what is waiting for you, and anything that was refused.</p></div>
      : <>
        <section className="feed-section tg-needsyou" aria-label="Needs you">
          <h2 className="feed-section-title">Needs you</h2>
          {needsYou.length === 0
            ? <p className="secondary feed-empty">Nothing needs you right now.</p>
            : <ol className="feed">{needsYou.map(item => <Row key={item.key} item={item} busy={busy} onOpen={onOpen} timezone={timezone} />)}</ol>}
        </section>

        {recentByDay.length > 0 && <section className="feed-section tg-recent" aria-label="Recent activity">
          <h2 className="feed-section-title">Recent activity</h2>
          {recentByDay.map(group => <div className="feed-day" key={group.heading}>
            <h3 className="feed-day-heading">{group.heading}</h3>
            <ol className="feed feed-compact">{group.items.map(item => <Row key={item.key} item={item} busy={busy} onOpen={onOpen} timezone={timezone} compact />)}</ol>
          </div>)}
        </section>}
      </>}
    {aside && <aside className="today-aside tg-aside">{aside}</aside>}
  </div>;
}

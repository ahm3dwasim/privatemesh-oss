'use client';
import { apiMode } from '@/lib/api';
import { DemoApp } from '@/components/DemoApp';
import TourApp from '@/components/TourApp';

/**
 * The default landing branches on the BUILD's mode, not on anything read at runtime: a build made
 * with `NEXT_PUBLIC_API_MODE=demo` points at a public demo API and its front door is "Try it live"
 * (`DemoApp`) — the sample workspace stays one click away at `/tour`, unchanged. Every other build
 * keeps exactly the page it had: the sample workspace itself, at `/`.
 */
export default function Home() {
  return apiMode === 'demo' ? <DemoApp /> : <TourApp />;
}

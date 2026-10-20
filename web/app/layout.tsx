import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = { title: 'PrivateMesh — Handle the change', description: 'Handle the change. Keep your boundaries.' };
export default function RootLayout({ children }: {
  children: React.ReactNode;
}) {
  return <html lang="en"><body>{children}</body></html>;
}

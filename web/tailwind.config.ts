import type { Config } from 'tailwindcss';
// Layout and the light-only design tokens live in app/globals.css.
export default {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: { extend: {} },
  plugins: [],
} satisfies Config;

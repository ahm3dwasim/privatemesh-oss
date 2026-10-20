/**
 * The caregiver layer, as pure functions: the two lists the owner keeps, and the one name that
 * makes an approval an approval OF SOMETHING.
 *
 * Nothing here talks to a transport. `artifactId` in particular has to be computable by the
 * BROWSER, from the bytes on the screen, or the property it exists to show is not shown at all:
 * the owner is told that approving a digest approves those exact bytes, and the only way a screen
 * can make that legible is to recompute the name itself and watch it move when a word does. A
 * server-supplied id would be a label beside the content, which is precisely the thing
 * `docs/API_V2.md` says a random id would have been.
 */

/** The three kinds a care-circle member may be listed for. A closed list on the wire
 *  (`422 care_kind_unknown` outside it), so a closed list here. */
export const CARE_KINDS = ['digest', 'reply', 'schedule'] as const;
export type CareKind = (typeof CARE_KINDS)[number];

/** What each grant actually permits, in the owner's words. The screen shows this next to the
 *  checkbox, because "reply" on its own is a word from the API and not a permission anybody can
 *  weigh. */
export const CARE_KIND_MEANING: Record<CareKind, { title: string; body: string }> = {
  digest: { title: 'The family digest', body: 'One summary of the day — what happened, what is coming, what needs you. At most one a day.' },
  reply: { title: 'Replies about care', body: 'An answer this assistant sends on a care thread this person is already part of.' },
  schedule: { title: 'Scheduling only', body: 'Times and places for an appointment. Nothing about health, money or documents.' },
};

/** The six kinds a protected fact may have, and the one placeholder each one has. Mirrors
 *  `care.placeholder` in `src/privatemesh/care.py`: the `[[…]]` shape cannot occur inside an
 *  address, a date or an id, so a placeholder can never be mistaken for a value. */
export const FACT_KINDS = ['benefits_id', 'date_of_birth', 'home_address', 'document_ref', 'phone', 'account'] as const;
export type FactKind = (typeof FACT_KINDS)[number];
export const FACT_KIND_LABEL: Record<FactKind, string> = {
  benefits_id: 'Benefits id',
  date_of_birth: 'Date of birth',
  home_address: 'Home address',
  document_ref: 'Document reference',
  phone: 'Phone number',
  account: 'Account number',
};
export function factPlaceholder(kind: string): string {
  return `[[private:${kind}]]`;
}

/** The four sources a digest fragment may declare, and what each section is called on screen.
 *  `care_greeting` is the composer's own opening line and is the only one with no record behind
 *  it, which is why it is listed here and refused by `DigestLine`. */
export const DIGEST_SECTIONS: { source: string; heading: string }[] = [
  { source: 'care_greeting', heading: 'Opening' },
  { source: 'care_happened', heading: 'What happened' },
  { source: 'care_coming', heading: 'What is coming' },
  { source: 'care_needs_you', heading: 'What needs you' },
];

export interface DigestFragment {
  source: string;
  text: string;
  /** The calendar event or change this sentence was rendered from. Part of the artifact's identity:
   *  two fragments with the same words and different records are two different digests. */
  record_id: string;
}

export interface DigestArtifact {
  day: string;
  recipients: string[];
  fragments: DigestFragment[];
}

/* ------------------------------------------------------------------ sha-256 ------------------ */
/* Written out rather than reached for, because `crypto.subtle.digest` is async and this value is
 * read during a render: an id that arrives one paint later is an id the owner can approve before
 * it is on screen. ~40 lines against that is a good trade. */

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));

export function sha256Hex(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const bitLength = bytes.length * 8;
  const padded = new Uint8Array((((bytes.length + 9) >> 6) + 1) << 6);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  // The length goes in the last eight bytes, big-endian. `bitLength` is well under 2^53 for any
  // string a browser holds, so the high word is computed by division rather than by a shift that
  // would silently wrap at 32 bits.
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bitLength / 0x100000000));
  view.setUint32(padded.length - 4, bitLength >>> 0);
  const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const w = new Uint32Array(64);
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(offset + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15]!, 7) ^ rotr(w[i - 15]!, 18) ^ (w[i - 15]! >>> 3);
      const s1 = rotr(w[i - 2]!, 17) ^ rotr(w[i - 2]!, 19) ^ (w[i - 2]! >>> 10);
      w[i] = (w[i - 16]! + s0 + w[i - 7]! + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = [h[0]!, h[1]!, h[2]!, h[3]!, h[4]!, h[5]!, h[6]!, h[7]!];
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + K[i]! + w[i]!) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h[0] = (h[0]! + a) >>> 0; h[1] = (h[1]! + b) >>> 0; h[2] = (h[2]! + c) >>> 0; h[3] = (h[3]! + d) >>> 0;
    h[4] = (h[4]! + e) >>> 0; h[5] = (h[5]! + f) >>> 0; h[6] = (h[6]! + g) >>> 0; h[7] = (h[7]! + hh) >>> 0;
  }
  return [...h].map(value => value.toString(16).padStart(8, '0')).join('');
}

/* --------------------------------------------------------------- the artifact id -------------- */

/**
 * `json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)`, reproduced.
 *
 * `JSON.stringify` is NOT that function: it writes no spaces after `:` or `,`, and it does not sort
 * keys. Both differences change every byte of the hash, so the browser would compute a name the
 * server has never heard of and the screen's whole claim — that this is the name of these bytes —
 * would be false in the one way nobody would notice. Only the shapes this payload contains are
 * handled: strings, arrays and one flat object with string keys.
 */
export function pythonJson(value: unknown): string {
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(pythonJson).join(', ')}]`;
  const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([key, item]) => `${JSON.stringify(key)}: ${pythonJson(item)}`).join(', ')}}`;
}

/**
 * The name of THIS digest's exact bytes — day, recipients, and every fragment in order, each with
 * the record it names. `docs/API_V2.md` § "What seeing a value means": editing the digest
 * invalidates the approval by not matching, and this is the function that makes that visible before
 * the owner presses anything rather than after.
 *
 * It is byte-for-byte `care.artifact_id`. `lib/care.test.ts` pins it against ids computed by the
 * Python, because a hash that is nearly right is a hash that is wrong.
 */
export function artifactId(artifact: DigestArtifact): string {
  const payload = {
    day: artifact.day,
    recipients: [...artifact.recipients],
    fragments: artifact.fragments.map(fragment => [fragment.source, fragment.text, String(fragment.record_id ?? '')]),
  };
  return `digest-${sha256Hex(pythonJson(payload)).slice(0, 16)}`;
}

/** The short form a screen shows. Never the only thing shown: the full id is in the DOM for anyone
 *  comparing two of them, and this is the form a person can hold in their eye while they read. */
export function shortArtifactId(id: string): string {
  return id.length > 18 ? `${id.slice(0, 14)}…` : id;
}

/* ----------------------------------------------------------------- disclosures ---------------- */

export interface Disclosure {
  /** The fact's LABEL, never its value — the label is what `GET /v2/protected-facts` serves. */
  label: string;
  kind: string;
  /** The exact substring of the fragment that is the protected value. It is in this artifact
   *  because the owner is being asked to approve these bytes; it is nowhere else in this app. */
  value: string;
  /** Which fragment carries it, by index into `fragments`. */
  fragment: number;
}

/**
 * Which recipients of an artifact are inside the circle for a kind, and which are not.
 *
 * Both halves are returned because a disclosure is only approvable when EVERY recipient is a
 * member listed for that kind — so a screen that shows the members without showing the
 * non-members is showing the half of the answer that always looks fine.
 */
export function audience(recipients: string[], members: { email: string; may_receive: string[] }[], kind: string) {
  const listed = new Map(members.map(member => [bareAddress(member.email), member]));
  const inside: { email: string; member: { email: string; may_receive: string[] } }[] = [];
  const outside: string[] = [];
  for (const recipient of recipients) {
    const member = listed.get(bareAddress(recipient));
    if (member && member.may_receive.includes(kind)) inside.push({ email: recipient, member });
    else outside.push(recipient);
  }
  return { inside, outside, approvable: outside.length === 0 && inside.length > 0 };
}

/** The bare address, lowercased — what the provider routes on. Mirrors `CareMember.address`: a
 *  display name is chosen by whoever wrote the message, so a comparison is never on it. */
export function bareAddress(value: string): string {
  const match = /<([^>]+)>/.exec(value ?? '');
  return (match ? match[1]! : String(value ?? '')).trim().toLowerCase();
}

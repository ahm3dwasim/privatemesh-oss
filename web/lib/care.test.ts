import { artifactId, audience, bareAddress, factPlaceholder, pythonJson, sha256Hex } from './care';

/**
 * The artifact id is the one number on the caregiver screens that has to agree with a value
 * computed somewhere else. These expectations were produced by `src/privatemesh/care.py`'s own
 * `artifact_id` over the same three artifacts, not by running this function and writing down what
 * it said — an expectation copied from the implementation proves the implementation is consistent
 * with itself, which is exactly the property a hash does not need.
 */
const FRAGMENTS = [
  { source: 'care_greeting', text: 'Here is the day at a glance.', record_id: '' },
  { source: 'care_happened', text: 'Transport picked Rosa up at 09:30.', record_id: 'evt-transport' },
  { source: 'care_happened', text: "Rosa's physiotherapy was at 11:00.", record_id: 'evt-physio' },
  { source: 'care_coming', text: 'Pharmacy collection at 15:00.', record_id: 'evt-pharmacy' },
  { source: 'care_coming', text: 'Transport will collect Rosa from Flat 3, 118 Wentworth Road, London N17 6XQ at 09:30 tomorrow.', record_id: 'evt-transport-thu' },
  { source: 'care_needs_you', text: 'A prescription refill is waiting for you to confirm collection.', record_id: 'msg-refill' },
  { source: 'care_needs_you', text: 'The clinic moved the physiotherapy appointment and nobody told you.', record_id: 'agr-evt-physio' },
];
const RECIPIENTS = ['rosa.almeida@example.com', 'tom.almeida@example.com'];
const DAY = '2026-09-22';

test('sha256 agrees with the known digests of the empty string and of "abc"', () => {
  expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  expect(sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  // Multi-block, so the padding and the 64-byte loop are both exercised rather than assumed.
  expect(sha256Hex('a'.repeat(1000))).toBe('41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3');
});

test('the JSON is Python’s, not JavaScript’s: sorted keys and a space after every separator', () => {
  expect(pythonJson({ b: '2', a: ['x', 'y'] })).toBe('{"a": ["x", "y"], "b": "2"}');
  // The difference this function exists for. JSON.stringify would hash different bytes.
  expect(pythonJson({ b: '2', a: [] })).not.toBe(JSON.stringify({ b: '2', a: [] }));
});

test('the artifact id is the one `care.artifact_id` computes for the same bytes', () => {
  expect(artifactId({ day: DAY, recipients: RECIPIENTS, fragments: FRAGMENTS })).toBe('digest-8c74fe6a8219d451');
});

test('editing the digest invalidates the approval by not matching', () => {
  const full = artifactId({ day: DAY, recipients: RECIPIENTS, fragments: FRAGMENTS });
  // Drop the one line that carries a protected value.
  const trimmed = artifactId({ day: DAY, recipients: RECIPIENTS, fragments: FRAGMENTS.filter(f => f.record_id !== 'evt-transport-thu') });
  expect(trimmed).toBe('digest-203f4a22a1665b46');
  expect(trimmed).not.toBe(full);
  // Drop a recipient. Approving a field for one recipient does not approve it for another, so the
  // audience is part of the identity.
  const narrower = artifactId({ day: DAY, recipients: ['rosa.almeida@example.com'], fragments: FRAGMENTS });
  expect(narrower).toBe('digest-47600250c6b3ffe8');
  expect(narrower).not.toBe(full);
});

test('the record a sentence names is part of the identity, not decoration beside it', () => {
  const swapped = FRAGMENTS.map(f => f.record_id === 'evt-physio' ? { ...f, record_id: 'evt-transport' } : f);
  expect(artifactId({ day: DAY, recipients: RECIPIENTS, fragments: swapped }))
    .not.toBe(artifactId({ day: DAY, recipients: RECIPIENTS, fragments: FRAGMENTS }));
});

test('a placeholder is a name a model can reason with and never a value', () => {
  expect(factPlaceholder('benefits_id')).toBe('[[private:benefits_id]]');
  expect(factPlaceholder('home_address')).toBe('[[private:home_address]]');
});

test('an audience is approvable only when every recipient is listed for that kind', () => {
  const members = [
    { email: 'rosa.almeida@example.com', may_receive: ['digest', 'reply'] },
    { email: 'tom.almeida@example.com', may_receive: ['digest'] },
    { email: 'front.desk@northgate.example.com', may_receive: ['schedule'] },
  ];
  expect(audience(RECIPIENTS, members, 'digest').approvable).toBe(true);
  // A member who exists but was not listed for this kind is OUTSIDE, not inside: the grant is
  // per-kind, and a circle where membership alone cleared a send would be an address book.
  const withClinic = audience([...RECIPIENTS, 'front.desk@northgate.example.com'], members, 'digest');
  expect(withClinic.approvable).toBe(false);
  expect(withClinic.outside).toEqual(['front.desk@northgate.example.com']);
  // A stranger is outside for every kind.
  expect(audience(['records@benefits-verify.example.net'], members, 'digest').outside).toHaveLength(1);
  // An empty audience is not an approval to nobody.
  expect(audience([], members, 'digest').approvable).toBe(false);
});

test('an address is compared bare and lowercased, never on the display name', () => {
  expect(bareAddress('Rosa Almeida <Rosa.Almeida@Example.com>')).toBe('rosa.almeida@example.com');
  expect(bareAddress('  TOM@EXAMPLE.COM ')).toBe('tom@example.com');
});

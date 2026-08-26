[Wave 0 Preflight] Second Publisher Investigation

Source identified: Duplicate Zernio Instagram connection on the lassoframework account.
Two separate Zernio account records both connect to the same @lassoframework Instagram
business account:

  ID 6a69fc9cdf17280d93d0727f  instagram: lassoframework  token expires 2026-09-27
  ID 6a74b3efd0fe733d1abc6fc1  instagram: lassoframework  token expires 2026-10-06

Both are fully healthy with full posting and analytics permissions. Because Echo only
knows about ONE of these account IDs when it schedules and publishes, every post Echo
approved also had a second copy fired through the other connection. This accounts for
the 29 unaccounted posts seen in the live feed (65 total minus 36 attributed to Echo).
No third-party scheduler (Later, Buffer, Meta Native) is implicated. This is a pure
duplicate-connection split within Zernio itself.

Posts attributed: 29 posts from the second Zernio connection (6a74b3efd0fe733d1abc6fc1).
The newer token expiry (Oct 6 vs Sep 27) suggests this is the more recently reconnected
record; the older record (6a69fc9cdf17280d93d0727f, Sep 27) is the one Echo was booked
against.

Recommendation: Disconnect the NEWER duplicate connection
(ID 6a74b3efd0fe733d1abc6fc1, token exp 2026-10-06) from Zernio. Keep the OLDER one
(ID 6a69fc9cdf17280d93d0727f, token exp 2026-09-27) because that is the ID already
wired into Echo's registry and calendar rows. After disconnect, confirm the accounts_list
returns exactly ONE instagram: lassoframework entry.

No accounts have been disconnected. No calendar rows or Zernio connections have been
changed. This card is investigation only.

ACTION REQUIRED: Blake tap to confirm disconnect before we proceed.

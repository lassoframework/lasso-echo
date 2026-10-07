# GBP provider attempt fence

Draft only. Apply after the forward media claim and GBP final send authority drafts. No production migration or activation was performed.

The final authorization RPC now commits an immutable attempt with the exact creative, native account, location and claim token. Its first successful response authorizes one invocation. A repeat returns false, including an exact same token replay. A lost response or crash therefore remains held for reconciliation; it cannot automatically authorize another send.

That commit is the irrevocable authorization point. Subsequent gate disable or connection edits apply to future grants. They do not cancel an already granted invocation. The calendar trigger prevents clearing or replacing the unresolved lease, editing its outgoing creative, deleting its row or truncating calendar. Existing confirmed published or definite failed terminal writes may clear the token. Ambiguous outcomes must stay publishing. There is no timeout release.

The fence establishes at most one RPC grant per token, assuming each successful caller invokes its provider once. It cannot prove the network invocation occurred, cancel a request already granted, or stop a malicious caller from invoking the provider twice. DB owners can disable triggers or falsify a terminal outcome; provider credentials and privileged database access remain trusted. Definitive failure followed by a new independently authorized token is a new attempt.

The worker uses its existing final RPC and literal true check. OFF behavior stays unchanged. If the RPC committed but its response was lost, the existing hold/release path encounters the database fence instead of silently releasing the lease; this is an operator reconciliation condition. Fence coverage is GBP post and gallery only. Other provider lanes require separate integration.

Validation uses a disposable local PG17 cluster, synthetic authority and no provider traffic. The fixture checks concurrent single grant, replay denial, creative and lease mutation refusal, delete/truncate refusal, permanent attempt retention, gallery parity, gate/destination edits after commit and existing terminal transitions.

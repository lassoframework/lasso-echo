# Forward media owner and attester process contracts

Both lanes default to no production operation. Deploy each in its own process
with a dedicated least-privilege DSN and exact role. Never run either with the
shared Echo publisher environment. `forward_media_lane.py` contains the exact
allowed names; all other names fail closed, including empty values, unknown
aliases, GOOGLE_DRIVE_SA_JSON, PGPASSWORD, proxy settings, and opposite-lane DSNs.
The owner checks the contract at packet apply, connection construction and
transaction entry. The attester checks it at worker settings and direct database
connection. The shared publisher and its S3 reader are unchanged.

The owner original reader uses only `FORWARD_MEDIA_OWNER_DRIVE_SA_JSON`, an
inline JSON key for a dedicated Google service account with Viewer access to
the approved gym source folders. It requests only `drive.readonly`, uses the
fixed Google OAuth token endpoint and a 30-second HTTP timeout, and performs
original downloads without retries. Credential file paths, ADC, delegated user
subjects, shared Drive aliases and publisher credentials remain forbidden.
This name is owner-only and rejected by the attester. Missing or invalid keys
leave original verification held; they never fall back to shared Echo secrets.

Use `agent.forward_media_lane_launcher.launch_isolated(lane, arguments,
environment=dedicated_mapping)` from an operator supervisor. Its required mapping
is passed explicitly as the complete child environment, never inherited. The
mapping must come from the lane's dedicated secret configuration. Do not pass
`dict(os.environ)` or log the mapping. The launcher fixes the owner and attester
module entrypoints; use owner `--packet <local-path>` (dry run) or explicit
`--apply`, and attester `--once` or its standalone loop. Provision worker and guard
flags only after release acceptance. Python must already have the existing
runtime dependencies; no per-worker installation is required.

The credential-free reader uses only `AGENT_S3_PUBLIC_BASE_URL`, the existing
public CDN configuration documented in ENV.md. An unset origin, non-HTTPS origin,
private IP or private DNS result holds. It performs an HTTPS GET of the exact URL,
including its query, under the configured origin/path. It refuses redirects,
non-200/private responses, encoded bodies and bodies above 128 MiB. Every read
creates a requests Session with `trust_env=False`; ambient netrc credentials,
proxies and S3 credentials are unused. Approved explicit CA paths are applied
separately. Reads have five-second connection/socket timeouts and a 30-second
stream deadline checked after each read1 call, avoiding full-chunk slow-drip
blocking. A blocking socket read may last up to five seconds beyond the deadline;
DNS uses the host resolver. Unsupported raw.read1 runtimes hold rather than fall
back to an unbounded reader.

Release requirements remain: independently accept this patch, perform the
required exact-head cloud review, configure and verify a real approved public
CDN origin and immutable/versioned object paths, provision the draft authority
schema and narrow dedicated roles through the approved migration workflow, and
verify tenant/provenance/rollback behavior on the deployed lane before activation.
No production public origin is present in this repository. Private S3-only
objects cannot be attested through this lane; arrange approved public immutable
objects or leave the lane held. DNS is resolved again by the HTTP transport;
operator ownership of the approved CDN DNS and network egress controls remain
required. No production DSN, migration, service, feature flag or publisher change
was applied for this patch.

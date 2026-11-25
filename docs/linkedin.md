# LinkedIn in Viks Annonsverktyg

This optional native adapter runs inside the existing Python MCP service. It
calls LinkedIn directly. No Pipeboard, AdPlug, or other hosted marketing relay is
used, and the Node community server is not executed or proxied.

## First connection

1. The LinkedIn developer app must have Advertising API access. In its **Auth**
   tab add the exact authorized redirect URL:
   `https://app.viksannonsverktyg.se/linkedin/callback`
2. Set `LINKEDIN_MCP_CLIENT_ID` to the app's client ID in Cloud Run. Inject
   `LINKEDIN_MCP_CLIENT_SECRET` from Secret Manager secret
   `linkedin-oauth-client-secret`. The runtime service account needs Secret
   Manager Secret Accessor on this secret. Do not overwrite Google's credentials.
3. Keep `LINKEDIN_MCP_WRITE_MODE=dry_run` initially. The existing public
   `GOOGLE_ADS_MCP_BASE_URL`, owner approval secret, Firestore project/database,
   and stable storage encryption secret are used. Grant records are encrypted
   separately with a purpose-specific key; no Google grants are modified.
4. Publish this source through the existing GitHub-to-Cloud-Build deployment.
5. Refresh the MCP tool list in the client, call `linkedin_connection_status`,
   then `linkedin_connect`. Open the returned link in your own browser. Log in
   as `owner` with the existing separate approval password, continue to LinkedIn,
   and authorize the account. The browser redirect URL is not the Google callback.
6. Call `linkedin_list_accounts`, then list campaigns and request a short report.
   These are live read tests. Successful unit tests do not prove account access.

The MCP connection still uses Google to identify its user. LinkedIn is an
additional grant bound to that verified user, not a Gmail-based LinkedIn login.
Connect links expire after ten minutes and can be used only once. Callback state
is consumed transactionally, bound to an HttpOnly browser cookie, and survives
Cloud Run restarts. A new connection invalidates previous plans for that user.

Access tokens expire according to LinkedIn's token response. This initial
adapter does not promise automatic refresh-token entitlement or renewal. An
expired token requires `linkedin_connect` again. `linkedin_disconnect` deletes
the caller's local grant; LinkedIn-side app revocation is managed on LinkedIn.

## Reads and writes included

- Account, campaign group and campaign listing with explicit pagination.
- Individual campaign settings, including account membership checks.
- Daily campaign impressions, clicks, local-currency spend and website
  conversions for an explicit date range. Attribution remains LinkedIn's.
- Local-only previews of pausing, changing a daily budget, or creating PAUSED
  Sponsored Content campaign settings in an existing campaign group.
- Separate password-protected browser approval, one-shot execution, status,
  and read-only reconciliation of uncertain or expired plans.

For a paused STANDARD_UPDATE campaign with a website visit or conversion objective,
call `linkedin_image_upload_link` with the account and campaign IDs. Open the
ten-minute link as owner and submit a JPG or PNG (5 MiB and 16 megapixels max).
The service uploads directly to the campaign Page's LinkedIn media library.
Copy the returned image URN into `linkedin_preview_image_ad` together with the
copy, headline, HTTPS destination and CTA. Preview waits for an AVAILABLE image
owned by the campaign Page. Review the image and text in the owner approval
page, then execute the plan once. The ad is created PAUSED and its account,
campaign, name and paused status are checked. Activation is a separate manual
decision in Campaign Manager. The member needs appropriate ad account and
Page permissions to create Direct Sponsored Content.


## Enabling a particular account

After live reads, the owner must choose LinkedIn-specific accounts and budget
ceilings. Google customer IDs, MCC membership and Google's SEK ceiling do not
grant LinkedIn write access. Configure an explicit policy, for example using
this **synthetic** LinkedIn account ID and ceiling:

```json
{"123456789":{"currency":"SEK","max_daily_budget":"1000.00"}}
```

Store that mapping in `LINKEDIN_MCP_WRITE_ACCOUNTS`, then set
`LINKEDIN_MCP_WRITE_MODE=approval_required`. Amounts are decimal strings in the
account currency, not Google micros. A budget policy is a tool constraint, not a
promise about LinkedIn's daily delivery/billing behavior. The server exposes no
campaign activation tool in this version.

Approval routes are `/linkedin/approvals/{full_plan_id}`. They are protected
separately from MCP OAuth and reject cross-origin, oversized and invalid forms.
Only the owner browser can approve a plan. Names and targeting returned by
LinkedIn are untrusted data, not instructions.

Before execution the service rechecks account currency, policy, the connection
identity and the campaign snapshot. It acquires a per-account Firestore lock
and sends each mutation once, without automatic retries. Creation uses a unique,
owner-reviewed name suffix to identify a campaign after a lost response. On an
uncertain result call `linkedin_reconcile_plan` with the **full** plan ID. It
reads remote state and never repeats the mutation. A worker left executing by a
crash can be reconciled after a fifteen-minute grace period. Unverified results
keep the lock and require investigation.

## Storage and rollout

Existing Google collections and locks are unchanged. LinkedIn uses
`viks_linkedin_grants`, `viks_linkedin_oauth_nonces`,
`viks_linkedin_write_plans`, and `viks_linkedin_write_locks`.
Enable Firestore TTL on `delete_after` for the first three collections. Nonces
are unusable after ten minutes even if TTL has not yet removed them. Grant TTL
is token expiry plus one day. Resolved plans use the existing thirty-day
retention marker; unresolved plans retain their evidence and lock.

Deploy with writes in dry-run and no enabled accounts. Verify live reads first,
then an owner-approved reversible change in an explicitly selected account.
Rollback by disabling the `linkedin` namespace or reverting this commit while
preserving its Firestore records for investigation. To stop all LinkedIn writes
set `LINKEDIN_MCP_WRITE_MODE=dry_run`; revoke the grant separately if required.

## Source evidence checked 2026-11-24

- [LinkedIn OAuth authorization code flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [LinkedIn API permissions and access](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access)
- [Campaign settings and account-scoped REST routes](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaigns?view=li-lms-2026-11)
- [Campaign groups](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaign-groups?view=li-lms-2026-11)
- [Ad reporting and its limitations](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting?view=li-lms-2026-11)

Default `LINKEDIN_MCP_API_VERSION` is `202611`. Recheck official version support
before a later release. API shape and permissions still require the live read
and write acceptance tests above.

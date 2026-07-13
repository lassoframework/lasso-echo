# WhatsApp Intake Setup

## Overview

Echo receives client media through the WhatsApp Business API (Meta Cloud API).
This is the hosted Cloud API, not the On-Premises option. Receiving media from
clients requires the `whatsapp_business_messaging` permission, which is granted
only after Meta App Review.

Do not arm `AGENT_WHATSAPP_INTAKE_ENABLED` until Meta App Review grants
`whatsapp_business_messaging` for this application.

## Meta Setup Steps

1. Log in to developers.facebook.com with the LASSO business account.
2. Create a new app. Choose Business as the app type.
3. Add the WhatsApp product to the app from the Add Products dashboard.
4. Under WhatsApp > Getting Started, select or create a phone number for the business.
5. Note the Phone Number ID shown on that page.
6. Under App Settings > Basic, note the App Secret.
7. Generate a permanent System User token scoped to `whatsapp_business_messaging`.
   This is the token value for `AGENT_WHATSAPP_TOKEN`.
8. Choose a Verify Token string. This can be any random string you pick. It is
   used only during webhook registration. Set it as `AGENT_WHATSAPP_VERIFY_TOKEN`.

## Webhook Configuration

In the Meta developer console under WhatsApp > Configuration:

1. Set the Callback URL to `https://<intake-web-domain>/whatsapp`
2. Set the Verify Token to the value you chose for `AGENT_WHATSAPP_VERIFY_TOKEN`
3. Click Verify and Save. Meta will send a GET request to the callback URL; the
   intake web service responds with the hub challenge.
4. Under Webhook Fields, subscribe to the `messages` field.

The intake web service handles both the GET verification request and the POST
webhook events at the same path.

## Environment Variables

All secrets and tokens are set by hand in the deployment environment. They are
never logged, printed, or committed. Owner for every variable is BLAKE.

| Variable | Default | Owner | Notes |
|---|---|---|---|
| `AGENT_WHATSAPP_INTAKE_ENABLED` | false | BLAKE | Arm only after App Review approval |
| `AGENT_WHATSAPP_APP_SECRET` | (none) | BLAKE | From App Settings > Basic |
| `AGENT_WHATSAPP_TOKEN` | (none) | BLAKE | System User token for messaging |
| `AGENT_WHATSAPP_PHONE_NUMBER_ID` | (none) | BLAKE | From WhatsApp > Getting Started |
| `AGENT_WHATSAPP_VERIFY_TOKEN` | (none) | BLAKE | Random string chosen at setup |

## Verify Your Setup

Run the status command to confirm all variables are set before arming the flag:

```
python -m agent whatsapp-status
```

The output shows `preflight: PASS` when all five variables are set and the flag
is enabled. It shows `WARN (disabled)` when the flag is off (normal before App
Review). It shows `FAIL` when the flag is on but one or more variables are missing.

## Notes

Incoming media is accepted from registered sender phones only. Unknown senders
are held with one ops alert and never staged. Media above 16 MB is refused and
never truncated; ask the client to use the upload link instead.

The receipt reply sent after successful media ingest reads: "Got it! Your media
is in review. We will reach out if we need anything else."

No reply is ever sent to non-media messages.

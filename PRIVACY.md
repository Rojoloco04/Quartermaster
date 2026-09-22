# Privacy Policy

Quartermaster is a personal assistant that one person runs on their own
computer for their own accounts. It is not a hosted service, and no one else
signs in to it.

## What it accesses

When you authorise a Google account, Quartermaster requests:

- **Google Calendar** (read and write): to answer questions about your schedule
  and to add events when you ask.
- **Gmail** (read-only): to search and read your email when you ask. It cannot
  send, delete, or change mail.

## Where your data goes

- OAuth tokens are stored only on the computer running Quartermaster, in the
  user's local configuration directory.
- Calendar and email content is fetched when needed to answer a request. It is
  passed to Anthropic's Claude model to write the answer, under the account
  holder's own Anthropic subscription.
- Nothing is sold, shared with third parties, or used for advertising.
- Nothing is sent to the developer of this software.

## Google API data

Quartermaster's use of information received from Google APIs follows the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Removing access

Revoke access at any time at <https://myaccount.google.com/permissions>, and
delete the local token files to remove the stored credentials.

## Contact

Open an issue on this repository's GitHub Issues page.

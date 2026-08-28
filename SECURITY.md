# Security

SpotiDown is designed so local secrets and runtime media are not committed by default.

Never commit or share:

- `.env`
- Spotify Client Secrets
- authenticated proxy URLs
- browser/YouTube cookies
- `library.sqlite`
- downloaded audio files

If a credential was exposed in an older archive or Git history, rotate it at the provider. Deleting the visible string from a later commit does not revoke the old credential.

For security reports related to the repository code, contact the maintainer through the GitHub profile: **@mebularts**.

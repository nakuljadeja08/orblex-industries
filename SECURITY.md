# Security

This code can connect to a real brokerage account, so please report security problems privately, not in a public issue. Use GitHub's "Report a vulnerability" button on the Security tab of this repository.

When you report anything, or open an issue or pull request, never include API keys, tokens, `.env` contents, account numbers, screenshots of an account, or real trades.

If you run your own copy:
- Keep `.env` out of git, and store keys as GitHub Actions secrets, which are masked in logs.
- Run `private_access_schema.sql` last, so the dashboard's public key can read nothing.
- Keep `EXECUTOR_LIVE` unset until you have watched the dry run for weeks, and keep your broker's per-order approval on.

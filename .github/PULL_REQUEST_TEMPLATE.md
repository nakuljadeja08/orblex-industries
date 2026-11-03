<!--
  Merging into `release` deploys to production on Vercel.
  Preview deploys are created automatically for other branches.
-->

## Summary

<!-- What does this PR change and why? One or two sentences. -->

## Type of change

- [ ] Dashboard / frontend (`src/`, Vite build)
- [ ] Pipeline / backend (Python agents, schemas)
- [ ] Config / infra (`vercel.json`, `.github`, env vars)
- [ ] Docs

## Deployment

- [ ] Merging into **`release`** (production deploy) — otherwise this is a preview only
- [ ] New/changed env vars documented and added in **Vercel → Settings → Environment Variables**
- [ ] No secrets committed (`.env` stays gitignored)

## Verification

- [ ] `npm run build` succeeds locally
- [ ] Dashboard loads and reads from Supabase without console errors
- [ ] Backend/pipeline changes run cleanly (if applicable)

## Notes

<!-- Screenshots, follow-ups, or anything reviewers should know. -->

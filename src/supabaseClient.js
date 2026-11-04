// supabaseClient.js — the trading database's client library. The demo build (vite.config.js) swaps
// this one file for src/demo/fakeSupabase.js, so the dashboard runs on sample data there, while the
// playground (src/demo/Playground.jsx) still talks to its own, separate Supabase project.
export { createClient } from "@supabase/supabase-js";
